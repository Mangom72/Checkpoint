"""Local/mounted-filesystem backend (also handy for testing)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .base import StorageBackend

log = logging.getLogger(__name__)


class LocalBackend(StorageBackend):
    name = "local"

    def __init__(self, cfg) -> None:
        self.root = Path(cfg.get("storage.local.path", "./remote-backups")).expanduser()

    def _target(self, remote_subpath: str) -> Path:
        return self.root / remote_subpath.strip("/")

    def check(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, local_path: Path, remote_subpath: str) -> str:
        target = self._target(remote_subpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        if local_path.is_dir():
            shutil.copytree(local_path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(local_path, target)
        log.info("copied -> %s", target)
        return str(target)

    def list_snapshots(self, remote_subpath: str) -> list[str]:
        target = self._target(remote_subpath)
        if not target.is_dir():
            return []
        return sorted(p.name for p in target.iterdir())

    def download(self, remote_subpath: str, local_path: Path) -> bool:
        source = self._target(remote_subpath)
        if not source.is_file():
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, local_path)
        return True

    def delete(self, remote_subpath: str) -> None:
        target = self._target(remote_subpath)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
