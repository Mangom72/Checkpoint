"""Git history export: mirror clone -> bundle (+ wiki, LFS, ref listing)."""

from __future__ import annotations

import base64
import logging
import tarfile
from pathlib import Path
from typing import Any

from ..util import CommandError, human_size, run, rmtree

log = logging.getLogger(__name__)


def _auth_env(token: str, host: str = "https://github.com/") -> dict[str, str]:
    """Authenticate git over HTTPS without putting the token in argv."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{host}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _list_refs(mirror: Path, env: dict[str, str], token: str) -> list[str]:
    proc = run(
        ["git", "for-each-ref", "--format=%(objectname) %(refname)"],
        cwd=mirror,
        env=env,
        check=False,
        secrets=[token],
    )
    return [line for line in (proc.stdout or "").splitlines() if line.strip()]


def export_git(
    *,
    clone_url: str,
    dest: Path,
    token: str,
    work_dir: Path,
    pull_refs: bool = False,
    lfs: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Mirror ``clone_url`` and write a restorable bundle into ``dest``.

    Returns a summary dict; raises only on unexpected failures (an empty
    repository is reported, not raised).
    """
    dest.mkdir(parents=True, exist_ok=True)
    mirror = work_dir / "mirror.git"
    rmtree(mirror)
    mirror.parent.mkdir(parents=True, exist_ok=True)

    env = _auth_env(token)
    summary: dict[str, Any] = {"bundle": None, "refs": 0, "empty": False, "lfs": False}

    run(
        ["git", "clone", "--mirror", "--quiet", clone_url, str(mirror)],
        env=env,
        timeout=timeout,
        secrets=[token],
    )

    if pull_refs:
        # Pull request head/merge refs are not advertised by default.
        proc = run(
            ["git", "fetch", "--quiet", "origin", "+refs/pull/*:refs/pull/*"],
            cwd=mirror,
            env=env,
            check=False,
            timeout=timeout,
            secrets=[token],
        )
        if proc.returncode != 0:
            log.warning("could not fetch pull refs for %s", clone_url.split("@")[-1])

    refs = _list_refs(mirror, env, token)
    summary["refs"] = len(refs)
    (dest / "refs.txt").write_text("\n".join(refs) + ("\n" if refs else ""), encoding="utf-8")

    head = run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=mirror, env=env, check=False, secrets=[token]
    ).stdout.strip()
    if head:
        (dest / "HEAD").write_text(head + "\n", encoding="utf-8")

    if not refs:
        summary["empty"] = True
        log.info("repository has no refs (empty); skipping bundle")
        rmtree(mirror)
        return summary

    if lfs:
        proc = run(
            ["git", "lfs", "fetch", "--all"],
            cwd=mirror,
            env=env,
            check=False,
            timeout=timeout,
            secrets=[token],
        )
        lfs_dir = mirror / "lfs"
        if proc.returncode == 0 and lfs_dir.is_dir():
            archive = dest / "lfs-objects.tar"
            with tarfile.open(archive, "w") as tar:
                tar.add(lfs_dir, arcname="lfs")
            summary["lfs"] = True
            summary["lfs_bytes"] = archive.stat().st_size
        elif proc.returncode != 0:
            log.warning("git lfs fetch failed (is git-lfs installed?)")

    bundle = dest / "repo.bundle"
    try:
        run(
            ["git", "bundle", "create", str(bundle), "--all"],
            cwd=mirror,
            env=env,
            timeout=timeout,
            secrets=[token],
        )
    except CommandError as exc:
        if "empty bundle" in str(exc).lower():
            summary["empty"] = True
            rmtree(mirror)
            return summary
        raise

    summary["bundle"] = bundle.name
    summary["bundle_bytes"] = bundle.stat().st_size
    log.info("git bundle %s (%s refs)", human_size(summary["bundle_bytes"]), len(refs))
    rmtree(mirror)
    return summary


def export_wiki(
    *, wiki_url: str, dest: Path, token: str, work_dir: Path, timeout: int = 900
) -> dict[str, Any] | None:
    """Mirror the repository wiki if one exists. Returns ``None`` when absent."""
    mirror = work_dir / "wiki.git"
    rmtree(mirror)
    env = _auth_env(token)
    proc = run(
        ["git", "clone", "--mirror", "--quiet", wiki_url, str(mirror)],
        env=env,
        check=False,
        timeout=timeout,
        secrets=[token],
    )
    if proc.returncode != 0:
        rmtree(mirror)
        return None

    dest.mkdir(parents=True, exist_ok=True)
    bundle = dest / "wiki.bundle"
    result = run(
        ["git", "bundle", "create", str(bundle), "--all"],
        cwd=mirror,
        env=env,
        check=False,
        timeout=timeout,
        secrets=[token],
    )
    rmtree(mirror)
    if result.returncode != 0:
        return None
    return {"bundle": bundle.name, "bytes": bundle.stat().st_size}
