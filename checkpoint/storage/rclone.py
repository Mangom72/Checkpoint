"""Google Drive (and any other rclone remote) backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..util import CommandError, run, which
from .base import StorageBackend

log = logging.getLogger(__name__)


class RcloneBackend(StorageBackend):
    name = "rclone"

    def __init__(self, cfg) -> None:
        rc = cfg.get("storage.rclone", {}) or {}
        self.binary = rc.get("binary") or "rclone"
        self.remote = (rc.get("remote") or "").rstrip(":") + ":"
        self.base_path = (rc.get("path") or "").strip("/")
        self.extra_args = list(rc.get("extra_args") or [])

    # -- helpers --------------------------------------------------------
    def _target(self, remote_subpath: str) -> str:
        parts = [p for p in (self.base_path, remote_subpath.strip("/")) if p]
        return f"{self.remote}{'/'.join(parts)}"

    def _run(self, args: list[str], *, check: bool = True, timeout: int | None = None):
        cmd = [self.binary, *args, "--stats-one-line", "--stats=30s", *self.extra_args]
        return run(cmd, check=check, timeout=timeout)

    # -- interface ------------------------------------------------------
    def check(self) -> None:
        if not which(self.binary):
            raise RuntimeError(
                f"rclone binary '{self.binary}' not found. Install it "
                "(https://rclone.org/install/) or set storage.backend: local."
            )
        proc = run([self.binary, "listremotes"], check=False)
        remotes = {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}
        if remotes and self.remote not in remotes:
            raise RuntimeError(
                f"rclone remote '{self.remote}' is not configured. Known remotes: "
                f"{', '.join(sorted(remotes)) or '(none)'}"
            )

    def upload(self, local_path: Path, remote_subpath: str) -> str:
        target = self._target(remote_subpath)
        verb = "copyto" if local_path.is_file() else "copy"
        log.info("rclone %s -> %s", verb, target)
        self._run([verb, str(local_path), target, "--progress=false"], timeout=None)
        return target

    def list_snapshots(self, remote_subpath: str) -> list[str]:
        target = self._target(remote_subpath)
        proc = self._run(["lsjson", "--max-depth=1", target], check=False)
        if proc.returncode != 0:
            return []
        try:
            entries = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return sorted(entry["Name"] for entry in entries)

    def download(self, remote_subpath: str, local_path: Path) -> bool:
        target = self._target(remote_subpath)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run(["copyto", target, str(local_path)], check=False)
        return proc.returncode == 0 and local_path.exists()

    def delete(self, remote_subpath: str) -> None:
        target = self._target(remote_subpath)
        log.info("rclone purge %s", target)
        try:
            self._run(["purge", target])
        except CommandError:
            self._run(["deletefile", target], check=False)
