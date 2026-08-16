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
    keep_mirror: bool = False,
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
        if not keep_mirror:
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

    # git 명령은 mirror 디렉터리에서 실행되므로 출력 경로를 절대 경로로 고정합니다.
    bundle = (dest / "repo.bundle").resolve()
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
            if not keep_mirror:
                rmtree(mirror)
            return summary
        raise

    # 만든 직후 검증합니다. 코드가 유일본일 때 "올렸는데 못 여는 번들" 이 최악입니다.
    check = run(
        ["git", "bundle", "verify", str(bundle)],
        cwd=mirror,
        env=env,
        check=False,
        timeout=timeout,
        secrets=[token],
    )
    if check.returncode != 0:
        raise CommandError(
            ["git", "bundle", "verify", bundle.name],
            check.returncode,
            (check.stdout or "") + (check.stderr or ""),
        )
    summary["verified"] = True
    summary["bundle"] = bundle.name
    summary["bundle_bytes"] = bundle.stat().st_size
    log.info("git bundle %s (%s refs)", human_size(summary["bundle_bytes"]), len(refs))
    if keep_mirror:
        summary["mirror_path"] = str(mirror)
    else:
        rmtree(mirror)
    return summary


def reachable_predicate(mirror: Path):
    """Build a test for "is this old ref tip still reachable from some ref?".

    Object *existence* is the wrong question: a local clone hardlinks the object
    store, so commits orphaned by a force-push are still on disk. Only
    reachability from a current ref says whether the history survived.
    """
    listed = run(["git", "rev-list", "--all"], cwd=mirror, check=False)
    reachable = set((listed.stdout or "").split())

    def still_reachable(sha: str) -> bool:
        peeled = run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=mirror,
            check=False,
        ).stdout.strip()
        return bool(peeled) and peeled in reachable

    return still_reachable


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
