"""Mirror mode: one living copy that never loses what GitHub drops.

Snapshot mode keeps N independent point-in-time copies, so unchanged data is
stored over and over. Mirror mode keeps a single copy that is refreshed in
place, and anything that disappears upstream is kept and marked rather than
removed:

- a repository that vanishes keeps its directory, flagged in the manifest
- an issue/PR/release that vanishes stays in the JSON with ``_vanished_at``
- history that a force-push made unreachable is parked under ``git/history/``

Uploads are additive (rclone ``copy``, never ``sync``), so nothing on the
remote is ever deleted by a backup run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import read_json, write_json

log = logging.getLogger(__name__)

MIRROR_ROOT = "mirror"

# 컬렉션 파일별로 "같은 항목"을 판별할 키.
IDENTITY_KEYS: dict[str, str] = {
    "issues.json": "number",
    "pull_requests.json": "number",
    "discussions.json": "number",
    "releases.json": "id",
    "milestones.json": "number",
    "labels.json": "name",
    "tags.json": "name",
    "branches.json": "name",
    "comments.json": "id",
    "workflows.json": "id",
    "workflow_runs.json": "id",
    "deployments.json": "id",
    "environments.json": "id",
    "webhooks.json": "id",
    "collaborators.json": "login",
    "contributors.json": "login",
    "stargazers.json": "login",
    "watchers.json": "login",
    "forks.json": "full_name",
    "projects_v2.json": "number",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def merge_collection(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    key: str,
    when: str,
) -> tuple[list[dict[str, Any]], int]:
    """Union of both lists. Items only in ``previous`` are kept and flagged.

    Returns the merged list and how many entries are currently marked gone.
    """
    by_id: dict[Any, dict[str, Any]] = {}
    for item in previous:
        if isinstance(item, dict) and item.get(key) is not None:
            by_id[item[key]] = item

    merged: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for item in current:
        if not isinstance(item, dict):
            continue
        identity = item.get(key)
        seen.add(identity)
        earlier = by_id.get(identity)
        if earlier and earlier.get("_vanished_at"):
            # 지워진 줄 알았는데 다시 나타난 경우
            item = {**item, "_reappeared_at": when}
        merged.append(item)

    vanished = 0
    for identity, earlier in by_id.items():
        if identity in seen:
            continue
        kept = dict(earlier)
        kept.setdefault("_vanished_at", when)
        kept.pop("_reappeared_at", None)
        merged.append(kept)
        vanished += 1

    merged.sort(key=lambda entry: (entry.get("_vanished_at") is not None, str(entry.get(key))))
    return merged, vanished


def merge_repo_export(previous_api: Path, current_api: Path, when: str) -> dict[str, int]:
    """Fold a repository's previous API export into the freshly written one."""
    if not previous_api.is_dir():
        return {}

    vanished: dict[str, int] = {}
    for filename, key in IDENTITY_KEYS.items():
        old_path = previous_api / filename
        if not old_path.is_file():
            continue
        old = read_json(old_path, default=[])
        if not isinstance(old, list) or not old:
            continue

        new_path = current_api / filename
        new = read_json(new_path, default=[]) if new_path.is_file() else []
        if not isinstance(new, list):
            continue

        merged, gone = merge_collection(old, new, key, when)
        write_json(new_path, merged)
        if gone:
            vanished[filename] = gone
            log.info("%s: %d entries no longer on GitHub, kept", filename, gone)

    # 이전에만 있던 파일(수집 항목을 끈 경우 등)은 그대로 살려 둡니다.
    for old_path in previous_api.rglob("*"):
        if not old_path.is_file():
            continue
        target = current_api / old_path.relative_to(previous_api)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(old_path.read_bytes())

    return vanished


def vanished_repos(
    previous_manifest: dict[str, Any] | None, present: set[str], when: str
) -> list[dict[str, Any]]:
    """Repositories that earlier runs saw but GitHub no longer returns."""
    if not previous_manifest:
        return []
    entries: list[dict[str, Any]] = []
    for entry in previous_manifest.get("repos") or []:
        name = entry.get("repo")
        if not name or name in present:
            continue
        entries.append(
            {
                "repo": name,
                "status": "vanished",
                "vanished_at": entry.get("vanished_at") or when,
                "last_seen": entry.get("last_seen") or previous_manifest.get("updated_at"),
                "warnings": [],
            }
        )
    return entries


def lost_commits(previous_refs: Path, is_reachable) -> list[str]:
    """Old ref tips the refreshed mirror can no longer reach.

    A force-push or a deleted branch orphans commits; those are the ones worth
    parking. Everything else is already inside the new bundle.
    """
    if not previous_refs.is_file():
        return []
    lost: list[str] = []
    for line in previous_refs.read_text(encoding="utf-8").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts
        if not is_reachable(sha):
            lost.append(f"{sha} {ref}")
    return lost
