"""Orchestrates a full backup run: discover -> export -> archive -> upload -> prune."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .discovery import discover_repos
from .exporters import RepoApiExporter, export_account, export_git, export_wiki
from .github_client import GitHubClient
from .packager import archive_dir, write_manifest
from .redaction import RepoNameRedactor
from .retention import select_expired
from .storage import build_backend
from .util import human_size, read_json, rmtree, safe_name, sha256_file, utc_stamp, write_json

log = logging.getLogger(__name__)

STATE_REMOTE_NAME = "state.json"


class BackupRunner:
    def __init__(self, cfg: Config, *, dry_run: bool = False, force_full: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.force_full = force_full
        self.token = cfg.resolve_token()
        self.client = GitHubClient(
            self.token,
            api_url=cfg.get("github.api_url"),
            graphql_url=cfg.get("github.graphql_url"),
            timeout=int(cfg.get("runtime.request_timeout", 60)),
            max_retries=int(cfg.get("runtime.max_retries", 6)),
        )
        self.backend = build_backend(cfg)
        self.output_dir = Path(cfg.get("output.dir", "./backups")).expanduser()
        self.work_root = Path(cfg.get("output.work_dir", "./work")).expanduser()
        self.state_path = Path(cfg.get("runtime.state_file", ".checkpoint-state.json")).expanduser()
        self.snapshot_fmt = cfg.get("output.snapshot_name", "%Y-%m-%dT%H-%M-%SZ")
        # 러너 디스크가 좁을 때: 레포 아카이브를 만드는 즉시 올리고 지웁니다.
        self.stream_upload = bool(cfg.get("output.stream_upload", False)) and self.backend.name != "none"
        self._uploaded: dict[str, tuple[str, int]] = {}
        self._uploaded_lock = threading.Lock()
        self.redactor = RepoNameRedactor(bool(cfg.get("runtime.redact_repo_names", False)))
        if self.redactor.enabled:
            for handler in logging.getLogger().handlers:
                handler.addFilter(self.redactor)

    # -- state ----------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            # A fresh runner (e.g. CI) can pick the state back up from the remote.
            self.backend.download(STATE_REMOTE_NAME, self.state_path)
        return read_json(self.state_path, default={}) or {}

    def save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)
        if not self.dry_run and self.backend.name != "none":
            try:
                self.backend.upload(self.state_path, STATE_REMOTE_NAME)
            except Exception as exc:  # non-fatal: state is an optimisation
                log.warning("could not upload state file: %s", exc)

    # -- per repo -------------------------------------------------------
    def _repo_changed(self, repo: dict[str, Any], state: dict[str, Any]) -> bool:
        previous = (state.get("repos") or {}).get(repo["full_name"])
        if not previous:
            return True
        return (
            previous.get("pushed_at") != repo.get("pushed_at")
            or previous.get("updated_at") != repo.get("updated_at")
        )

    def backup_repo(self, repo: dict[str, Any], snapshot_dir: Path, snapshot_name: str) -> dict[str, Any]:
        full_name = repo["full_name"]
        slug = safe_name(full_name)
        started = time.time()
        result: dict[str, Any] = {
            "repo": full_name,
            "status": "exported",
            "private": bool(repo.get("private")),
            "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")),
            "warnings": [],
        }

        repo_dir = snapshot_dir / "repos" / slug
        work_dir = self.work_root / slug
        repo_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            if self.cfg.wants("git"):
                clone_url = repo.get("clone_url") or f"https://github.com/{full_name}.git"
                result["git"] = export_git(
                    clone_url=clone_url,
                    dest=repo_dir / "git",
                    token=self.token,
                    work_dir=work_dir,
                    pull_refs=self.cfg.wants("git_pull_refs"),
                    lfs=self.cfg.wants("git_lfs"),
                )

            if self.cfg.wants("wiki") and repo.get("has_wiki"):
                wiki_url = f"https://github.com/{full_name}.wiki.git"
                wiki = export_wiki(
                    wiki_url=wiki_url,
                    dest=repo_dir / "git",
                    token=self.token,
                    work_dir=work_dir,
                )
                result["wiki"] = wiki or "none"

            exporter = RepoApiExporter(self.client, repo, repo_dir / "api", self.cfg)
            api_result = exporter.run(self.token)
            result["counts"] = api_result["counts"]
            result["warnings"].extend(api_result["warnings"])
        finally:
            rmtree(work_dir)

        if self.cfg.get("output.archive", True):
            archive = archive_dir(
                repo_dir,
                snapshot_dir / "repos",
                slug,
                compression=self.cfg.get("output.compression", "gz"),
            )
            result["archive"] = archive.name
            result["bytes"] = archive.stat().st_size
            if self.stream_upload:
                self._stream(archive, snapshot_dir, snapshot_name, f"repos/{archive.name}")
                result["streamed"] = True

        result["seconds"] = round(time.time() - started, 1)
        return result

    def _stream(self, path: Path, snapshot_dir: Path, snapshot_name: str, relative: str) -> None:
        """Upload one finished file, record its checksum, then free the local copy."""
        digest = sha256_file(path)
        size = path.stat().st_size
        self.backend.upload(path, f"{snapshot_name}/{relative}")
        with self._uploaded_lock:
            self._uploaded[relative] = (digest, size)
        path.unlink()
        log.debug("streamed %s (%s) and freed local copy", relative, human_size(size))

    # -- main -----------------------------------------------------------
    def run(self) -> dict[str, Any]:
        started = time.time()
        if self.backend.name != "none":
            self.backend.check()

        state = self.load_state()
        repos = discover_repos(self.client, self.cfg)
        run_warnings: list[str] = []
        if not repos:
            message = (
                "no repositories matched the current selection filters - check "
                "github.include/exclude and that the token can see your repositories"
            )
            log.warning(message)
            run_warnings.append(message)

        # 이름을 가릴 거라면 로그가 나가기 전에 등록해야 합니다.
        self.redactor.register([repo["full_name"] for repo in repos])

        snapshot_name = utc_stamp(self.snapshot_fmt)
        snapshot_dir = self.output_dir / snapshot_name
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        rmtree(self.work_root)

        incremental = self.cfg.get("runtime.incremental", False) and not self.force_full
        pending: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for repo in repos:
            if incremental and not self._repo_changed(repo, state):
                previous = (state.get("repos") or {}).get(repo["full_name"], {})
                results.append(
                    {
                        "repo": repo["full_name"],
                        "status": "unchanged",
                        "in_snapshot": previous.get("snapshot"),
                        "warnings": [],
                    }
                )
                log.info("%s unchanged since %s; skipping", repo["full_name"], previous.get("snapshot"))
            else:
                pending.append(repo)

        if self.dry_run:
            log.info("dry run: would back up %d repositories", len(pending))
            for repo in pending:
                log.info("  - %s", repo["full_name"])
            rmtree(snapshot_dir)
            return {
                "dry_run": True,
                "selected": [r["full_name"] for r in repos],
                "would_export": [r["full_name"] for r in pending],
            }

        failures: list[dict[str, Any]] = []
        workers = max(1, int(self.cfg.get("runtime.concurrency", 4)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self.backup_repo, repo, snapshot_dir, snapshot_name): repo
                for repo in pending
            }
            for index, future in enumerate(as_completed(futures), start=1):
                repo = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    log.info(
                        "[%d/%d] %s done in %ss%s",
                        index,
                        len(futures),
                        repo["full_name"],
                        result.get("seconds"),
                        f" ({human_size(result['bytes'])})" if result.get("bytes") else "",
                    )
                except Exception as exc:  # keep going; report at the end
                    log.error("[%d/%d] %s FAILED: %s", index, len(futures), repo["full_name"], exc)
                    failure = {"repo": repo["full_name"], "status": "failed", "error": str(exc)[:800]}
                    results.append(failure)
                    failures.append(failure)
                    if self.cfg.get("runtime.fail_fast", False):
                        raise

        account = None
        if any(self.cfg.wants(flag) for flag in ("account_profile", "account_gists", "account_starred", "account_following")):
            try:
                account = export_account(self.client, snapshot_dir / "account", self.cfg)
                if self.cfg.get("output.archive", True):
                    account_archive = archive_dir(
                        snapshot_dir / "account",
                        snapshot_dir,
                        "account",
                        compression=self.cfg.get("output.compression", "gz"),
                    )
                    if self.stream_upload:
                        self._stream(
                            account_archive, snapshot_dir, snapshot_name, account_archive.name
                        )
            except Exception as exc:
                log.error("account export failed: %s", exc)
                failures.append({"repo": "(account)", "status": "failed", "error": str(exc)[:800]})

        manifest = {
            "tool": "checkpoint",
            "schema": 1,
            "snapshot": snapshot_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "incremental": incremental,
            "stream_upload": self.stream_upload,
            "config_source": str(self.cfg.source) if self.cfg.source else "(defaults)",
            "collect": self.cfg.collect,
            "repo_count": len(results),
            "failed": len(failures),
            "warnings": run_warnings,
            "account": account,
            # 로그에서 가린 이름을 나중에 되짚을 수 있도록 대응표를 남깁니다.
            # manifest 는 Drive 로만 가고 로그에는 찍히지 않습니다.
            "log_aliases": self.redactor.aliases or None,
            "repos": sorted(results, key=lambda r: r["repo"].lower()),
        }
        write_manifest(snapshot_dir, manifest, uploaded=self._uploaded)
        rmtree(self.work_root)

        remote_uri = None
        if self.backend.name != "none":
            # streaming 모드에서는 manifest.json 과 SHA256SUMS 만 남아 있습니다.
            # manifest.json 의 존재 여부가 곧 "완성된 스냅샷" 표시입니다.
            remote_uri = self.backend.upload(snapshot_dir, snapshot_name)
            log.info("uploaded snapshot to %s", remote_uri)

        # Only record success for repos that actually completed.
        repo_state = dict(state.get("repos") or {})
        by_name = {r["full_name"]: r for r in repos}
        for result in results:
            if result["status"] == "exported":
                repo = by_name.get(result["repo"], {})
                repo_state[result["repo"]] = {
                    "pushed_at": repo.get("pushed_at"),
                    "updated_at": repo.get("updated_at"),
                    "snapshot": snapshot_name,
                }
        state.update(
            {
                "last_snapshot": snapshot_name,
                "last_run": manifest["created_at"],
                "repos": repo_state,
            }
        )
        self.save_state(state)

        self.prune(snapshot_name)

        if not self.cfg.get("output.keep_local", True) and self.backend.name != "none" and not failures:
            rmtree(snapshot_dir)
            log.info("removed local snapshot copy (output.keep_local: false)")

        manifest["remote"] = remote_uri
        manifest["seconds"] = round(time.time() - started, 1)
        log.info(
            "snapshot %s complete: %d repos, %d failed, %s in %.0fs",
            snapshot_name,
            len(results),
            len(failures),
            manifest.get("size_human"),
            manifest["seconds"],
        )
        return manifest

    # -- retention ------------------------------------------------------
    def _read_manifest(self, snapshot: str) -> dict[str, Any] | None:
        local = self.output_dir / snapshot / "manifest.json"
        if local.is_file():
            return read_json(local)
        if self.backend.name == "none":
            return None
        tmp = self.work_root / "manifests" / f"{safe_name(snapshot)}.json"
        if self.backend.download(f"{snapshot}/manifest.json", tmp):
            return read_json(tmp)
        return None

    def _referenced_snapshots(self, survivors: list[str]) -> set[str]:
        """Older snapshots that surviving incremental snapshots still depend on.

        With ``runtime.incremental`` an unchanged repository is recorded as a
        pointer to the snapshot that actually holds it. Deleting that snapshot
        on age alone would silently orphan the data, so those targets are
        protected from pruning.
        """
        if not self.cfg.get("runtime.incremental", False):
            return set()

        referenced: set[str] = set()
        pending, seen = list(survivors), set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            manifest = self._read_manifest(name)
            if not manifest:
                continue
            for entry in manifest.get("repos") or []:
                target = entry.get("in_snapshot")
                if target and target not in referenced:
                    referenced.add(target)
                    pending.append(target)  # 참조가 이어질 경우까지 따라갑니다
        return referenced

    def prune(self, current: str) -> None:
        rules = {
            "keep_last": int(self.cfg.get("retention.keep_last", 0) or 0),
            "keep_daily": int(self.cfg.get("retention.keep_daily", 0) or 0),
            "keep_weekly": int(self.cfg.get("retention.keep_weekly", 0) or 0),
            "keep_monthly": int(self.cfg.get("retention.keep_monthly", 0) or 0),
        }
        if not any(rules.values()):
            return

        if self.cfg.get("retention.prune_remote", True) and self.backend.name != "none":
            names = self.backend.list_snapshots("")
            expired = select_expired(names, self.snapshot_fmt, **rules)
            protected = self._referenced_snapshots([n for n in names if n not in expired])
            for name in expired:
                if name == current:
                    continue
                if name in protected:
                    log.info("keeping remote snapshot %s: still referenced by a later one", name)
                    continue
                log.info("pruning remote snapshot %s", name)
                if not self.dry_run:
                    self.backend.delete(name)

        if self.cfg.get("retention.prune_local", True) and self.output_dir.is_dir():
            names = [p.name for p in self.output_dir.iterdir()]
            expired = select_expired(names, self.snapshot_fmt, **rules)
            protected = self._referenced_snapshots([n for n in names if n not in expired])
            for name in expired:
                if name in protected:
                    log.info("keeping local snapshot %s: still referenced by a later one", name)
                    continue
                if name == current:
                    continue
                log.info("pruning local snapshot %s", name)
                if not self.dry_run:
                    rmtree(self.output_dir / name)
