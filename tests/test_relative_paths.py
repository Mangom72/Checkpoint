"""output.dir / output.work_dir 가 상대 경로일 때도 동작해야 한다.

config.yaml 과 config.example.yaml 의 기본값이 둘 다 상대 경로("./backups",
"./work")다. git 하위 명령은 미러 클론 디렉터리를 cwd 로 두고 실행되므로,
이 경로들이 상대 경로인 채로 넘어가면 그 cwd 기준으로 다시 해석되어
"<mirror>/backups/mirror/repos/.../repo.bundle" 같은, 실제로는 존재하지
않는 중첩 경로가 만들어지고 git bundle create 가 128 로 실패한다.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.backup import BackupRunner
from checkpoint.config import Config
from tests.fake_github import FakeGitHub, make_repo
from tests.test_integration import make_bare_repo


@pytest.fixture()
def cwd_elsewhere(tmp_path, monkeypatch):
    """실제 환경(예: GitHub Actions 워크스페이스)처럼 명령을 실행하는 위치와
    작업 디렉터리가 같은, 그러나 output.dir 은 상대 경로로 설정된 상황."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return workspace


def test_relative_output_dir_does_not_break_git_bundle(cwd_elsewhere, monkeypatch):
    bare = make_bare_repo(cwd_elsewhere.parent)
    routes = {
        "/user": {"login": "me"},
        "/user/repos": [make_repo("me/app", str(bare), has_wiki=False)],
        "__graphql__": {"repository": None},
    }
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")

    with FakeGitHub(routes, page_size=50) as server:
        cfg = Config.load(None)
        cfg.data["github"].update({"api_url": server.url, "graphql_url": f"{server.url}/graphql"})
        # config.yaml 실사용 설정과 동일하게 상대 경로로 둡니다.
        cfg.data["output"].update(
            {"mode": "mirror", "dir": "./backups", "work_dir": "./work", "keep_local": True}
        )
        cfg.data["storage"] = {"backend": "local", "local": {"path": "./remote"}}
        cfg.data["runtime"].update(
            {"state_file": "./state.json", "concurrency": 1, "log_level": "WARNING"}
        )
        cfg.data["collect"].update({"account_gists": False, "discussions": False})

        manifest = BackupRunner(cfg).run()

    assert manifest["failed"] == 0, manifest["repos"]
    entry = manifest["repos"][0]
    assert entry["status"] == "exported"
    assert entry["git"]["verified"] is True
    assert (cwd_elsewhere / "backups" / "mirror" / "repos" / "me__app" / "git" / "repo.bundle").is_file()


def test_output_dir_is_resolved_regardless_of_later_chdir(cwd_elsewhere, monkeypatch, tmp_path):
    """resolve() 는 BackupRunner 생성 시점의 cwd 를 기준으로 한 번만 고정해야 한다."""
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    with FakeGitHub({"/user": {"login": "me"}, "__graphql__": {}}, page_size=50) as server:
        cfg = Config.load(None)
        cfg.data["github"].update({"api_url": server.url, "graphql_url": f"{server.url}/graphql"})
        cfg.data["output"].update({"dir": "./backups", "work_dir": "./work"})
        runner = BackupRunner(cfg)

    assert runner.output_dir == (cwd_elsewhere / "backups").resolve()
    assert runner.output_dir.is_absolute()
    assert runner.work_root.is_absolute()
