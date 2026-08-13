"""End-to-end: fake GitHub + real git repo -> snapshot -> 'remote' storage."""

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.backup import BackupRunner
from checkpoint.config import Config
from tests.fake_github import FakeGitHub, make_repo

GIT_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def make_bare_repo(tmp_path: Path) -> Path:
    """A real bare repo with two commits and a tag, to clone from."""
    work = tmp_path / "src"
    work.mkdir()
    run = lambda *args: subprocess.run(args, cwd=work, check=True, capture_output=True, env={**GIT_ENV, "PATH": "/usr/bin:/bin"})
    run("git", "init", "-q", "-b", "main")
    (work / "README.md").write_text("hello\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "first")
    (work / "second.txt").write_text("more\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "second")
    run("git", "tag", "v1.0.0")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(bare)], check=True, capture_output=True)
    return bare


def routes_for(full_name: str, clone_url: str, server_url_holder: dict) -> dict:
    owner, name = full_name.split("/")
    base = f"/repos/{owner}/{name}"
    repo = make_repo(full_name, clone_url, has_wiki=False)
    issue = {
        "number": 1,
        "title": "a bug",
        "state": "open",
        "body": "it broke",
        "user": {"login": owner},
        "created_at": "2026-07-01T00:00:00Z",
    }
    pull_stub = {"number": 2, "title": "a fix", "pull_request": {"url": f"{base}/pulls/2"}}
    return {
        "/user": {"login": owner},
        "/user/repos": [repo],
        base: repo,
        f"{base}/languages": {"Python": 100},
        f"{base}/topics": {"names": ["backup"]},
        f"{base}/readme": {
            "name": "README.md",
            "encoding": "base64",
            "content": "aGVsbG8K",
        },
        f"{base}/issues": [issue, pull_stub],
        f"{base}/issues/comments": [
            {"id": 11, "body": "me too", "issue_url": f"{base}/issues/1"},
        ],
        f"{base}/issues/events": [
            {"id": 21, "event": "labeled", "issue": {"number": 1}},
        ],
        f"{base}/pulls": [
            {"number": 2, "title": "a fix", "state": "closed", "merged_at": "2026-07-05T00:00:00Z"}
        ],
        f"{base}/pulls/comments": [
            {"id": 31, "body": "nit", "pull_request_url": f"{base}/pulls/2"},
        ],
        f"{base}/pulls/2/reviews": [{"id": 41, "state": "APPROVED"}],
        f"{base}/pulls/2/commits": [{"sha": "deadbeef"}],
        f"{base}/releases": [
            {
                "id": 51,
                "tag_name": "v1.0.0",
                "name": "First",
                "assets": [
                    {
                        "name": "artifact.bin",
                        "size": 5,
                        "url": lambda holder=server_url_holder: None,  # replaced below
                    }
                ],
            }
        ],
        f"{base}/labels": [{"name": "bug"}],
        f"{base}/milestones": [{"number": 1, "title": "v1"}],
        f"{base}/tags": [{"name": "v1.0.0"}],
        f"{base}/branches": [{"name": "main"}],
        f"{base}/comments": [],
        f"{base}/contributors": [{"login": owner, "contributions": 2}],
        f"{base}/collaborators": [{"login": owner}],
        f"{base}/actions/workflows": {"total_count": 0, "workflows": []},
        "/asset/1": {"payload": "binary-ish"},
        "/user/orgs": [],
        "/user/keys": [],
        "/user/gpg_keys": [],
        "/user/emails": [],
        "/user/starred": [{"full_name": "someone/else"}],
        "/user/following": [],
        "/user/followers": [],
        "/gists": [],
        "__graphql__": {"repository": None},
    }


def _configure(tmp_path, server, **output_overrides) -> Config:
    cfg = Config.load(None)
    cfg.data["github"].update({"api_url": server.url, "graphql_url": f"{server.url}/graphql"})
    cfg.data["output"].update(
        {"dir": str(tmp_path / "backups"), "work_dir": str(tmp_path / "work"), **output_overrides}
    )
    cfg.data["storage"] = {"backend": "local", "local": {"path": str(tmp_path / "remote")}}
    cfg.data["runtime"].update(
        {"state_file": str(tmp_path / "state.json"), "concurrency": 1, "log_level": "WARNING"}
    )
    cfg.data["retention"] = {"keep_last": 0, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}
    return cfg


