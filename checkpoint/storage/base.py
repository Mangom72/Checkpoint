"""Storage backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    name = "base"

    @abstractmethod
    def upload(self, local_path: Path, remote_subpath: str) -> str:
        """Copy ``local_path`` (file or directory) to the remote. Returns remote URI."""

    @abstractmethod
    def list_snapshots(self, remote_subpath: str) -> list[str]:
        """Return the names of existing snapshot entries under ``remote_subpath``."""

    @abstractmethod
    def delete(self, remote_subpath: str) -> None:
        """Delete a remote file or directory."""

    def download(self, remote_subpath: str, local_path: Path) -> bool:
        """Fetch a single remote file. Returns False when it does not exist."""
        return False

    def check(self) -> None:
        """Raise if the backend is not usable (missing binary, bad credentials...)."""


class NullBackend(StorageBackend):
    name = "none"

    def upload(self, local_path: Path, remote_subpath: str) -> str:
        return f"(not uploaded) {local_path}"

    def list_snapshots(self, remote_subpath: str) -> list[str]:
        return []

    def delete(self, remote_subpath: str) -> None:
        return None
