"""incremental 모드에서 보존 규칙이 참조 중인 스냅샷을 지우지 않는지 검증."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.backup import BackupRunner
from checkpoint.config import Config

OLD, NEW = "2026-01-01T00-00-00Z", "2026-06-01T00-00-00Z"


def build(tmp_path, incremental: bool) -> BackupRunner:
    """OLD 는 실제 데이터를, NEW 는 OLD 를 가리키기만 하는 레이아웃을 만듭니다."""
    for root in (tmp_path / "backups", tmp_path / "remote"):
        (root / OLD).mkdir(parents=True)
        (root / NEW).mkdir(parents=True)
        (root / OLD / "manifest.json").write_text(
            json.dumps({"snapshot": OLD, "repos": [{"repo": "me/app", "status": "exported"}]})
        )
        (root / NEW / "manifest.json").write_text(
            json.dumps(
                {
                    "snapshot": NEW,
                    "repos": [{"repo": "me/app", "status": "unchanged", "in_snapshot": OLD}],
                }
            )
        )

    cfg = Config.load(None)
    cfg.data["output"].update({"dir": str(tmp_path / "backups"), "work_dir": str(tmp_path / "work")})
    cfg.data["storage"] = {"backend": "local", "local": {"path": str(tmp_path / "remote")}}
    cfg.data["runtime"].update(
        {"incremental": incremental, "state_file": str(tmp_path / "s.json"), "log_level": "WARNING"}
    )
    cfg.data["retention"] = {
        "keep_last": 1,  # NEW 만 남기는 규칙 -> OLD 는 만료 대상
        "keep_daily": 0,
        "keep_weekly": 0,
        "keep_monthly": 0,
        "prune_remote": True,
        "prune_local": True,
    }
    return BackupRunner(cfg)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")


def test_referenced_snapshot_survives_pruning(tmp_path):
    runner = build(tmp_path, incremental=True)
    runner.prune(current=NEW)

    assert (tmp_path / "backups" / OLD).is_dir(), "참조 중인 로컬 스냅샷이 삭제됨"
    assert (tmp_path / "remote" / OLD).is_dir(), "참조 중인 원격 스냅샷이 삭제됨"
    assert (tmp_path / "backups" / NEW).is_dir()


def test_unreferenced_snapshot_is_still_pruned(tmp_path):
    runner = build(tmp_path, incremental=True)
    # NEW 가 더 이상 OLD 를 참조하지 않도록 바꿉니다.
    for root in (tmp_path / "backups", tmp_path / "remote"):
        (root / NEW / "manifest.json").write_text(
            json.dumps({"snapshot": NEW, "repos": [{"repo": "me/app", "status": "exported"}]})
        )
    runner.prune(current=NEW)

    assert not (tmp_path / "backups" / OLD).exists(), "참조가 없으면 정상적으로 지워져야 함"
    assert not (tmp_path / "remote" / OLD).exists()


def test_non_incremental_runs_skip_the_reference_check(tmp_path):
    """증분을 안 쓰면 참조 자체가 없으므로 manifest 를 읽지 않고 그냥 지웁니다."""
    runner = build(tmp_path, incremental=False)
    assert runner._referenced_snapshots([NEW]) == set()
    runner.prune(current=NEW)
    assert not (tmp_path / "backups" / OLD).exists()


def test_reference_chains_are_followed(tmp_path):
    runner = build(tmp_path, incremental=True)
    older = "2025-01-01T00-00-00Z"
    for root in (tmp_path / "backups", tmp_path / "remote"):
        (root / older).mkdir(parents=True)
        (root / older / "manifest.json").write_text(
            json.dumps({"snapshot": older, "repos": [{"repo": "me/b", "status": "exported"}]})
        )
        # OLD 가 다시 older 를 참조하도록 연결
        (root / OLD / "manifest.json").write_text(
            json.dumps(
                {
                    "snapshot": OLD,
                    "repos": [
                        {"repo": "me/app", "status": "exported"},
                        {"repo": "me/b", "status": "unchanged", "in_snapshot": older},
                    ],
                }
            )
        )
    runner.prune(current=NEW)

    assert (tmp_path / "backups" / older).is_dir(), "간접 참조된 스냅샷도 지켜져야 함"
    assert (tmp_path / "remote" / older).is_dir()