def _run(tmp_path, monkeypatch, **output_overrides):
    bare = make_bare_repo(tmp_path)
    routes = routes_for("me/app", str(bare), {})
    with FakeGitHub(routes, page_size=50) as server:
        routes["/repos/me/app/releases"][0]["assets"][0]["url"] = f"{server.url}/asset/1"
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        cfg = _configure(tmp_path, server, **output_overrides)
        yield tmp_path, BackupRunner(cfg).run(), cfg


@pytest.fixture()
def snapshot(tmp_path, monkeypatch):
    yield from _run(tmp_path, monkeypatch)


@pytest.fixture()
def streamed(tmp_path, monkeypatch):
    yield from _run(tmp_path, monkeypatch, stream_upload=True)


def test_snapshot_is_created_and_uploaded(snapshot):
    tmp_path, manifest, _ = snapshot
    assert manifest["failed"] == 0
    assert manifest["repo_count"] == 1

    local = tmp_path / "backups" / manifest["snapshot"]
    remote = tmp_path / "remote" / manifest["snapshot"]
    assert (local / "manifest.json").is_file()
    assert (local / "SHA256SUMS").is_file()
    assert (remote / "manifest.json").is_file(), "snapshot was not copied to the storage backend"
    assert (remote / "repos" / "me__app.tar.gz").is_file()
    assert manifest["repos"][0]["warnings"] == [], "no collection step should have been skipped"


def test_archive_contains_git_history_and_api_data(snapshot, tmp_path):
    root, manifest, _ = snapshot
    archive = root / "backups" / manifest["snapshot"] / "repos" / "me__app.tar.gz"
    out = root / "unpacked"
    with tarfile.open(archive) as tar:
        tar.extractall(out)
    repo_dir = out / "me__app"

    issues = json.loads((repo_dir / "api" / "issues.json").read_text())
    assert [i["number"] for i in issues] == [1], "pull requests must not be filed as issues"
    assert issues[0]["_comments"][0]["id"] == 11
    assert issues[0]["_events"][0]["id"] == 21

    pulls = json.loads((repo_dir / "api" / "pull_requests.json").read_text())
    assert pulls[0]["_reviews"][0]["state"] == "APPROVED"
    assert pulls[0]["_review_comments"][0]["id"] == 31
    assert pulls[0]["_commits"][0]["sha"] == "deadbeef"

    releases = json.loads((repo_dir / "api" / "releases.json").read_text())
    assert releases[0]["tag_name"] == "v1.0.0"
    assert (repo_dir / "api" / "release_assets" / "v1.0.0" / "artifact.bin").is_file()

    for name in ("labels", "milestones", "tags", "branches", "contributors", "collaborators"):
        assert (repo_dir / "api" / f"{name}.json").is_file(), f"missing {name}.json"

    assert (repo_dir / "git" / "repo.bundle").is_file()
    assert manifest["repos"][0]["git"]["verified"] is True, "번들 검증이 수행되지 않음"
    refs = (repo_dir / "git" / "refs.txt").read_text()
    assert "refs/heads/main" in refs and "refs/tags/v1.0.0" in refs


