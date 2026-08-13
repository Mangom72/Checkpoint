"""미러 모드: 사본 하나만 유지하되, GitHub 에서 사라진 것은 남는다."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.backup import BackupRunner
from checkpoint.config import Config
from checkpoint.util import dir_size
from tests.fake_github import FakeGitHub, make_repo
from tests.test_integration import GIT_ENV, make_bare_repo


def base_routes(full_name, clone_url):
    owner, name = full_name.split("/")
    b = f"/repos/{owner}/{name}"
    return {
        "/user": {"login": owner},
        "/user/repos": [make_repo(full_name, clone_url, has_wiki=False)],
        f"{b}/issues": [
            {"number": 1, "title": "첫 이슈", "state": "open"},
            {"number": 2, "title": "지워질 이슈", "state": "open"},
        ],
        f"{b}/releases": [{"id": 9, "tag_name": "v1", "assets": []}],
        f"{b}/labels": [{"name": "bug"}, {"name": "지워질라벨"}],
        "__graphql__": {"repository": None},
    }


def configure(tmp_path, server, **overrides):
    cfg = Config.load(None)
    cfg.data["github"].update({"api_url": server.url, "graphql_url": f"{server.url}/graphql"})
    cfg.data["output"].update(
        {
            "mode": "mirror",
            "dir": str(tmp_path / "local"),
            "work_dir": str(tmp_path / "work"),
            "keep_local": False,
            **overrides,
        }
    )
    cfg.data["storage"] = {"backend": "local", "local": {"path": str(tmp_path / "remote")}}
    cfg.data["runtime"].update(
        {"state_file": str(tmp_path / "s.json"), "concurrency": 1, "log_level": "WARNING"}
    )
    cfg.data["collect"].update({"account_gists": False, "discussions": False})
    return cfg


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    bare = make_bare_repo(tmp_path)
    routes = base_routes("me/app", str(bare))
    with FakeGitHub(routes, page_size=50) as server:
        yield tmp_path, routes, server, bare


def mirror_api(tmp_path, filename):
    return json.loads(
        (tmp_path / "remote" / "mirror" / "repos" / "me__app" / "api" / filename).read_text()
    )


def test_mirror_writes_one_copy_with_no_timestamped_dirs(env):
    tmp_path, _routes, server, _bare = env
    manifest = BackupRunner(configure(tmp_path, server)).run()

    remote = tmp_path / "remote"
    assert manifest["mode"] == "mirror"
    assert sorted(p.name for p in remote.iterdir()) == ["mirror", "state.json"]
    assert (remote / "mirror" / "repos" / "me__app" / "git" / "repo.bundle").is_file()
    assert (remote / "mirror" / "manifest.json").is_file()


def test_repeat_runs_do_not_multiply_storage(env):
    tmp_path, _routes, server, _bare = env
    BackupRunner(configure(tmp_path, server)).run()
    after_first = dir_size(tmp_path / "remote")
    for _ in range(3):
        BackupRunner(configure(tmp_path, server)).run()
    after_fourth = dir_size(tmp_path / "remote")

    # 스냅샷 모드였다면 4배가 됐어야 합니다.
    assert after_fourth < after_first * 1.5, (after_first, after_fourth)


def test_deleted_issue_survives_in_the_mirror(env):
    tmp_path, routes, server, _bare = env
    BackupRunner(configure(tmp_path, server)).run()
    assert [i["number"] for i in mirror_api(tmp_path, "issues.json")] == [1, 2]

    # GitHub 에서 2번 이슈가 사라짐
    routes["/repos/me/app/issues"] = [{"number": 1, "title": "첫 이슈", "state": "open"}]
    BackupRunner(configure(tmp_path, server)).run()

    issues = {i["number"]: i for i in mirror_api(tmp_path, "issues.json")}
    assert set(issues) == {1, 2}, "지워진 이슈가 미러에서 사라짐"
    assert issues[2]["_vanished_at"], "사라진 시점이 기록돼야 함"
    assert "_vanished_at" not in issues[1]


def test_vanished_entry_that_returns_is_unflagged(env):
    tmp_path, routes, server, _bare = env
    original = list(routes["/repos/me/app/labels"])
    BackupRunner(configure(tmp_path, server)).run()

    routes["/repos/me/app/labels"] = [{"name": "bug"}]
    BackupRunner(configure(tmp_path, server)).run()
    labels = {l["name"]: l for l in mirror_api(tmp_path, "labels.json")}
    assert labels["지워질라벨"]["_vanished_at"]

    routes["/repos/me/app/labels"] = original
    BackupRunner(configure(tmp_path, server)).run()
    labels = {l["name"]: l for l in mirror_api(tmp_path, "labels.json")}
    assert "_vanished_at" not in labels["지워질라벨"]
    assert labels["지워질라벨"]["_reappeared_at"]


def test_deleted_repository_keeps_its_mirror(env):
    tmp_path, routes, server, _bare = env
    BackupRunner(configure(tmp_path, server)).run()

    routes["/user/repos"] = []          # 레포가 통째로 사라짐
    manifest = BackupRunner(configure(tmp_path, server)).run()

    assert (tmp_path / "remote" / "mirror" / "repos" / "me__app" / "git" / "repo.bundle").is_file()
    entry = next(r for r in manifest["repos"] if r["repo"] == "me/app")
    assert entry["status"] == "vanished"
    assert entry["vanished_at"]


def test_force_pushed_history_is_parked(env):
    tmp_path, _routes, server, bare = env
    BackupRunner(configure(tmp_path, server)).run()
    original_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True, check=True
    ).stdout.strip()

    # 히스토리를 통째로 갈아엎습니다 (force push 시뮬레이션).
    work = tmp_path / "rewrite"
    run = lambda *a: subprocess.run(a, cwd=work, check=True, capture_output=True,
                                    env={**GIT_ENV, "PATH": "/usr/bin:/bin"})
    work.mkdir()
    run("git", "init", "-q", "-b", "main")
    (work / "new.txt").write_text("완전히 다른 히스토리\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "rewritten")
    run("git", "push", "-q", "--force", str(bare), "main")
    # 태그가 남아 있으면 옛 커밋이 계속 닿으므로, 태그까지 지워야 진짜 소실입니다.
    run("git", "push", "-q", str(bare), ":refs/tags/v1.0.0")

    manifest = BackupRunner(configure(tmp_path, server)).run()

    history = tmp_path / "remote" / "mirror" / "repos" / "me__app" / "git" / "history"
    bundles = sorted(history.glob("*.bundle"))
    assert bundles, "닿을 수 없게 된 히스토리가 보존되지 않음"

    entry = next(r for r in manifest["repos"] if r["repo"] == "me/app")
    assert entry["parked_history"]["unreachable_refs"] >= 1

    # 보존된 번들에서 원래 커밋을 실제로 되살릴 수 있어야 합니다.
    restored = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(bundles[0]), str(restored)], check=True, capture_output=True)
    found = subprocess.run(
        ["git", "cat-file", "-e", f"{original_head}^{{commit}}"],
        cwd=restored, capture_output=True,
    )
    assert found.returncode == 0, "옛 커밋이 보존된 번들에 없음"


def test_mirror_with_incremental_skips_unchanged_without_pointers(env):
    tmp_path, _routes, server, _bare = env
    cfg = configure(tmp_path, server)
    cfg.data["runtime"]["incremental"] = True
    BackupRunner(cfg).run()
    manifest = BackupRunner(cfg).run()

    entry = next(r for r in manifest["repos"] if r["repo"] == "me/app")
    assert entry["status"] == "unchanged"
    # 미러에는 사본이 그대로 있으므로 이전 스냅샷을 가리킬 필요가 없습니다.
    assert "in_snapshot" not in entry
    assert (tmp_path / "remote" / "mirror" / "repos" / "me__app" / "git" / "repo.bundle").is_file()


def test_mirror_records_checksums_for_every_uploaded_file(env):
    """로컬 사본을 지워도 SHA256SUMS 는 완전해야 합니다."""
    tmp_path, _routes, server, _bare = env
    manifest = BackupRunner(configure(tmp_path, server)).run()

    remote = tmp_path / "remote" / "mirror"
    sums = dict(
        reversed(line.split("  ", 1))
        for line in (remote / "SHA256SUMS").read_text().splitlines()
        if line
    )
    assert "repos/me__app/git/repo.bundle" in sums
    assert manifest["files"] == len(sums) and manifest["bytes"] > 0

    from checkpoint.util import sha256_file

    for relative, expected in sums.items():
        assert sha256_file(remote / relative) == expected, relative
