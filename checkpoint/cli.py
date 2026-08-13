"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tarfile
from pathlib import Path

from . import __version__
from .backup import BackupRunner
from .config import Config, ConfigError
from .discovery import discover_repos
from .github_client import GitHubClient, GitHubError
from .logging_setup import setup_logging
from .retention import select_expired
from .storage import build_backend
from .util import human_size, run

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkpoint",
        description="Back up GitHub repositories (git history, issues, PRs, releases, ...) to Google Drive.",
    )
    parser.add_argument("--version", action="version", version=f"checkpoint {__version__}")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="perform a backup (default command)")
    run_cmd.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    run_cmd.add_argument("--full", action="store_true", help="ignore incremental state, export everything")
    run_cmd.add_argument("--repo", action="append", default=[], help="limit to owner/repo (repeatable)")
    run_cmd.add_argument("--json", action="store_true", help="print the run manifest as JSON")

    sub.add_parser("check", help="validate token, scopes and storage backend")
    list_cmd = sub.add_parser("list", help="list the repositories that would be backed up")
    list_cmd.add_argument("--json", action="store_true")

    prune_cmd = sub.add_parser("prune", help="apply retention rules without backing up")
    prune_cmd.add_argument("--dry-run", action="store_true")

    restore_cmd = sub.add_parser("restore", help="unpack a snapshot archive and rebuild a working clone")
    restore_cmd.add_argument("archive", help="path to <owner>__<repo>.tar.gz from a snapshot")
    restore_cmd.add_argument("dest", help="directory to restore into")

    return parser


def _load(args) -> Config:
    cfg = Config.load(args.config if Path(args.config).exists() or args.config != "config.yaml" else None)
    if getattr(args, "repo", None):
        cfg.data["github"]["include"] = list(args.repo)
    setup_logging(args.log_level or cfg.get("runtime.log_level", "INFO"))
    if cfg.source:
        log.debug("loaded config from %s", cfg.source)
    else:
        log.warning("no config.yaml found; using built-in defaults")
    return cfg


def cmd_check(cfg: Config) -> int:
    ok = True
    try:
        token = cfg.resolve_token()
    except ConfigError as exc:
        print(f"token:    FAIL - {exc}")
        return 1

    client = GitHubClient(
        token,
        api_url=cfg.get("github.api_url"),
        graphql_url=cfg.get("github.graphql_url"),
        timeout=int(cfg.get("runtime.request_timeout", 60)),
    )
    try:
        user = client.viewer()
        scopes = client.token_scopes()
        print(f"token:    OK - authenticated as {user.get('login')}")
        print(f"scopes:   {', '.join(scopes) if scopes else '(fine-grained token)'}")
        if scopes and "repo" not in scopes:
            print("          WARNING: classic tokens need the 'repo' scope for private repositories")
        rate = client.rate_limit().get("resources", {}).get("core", {})
        print(f"rate:     {rate.get('remaining')}/{rate.get('limit')} core requests remaining")
    except GitHubError as exc:
        print(f"token:    FAIL - {exc}")
        return 1

    backend = build_backend(cfg)
    try:
        backend.check()
        print(f"storage:  OK - backend '{backend.name}'")
    except Exception as exc:
        print(f"storage:  FAIL - {exc}")
        ok = False

    try:
        repos = discover_repos(client, cfg)
        private = sum(1 for r in repos if r.get("private"))
        print(f"repos:    {len(repos)} selected ({private} private)")
    except GitHubError as exc:
        print(f"repos:    FAIL - {exc}")
        ok = False

    return 0 if ok else 1


def cmd_list(cfg: Config, as_json: bool) -> int:
    client = GitHubClient(cfg.resolve_token(), api_url=cfg.get("github.api_url"))
    repos = discover_repos(client, cfg)
    if as_json:
        print(json.dumps([r["full_name"] for r in repos], indent=2))
        return 0
    for repo in repos:
        flags = "".join(
            [
                "P" if repo.get("private") else "-",
                "F" if repo.get("fork") else "-",
                "A" if repo.get("archived") else "-",
            ]
        )
        size = human_size((repo.get("size") or 0) * 1024)
        print(f"{flags}  {repo['full_name']:<50} {size:>10}  {repo.get('pushed_at', '')}")
    print(f"\n{len(repos)} repositories (flags: P=private F=fork A=archived)")
    return 0


def cmd_prune(cfg: Config, dry_run: bool) -> int:
    backend = build_backend(cfg)
    fmt = cfg.get("output.snapshot_name")
    rules = {
        "keep_last": int(cfg.get("retention.keep_last", 0) or 0),
        "keep_daily": int(cfg.get("retention.keep_daily", 0) or 0),
        "keep_weekly": int(cfg.get("retention.keep_weekly", 0) or 0),
        "keep_monthly": int(cfg.get("retention.keep_monthly", 0) or 0),
    }
    if not any(rules.values()):
        print("no retention rules configured; nothing to prune")
        return 0

    if backend.name != "none":
        backend.check()
        names = backend.list_snapshots("")
        expired = select_expired(names, fmt, **rules)
        print(f"remote: {len(names)} snapshots, {len(expired)} expired")
        for name in expired:
            print(f"  {'would delete' if dry_run else 'deleting'} {name}")
            if not dry_run:
                backend.delete(name)

    local_dir = Path(cfg.get("output.dir", "./backups")).expanduser()
    if local_dir.is_dir():
        names = [p.name for p in local_dir.iterdir()]
        expired = select_expired(names, fmt, **rules)
        print(f"local:  {len(names)} snapshots, {len(expired)} expired")
        for name in expired:
            print(f"  {'would delete' if dry_run else 'deleting'} {name}")
            if not dry_run:
                from .util import rmtree

                rmtree(local_dir / name)
    return 0


def cmd_restore(archive: str, dest: str) -> int:
    src = Path(archive).expanduser()
    target = Path(dest).expanduser()
    if not src.is_file():
        print(f"not a file: {src}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)

    with tarfile.open(src) as tar:
        tar.extractall(target)
    roots = [p for p in target.iterdir() if p.is_dir()]
    root = roots[0] if len(roots) == 1 else target
    print(f"extracted to {root}")

    bundle = root / "git" / "repo.bundle"
    if bundle.is_file():
        clone_dir = root / "working-copy"
        proc = run(["git", "clone", str(bundle), str(clone_dir)], check=False)
        if proc.returncode == 0:
            print(f"working clone: {clone_dir}")
            print("  (its 'origin' points at the bundle; run `git remote set-url origin <url>` to re-point it)")
        else:
            print(f"git clone from bundle failed:\n{proc.stderr}", file=sys.stderr)
    api_dir = root / "api"
    if api_dir.is_dir():
        files = sorted(p.name for p in api_dir.glob("*.json"))
        print(f"api data: {', '.join(files)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "restore":
        setup_logging(args.log_level or "INFO")
        return cmd_restore(args.archive, args.dest)

    try:
        cfg = _load(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        if command == "check":
            return cmd_check(cfg)
        if command == "list":
            return cmd_list(cfg, getattr(args, "json", False))
        if command == "prune":
            return cmd_prune(cfg, getattr(args, "dry_run", False))

        runner = BackupRunner(
            cfg,
            dry_run=getattr(args, "dry_run", False),
            force_full=getattr(args, "full", False),
        )
        manifest = runner.run()
        if getattr(args, "json", False):
            print(json.dumps(manifest, indent=2, default=str))
        return 1 if manifest.get("failed") else 0
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("%s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
        return 1
