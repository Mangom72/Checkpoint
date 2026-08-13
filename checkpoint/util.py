"""Small shared helpers: JSON writing, subprocess wrapper, hashing, sizes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """Turn ``owner/repo`` into a filesystem-safe component."""
    cleaned = _UNSAFE.sub("_", name.replace("/", "__")).strip("._")
    return cleaned or "unnamed"


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    tmp.replace(path)
    return path


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], code: int, output: str):
        super().__init__(f"command failed ({code}): {' '.join(cmd)}\n{output.strip()[:2000]}")
        self.cmd = list(cmd)
        self.code = code
        self.output = output


def run(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = None,
    secrets: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output and scrubbing secrets from any error text."""
    full_env = {**os.environ, **(env or {})}
    printable = " ".join(redact(part, secrets) for part in cmd)
    log.debug("run: %s", printable)
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        output = redact((proc.stdout or "") + (proc.stderr or ""), secrets)
        raise CommandError([redact(c, secrets) for c in cmd], proc.returncode, output)
    return proc


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return re.sub(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})", "***", out)


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TiB"


def which(binary: str) -> str | None:
    return shutil.which(binary)


def utc_stamp(fmt: str) -> str:
    return time.strftime(fmt, time.gmtime())


def rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
