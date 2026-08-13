from .base import NullBackend, StorageBackend
from .local import LocalBackend
from .rclone import RcloneBackend


def build_backend(cfg) -> StorageBackend:
    backend = cfg.get("storage.backend", "rclone")
    if backend == "rclone":
        return RcloneBackend(cfg)
    if backend == "local":
        return LocalBackend(cfg)
    return NullBackend()


__all__ = ["StorageBackend", "NullBackend", "LocalBackend", "RcloneBackend", "build_backend"]
