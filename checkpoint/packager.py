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


def write_manifest(
    snapshot_dir: Path,
    manifest: dict[str, Any],
    *,
    uploaded: dict[str, tuple[str, int]] | None = None,
) -> Path:
    """Write manifest.json plus a SHA256SUMS file covering every snapshot file.

    ``uploaded`` carries entries for files that were streamed to the remote and
    deleted locally, as ``{relative_path: (sha256, bytes)}``; they are hashed at
    upload time so the checksum file stays complete.
    """
    entries: dict[str, str] = dict((path, sha) for path, (sha, _size) in (uploaded or {}).items())
    total = sum(size for _sha, size in (uploaded or {}).values())

    for path in sorted(snapshot_dir.rglob("*")):
        if path.is_file() and path.name not in ("SHA256SUMS", "manifest.json"):
            relative = str(path.relative_to(snapshot_dir))
            entries[relative] = sha256_file(path)
            total += path.stat().st_size

    lines = [f"{sha}  {relative}" for relative, sha in sorted(entries.items())]
    (snapshot_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest["files"] = len(lines)
    manifest["bytes"] = total
    manifest["size_human"] = human_size(total)
    return write_json(snapshot_dir / "manifest.json", manifest)
