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
from .exporters.git_repo import reachable_predicate
from .github_client import GitHubClient
from .mirror import MIRROR_ROOT, lost_commits, merge_repo_export, now_iso, vanished_repos
from .packager import archive_dir, write_manifest
from .redaction import RepoNameRedactor
from .retention import select_expired
from .storage import build_backend
from .util import (
    dir_size,
    human_size,
    read_json,
    rmtree,
    safe_name,
    sha256_file,
    utc_stamp,
    write_json,
)

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
        # 절대 경로로 고정합니다. git 하위 명령은 미러 클론 디렉터리를 cwd 로 두고
        # 실행되는데, 여기 경로가 상대 경로면 그 cwd 기준으로 다시 해석되어
        # (예: <mirror>/backups/mirror/...) 존재하지 않는 경로가 만들어집니다.
        self.output_dir = Path(cfg.get("output.dir", "./backups")).expanduser().resolve()
        self.work_root = Path(cfg.get("output.work_dir", "./work")).expanduser().resolve()
        self.state_path = Path(cfg.get("runtime.state_file", ".checkpoint-state.json")).expanduser()
        self.snapshot_fmt = cfg.get("output.snapshot_name", "%Y-%m-%dT%H-%M-%SZ")
        self.mirror = cfg.get("output.mode", "snapshot") == "mirror"
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
            if self.mirror:
                self._mirror_repo(repo, slug, repo_dir, work_dir, result)
            else:
                self._export_repo(repo, repo_dir, work_dir, result)
        finally:
            rmtree(work_dir)

        if self.mirror:
            # 미러는 파일 단위로 덮어써야 하므로 압축하지 않고 그대로 올립니다.
            # rclone copy 는 원격에서 아무것도 지우지 않습니다.
            result["bytes"] = dir_size(repo_dir)
            # 로컬 사본을 지우기 전에 체크섬을 기록해 둡니다.
            self._record_checksums(repo_dir, f"repos/{slug}")
            self.backend.upload(repo_dir, f"{MIRROR_ROOT}/repos/{slug}")
            if not self.cfg.get("output.keep_local", True):
                rmtree(repo_dir)
        elif self.cfg.get("output.archive", True):
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

    def _export_repo(
        self,
        repo: dict[str, Any],
        dest: Path,
        work_dir: Path,
        result: dict[str, Any],
        *,
        keep_mirror: bool = False,
    ) -> None:
        """Write one repository's git bundle, wiki and API dump into ``dest``."""
        full_name = repo["full_name"]
        if self.cfg.wants("git"):
            clone_url = repo.get("clone_url") or f"https://github.com/{full_name}.git"
            result["git"] = export_git(
                clone_url=clone_url,
                dest=dest / "git",
                token=self.token,
                work_dir=work_dir,
                pull_refs=self.cfg.wants("git_pull_refs"),
                lfs=self.cfg.wants("git_lfs"),
                keep_mirror=keep_mirror,
            )

        if self.cfg.wants("wiki") and repo.get("has_wiki"):
            wiki = export_wiki(
                wiki_url=f"https://github.com/{full_name}.wiki.git",
                dest=dest / "git",
                token=self.token,
                work_dir=work_dir,
            )
            result["wiki"] = wiki or "none"

        exporter = RepoApiExporter(self.client, repo, dest / "api", self.cfg)
        api_result = exporter.run(self.token)
        result["counts"] = api_result["counts"]
        result["warnings"].extend(api_result["warnings"])

    def _mirror_repo(
        self,
        repo: dict[str, Any],
        slug: str,
        dest: Path,
        work_dir: Path,
        result: dict[str, Any],
    ) -> None:
        """Refresh this repository's mirror, keeping whatever GitHub dropped."""
        remote = f"{MIRROR_ROOT}/repos/{slug}"
        when = now_iso()

        previous_api = work_dir / "previous-api"
        self.backend.download_dir(f"{remote}/api", previous_api)
        previous_refs = work_dir / "previous-refs.txt"
        had_refs = self.backend.download(f"{remote}/git/refs.txt", previous_refs)

        self._export_repo(repo, dest, work_dir, result, keep_mirror=True)

        # 강제 푸시나 브랜치 삭제로 더 이상 닿을 수 없게 된 커밋을 지켜냅니다.
        mirror_path = (result.get("git") or {}).get("mirror_path")
        if had_refs and mirror_path:
            lost = lost_commits(previous_refs, reachable_predicate(Path(mirror_path)))
            if lost:
                self._park_lost_history(remote, dest, lost, when, result)
        if mirror_path:
            rmtree(Path(mirror_path))

        vanished = merge_repo_export(previous_api, dest / "api", when)
        if vanished:
            result["vanished_entries"] = vanished

    def _park_lost_history(
        self,
        remote: str,
        dest: Path,
        lost: list[str],
        when: str,
        result: dict[str, Any],
    ) -> None:
        """Keep the previous bundle when history became unreachable upstream."""
        history = dest / "git" / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = when.replace(":", "-").split(".")[0]
        parked = history / f"{stamp}.bundle"
        if self.backend.download(f"{remote}/git/repo.bundle", parked):
            (history / f"{stamp}.refs.txt").write_text("\n".join(lost) + "\n", encoding="utf-8")
            result["parked_history"] = {"bundle": parked.name, "unreachable_refs": len(lost)}
            log.warning(
                "%s: %d ref(s) no longer reachable upstream; previous bundle kept as %s",
                result["repo"],
                len(lost),
                parked.name,
            )
        else:
            result["warnings"].append(
                f"history rewritten upstream ({len(lost)} refs) but the previous bundle "
                "could not be fetched to preserve it"
            )

    def _record_checksums(self, directory: Path, prefix: str) -> None:
        """Hash every file under ``directory`` so SHA256SUMS survives deletion."""
        entries: dict[str, tuple[str, int]] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative = f"{prefix}/{path.relative_to(directory)}"
                entries[relative] = (sha256_file(path), path.stat().st_size)
        with self._uploaded_lock:
            self._uploaded.update(entries)

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

        if self.mirror:
            # 미러는 이름이 고정된 단일 사본입니다. 타임스탬프 디렉터리가 없습니다.
            snapshot_name = MIRROR_ROOT
            snapshot_dir = self.output_dir / MIRROR_ROOT
            previous_manifest = self._read_mirror_manifest()
        else:
            snapshot_name = utc_stamp(self.snapshot_fmt)
            snapshot_dir = self.output_dir / snapshot_name
            previous_manifest = None
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        rmtree(self.work_root)

        incremental = self.cfg.get("runtime.incremental", False) and not self.force_full
        pending: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for repo in repos:
            if incremental and not self._repo_changed(repo, state):
                previous = (state.get("repos") or {}).get(repo["full_name"], {})
                entry = {"repo": repo["full_name"], "status": "unchanged", "warnings": []}
                if not self.mirror:
                    # 스냅샷 모드에서는 실제 데이터를 가진 이전 스냅샷을 가리켜야 합니다.
                    # 미러 모드에서는 사본이 이미 제자리에 있으므로 포인터가 필요 없습니다.
                    entry["in_snapshot"] = previous.get("snapshot")
                results.append(entry)
                log.info(
                    "%s unchanged; already in the mirror" if self.mirror
                    else "%s unchanged since %s; skipping",
                    repo["full_name"],
                    *([] if self.mirror else [previous.get("snapshot")]),
                )
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
                if self.mirror:
                    self._record_checksums(snapshot_dir / "account", "account")
                    self.backend.upload(snapshot_dir / "account", f"{MIRROR_ROOT}/account")
                elif self.cfg.get("output.archive", True):
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

        if self.mirror:
            present = {repo["full_name"] for repo in repos}
            gone = vanished_repos(previous_manifest, present, now_iso())
            for entry in gone:
                log.warning("%s is no longer on GitHub; its mirror is kept", entry["repo"])
            results.extend(gone)

        manifest = {
            "tool": "checkpoint",
            "schema": 1,
            "mode": "mirror" if self.mirror else "snapshot",
            "snapshot": snapshot_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
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
            if self.mirror:
                # 레포별 디렉터리는 이미 올라갔습니다. 남은 것은 manifest 와 체크섬입니다.
                for name in ("manifest.json", "SHA256SUMS"):
                    if (snapshot_dir / name).is_file():
                        self.backend.upload(snapshot_dir / name, f"{MIRROR_ROOT}/{name}")
                remote_uri = f"{MIRROR_ROOT}/"
                log.info("mirror updated at %s", remote_uri)
            else:
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

        if not self.mirror:
            self.prune(snapshot_name)

        if not self.cfg.get("output.keep_local", True) and self.backend.name != "none" and not failures:
            rmtree(snapshot_dir)
            log.info("removed local snapshot copy (output.keep_local: false)")

        manifest["remote"] = remote_uri
        manifest["seconds"] = round(time.time() - started, 1)
        log.info(
            "%s complete: %d repos, %d failed, %s in %.0fs",
            "mirror" if self.mirror else f"snapshot {snapshot_name}",
            len(results),
            len(failures),
            manifest.get("size_human"),
            manifest["seconds"],
        )
        return manifest

    def _read_mirror_manifest(self) -> dict[str, Any] | None:
        local = self.output_dir / MIRROR_ROOT / "manifest.json"
        if local.is_file():
            return read_json(local)
        if self.backend.name == "none":
            return None
        tmp = self.work_root / "previous-mirror-manifest.json"
        if self.backend.download(f"{MIRROR_ROOT}/manifest.json", tmp):
            return read_json(tmp)
        return None

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
