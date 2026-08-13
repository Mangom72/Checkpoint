import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.retention import parse_snapshots, select_expired

FMT = "%Y-%m-%dT%H-%M-%SZ"


def names(*stamps):
    return list(stamps)


def test_no_rules_keeps_everything():
    snaps = names("2026-01-01T00-00-00Z", "2026-01-02T00-00-00Z")
    assert select_expired(snaps, FMT) == []


def test_keep_last_only():
    snaps = [f"2026-01-0{d}T00-00-00Z" for d in range(1, 6)]
    expired = select_expired(snaps, FMT, keep_last=2)
    assert sorted(expired) == ["2026-01-01T00-00-00Z", "2026-01-02T00-00-00Z", "2026-01-03T00-00-00Z"]


def test_daily_keeps_newest_per_day():
    snaps = [
        "2026-03-01T01-00-00Z",
        "2026-03-01T22-00-00Z",
        "2026-03-02T05-00-00Z",
        "2026-03-02T23-00-00Z",
    ]
    expired = select_expired(snaps, FMT, keep_daily=2)
    assert sorted(expired) == ["2026-03-01T01-00-00Z", "2026-03-02T05-00-00Z"]


def test_monthly_retains_older_generations():
    snaps = [
        "2026-01-15T00-00-00Z",
        "2026-02-15T00-00-00Z",
        "2026-03-15T00-00-00Z",
        "2026-03-20T00-00-00Z",
    ]
    expired = select_expired(snaps, FMT, keep_last=1, keep_monthly=3)
    # newest per month for 3 months + newest overall are kept
    assert expired == ["2026-03-15T00-00-00Z"]


def test_unparseable_names_are_never_deleted():
    snaps = ["state.json", "notes", "2026-01-01T00-00-00Z", "2026-01-02T00-00-00Z"]
    expired = select_expired(snaps, FMT, keep_last=1)
    assert expired == ["2026-01-01T00-00-00Z"]


def test_archive_suffixes_are_stripped_when_parsing():
    parsed = parse_snapshots(["2026-01-01T00-00-00Z.tar.gz"], FMT)
    assert len(parsed) == 1
