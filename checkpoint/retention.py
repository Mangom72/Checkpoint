"""Grandfather-father-son pruning of snapshot directories."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

log = logging.getLogger(__name__)


def parse_snapshots(names: Iterable[str], fmt: str) -> list[tuple[str, datetime]]:
    parsed: list[tuple[str, datetime]] = []
    for name in names:
        stem = name
        for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        try:
            parsed.append((name, datetime.strptime(stem, fmt)))
        except ValueError:
            log.debug("ignoring unrecognised snapshot name: %s", name)
    parsed.sort(key=lambda item: item[1], reverse=True)
    return parsed


def select_expired(
    names: Iterable[str],
    fmt: str,
    *,
    keep_last: int = 0,
    keep_daily: int = 0,
    keep_weekly: int = 0,
    keep_monthly: int = 0,
) -> list[str]:
    """Return snapshot names that no retention rule wants to keep.

    Snapshots whose names do not parse as timestamps are never deleted.
    """
    snapshots = parse_snapshots(names, fmt)
    if not snapshots:
        return []

    keep: set[str] = set()
    for name, _ in snapshots[:keep_last]:
        keep.add(name)

    def keep_newest_per(bucket_of, count: int) -> None:
        if count <= 0:
            return
        seen: set = set()
        for name, moment in snapshots:  # already newest-first
            bucket = bucket_of(moment)
            if bucket in seen:
                continue
            seen.add(bucket)
            keep.add(name)
            if len(seen) >= count:
                return

    keep_newest_per(lambda m: m.date(), keep_daily)
    keep_newest_per(lambda m: m.isocalendar()[:2], keep_weekly)
    keep_newest_per(lambda m: (m.year, m.month), keep_monthly)

    if not (keep_last or keep_daily or keep_weekly or keep_monthly):
        return []  # no rules configured -> keep everything

    return [name for name, _ in snapshots if name not in keep]