def test_bundle_restores_to_a_working_clone(snapshot, tmp_path):
    root, manifest, _ = snapshot
    archive = root / "backups" / manifest["snapshot"] / "repos" / "me__app.tar.gz"
    out = root / "restore"
    with tarfile.open(archive) as tar:
        tar.extractall(out)
    bundle = out / "me__app" / "git" / "repo.bundle"

    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True, capture_output=True)
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout
    assert "second" in log and "first" in log
    tags = subprocess.run(
        ["git", "tag"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout
    assert "v1.0.0" in tags
    assert (clone / "README.md").read_text() == "hello\n"


def test_account_data_is_exported(snapshot):
    root, manifest, _ = snapshot
    account = root / "backups" / manifest["snapshot"] / "account.tar.gz"
    assert account.is_file()
    with tarfile.open(account) as tar:
        names = tar.getnames()
    assert any(n.endswith("starred.json") for n in names)
    assert any(n.endswith("profile.json") for n in names)


def test_second_run_with_incremental_skips_unchanged_repo(snapshot):
    root, manifest, cfg = snapshot
    cfg.data["runtime"]["incremental"] = True
    second = BackupRunner(cfg).run()
    statuses = {r["repo"]: r["status"] for r in second["repos"]}
    assert statuses["me/app"] == "unchanged"
    assert second["repos"][0]["in_snapshot"] == manifest["snapshot"]


def test_stream_upload_frees_local_disk_as_it_goes(streamed):
    """레포 아카이브가 만들어지는 즉시 업로드되고 로컬에서 사라져야 합니다."""
    root, manifest, _ = streamed
    local = root / "backups" / manifest["snapshot"]
    remote = root / "remote" / manifest["snapshot"]

    assert manifest["stream_upload"] is True
    assert manifest["failed"] == 0

    # 로컬에는 manifest 와 체크섬만 남습니다.
    leftovers = sorted(p.name for p in local.rglob("*") if p.is_file())
    assert leftovers == ["SHA256SUMS", "manifest.json"], leftovers

    # 원격에는 전부 올라가 있어야 합니다.
    assert (remote / "repos" / "me__app.tar.gz").is_file()
    assert (remote / "account.tar.gz").is_file()
    assert (remote / "manifest.json").is_file()


def test_stream_upload_still_produces_complete_checksums(streamed):
    root, manifest, _ = streamed
    local = root / "backups" / manifest["snapshot"]
    remote = root / "remote" / manifest["snapshot"]

    sums = dict(
        reversed(line.split("  ", 1))
        for line in (local / "SHA256SUMS").read_text().splitlines()
        if line
    )
    assert "repos/me__app.tar.gz" in sums
    assert "account.tar.gz" in sums

    # 스트리밍으로 지워진 파일도 원격 실물과 해시가 일치해야 합니다.
    from checkpoint.util import sha256_file

    for relative, expected in sums.items():
        assert sha256_file(remote / relative) == expected, relative

    assert manifest["files"] == len(sums)
    assert manifest["bytes"] == sum((remote / r).stat().st_size for r in sums)


def test_streamed_and_normal_snapshots_have_identical_layout(tmp_path, monkeypatch, snapshot):
    """스트리밍 여부와 무관하게 원격에 남는 구조는 같아야 합니다."""
    root, manifest, _ = snapshot
    normal = sorted(
        str(p.relative_to(root / "remote" / manifest["snapshot"]))
        for p in (root / "remote" / manifest["snapshot"]).rglob("*")
        if p.is_file()
    )

    other = tmp_path / "streamed-run"
    other.mkdir()
    _, streamed_manifest, _ = next(_run(other, monkeypatch, stream_upload=True))
    streamed_files = sorted(
        str(p.relative_to(other / "remote" / streamed_manifest["snapshot"]))
        for p in (other / "remote" / streamed_manifest["snapshot"]).rglob("*")
        if p.is_file()
    )
    assert streamed_files == normal


def test_redaction_keeps_repo_names_out_of_logs(tmp_path, monkeypatch):
    """public 레포에서 실행 로그가 공개돼도 레포 이름이 드러나지 않아야 합니다."""
    import io
    import logging

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous, previous_level = root.handlers[:], root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    try:
        bare = make_bare_repo(tmp_path)
        routes = routes_for("me/app", str(bare), {})
        with FakeGitHub(routes, page_size=50) as server:
            routes["/repos/me/app/releases"][0]["assets"][0]["url"] = f"{server.url}/asset/1"
            monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
            cfg = _configure(tmp_path, server, stream_upload=True)
            cfg.data["runtime"]["redact_repo_names"] = True
            cfg.data["runtime"]["log_level"] = "DEBUG"
            manifest = BackupRunner(cfg).run()
    finally:
        root.handlers[:] = previous
        root.setLevel(previous_level)

    logged = stream.getvalue()
    assert logged.strip(), "로그가 비어 있으면 검증이 의미 없습니다"
    assert "me/app" not in logged
    assert "me__app" not in logged, "아카이브 슬러그도 가려져야 합니다"
    assert "repo#01" in logged, "별칭으로 대체되어 진행 상황은 계속 보여야 합니다"

    # 실제 이름은 manifest 에만 남고, 대응표로 로그를 되짚을 수 있어야 합니다.
    assert manifest["repos"][0]["repo"] == "me/app"
    assert manifest["log_aliases"] == {"me/app": "repo#01"}


def test_redaction_is_off_by_default(snapshot):
    _root, manifest, _cfg = snapshot
    assert manifest["log_aliases"] is None
