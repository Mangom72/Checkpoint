"""Turn an exported repository directory into a compressed archive + checksums."""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path
from typing import Any

from .util import dir_size, human_size, rmtree, sha256_file, write_json

log = logging.getLogger(__name__)

_MODES = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz", "none": "w"}
_SUFFIXES = {"gz": ".tar.gz", "bz2": ".tar.bz2", "xz": ".tar.xz", "none": ".tar"}


def archive_dir(src: Path, out_dir: Path, name: str, compression: str = "gz") -> Path:
    """Create ``out_dir/<name><suffix>`` from ``src`` and remove the source tree."""
    mode = _MODES.get(compression, "w:gz")
    suffix = _SUFFIXES.get(compression, ".tar.gz")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{name}{suffix}"
    raw = dir_size(src)
    with tarfile.open(archive, mode) as tar:
        tar.add(src, arcname=name)
    size = archive.stat().st_size
    log.info("archived %s: %s -> %s", name, human_size(raw), human_size(size))
    rmtree(src)
    return archive


def write_manifest(snapshot_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write manifest.json plus a SHA256SUMS file covering every snapshot file."""
    checksums: list[str] = []
    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file() and path.name not in ("SHA256SUMS", "manifest.json"):
            checksums.append(f"{sha256_file(path)}  {path.relative_to(snapshot_dir)}")
    (snapshot_dir / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    manifest["files"] = len(checksums)
    manifest["bytes"] = dir_size(snapshot_dir)
    manifest["size_human"] = human_size(manifest["bytes"])
    return write_json(snapshot_dir / "manifest.json", manifest)
