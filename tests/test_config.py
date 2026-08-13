import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.config import Config, ConfigError


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_apply_when_no_file():
    cfg = Config.load(None)
    assert cfg.get("storage.backend") == "rclone"
    assert cfg.wants("issues") is True
    assert cfg.get("runtime.concurrency") == 4


def test_user_values_merge_over_defaults(tmp_path):
    cfg = Config.load(write(tmp_path, "collect:\n  issues: false\nruntime:\n  concurrency: 8\n"))
    assert cfg.wants("issues") is False
    assert cfg.wants("pulls") is True  # untouched default survives
    assert cfg.get("runtime.concurrency") == 8


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_REMOTE", "drive:")
    cfg = Config.load(
        write(tmp_path, 'storage:\n  rclone:\n    remote: "${MY_REMOTE}"\n    path: "${NOPE:-fallback}"\n')
    )
    assert cfg.get("storage.rclone.remote") == "drive:"
    assert cfg.get("storage.rclone.path") == "fallback"


def test_invalid_backend_rejected(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(write(tmp_path, "storage:\n  backend: dropbox\n"))


def test_token_resolution(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg = Config.load(None)
    with pytest.raises(ConfigError):
        cfg.resolve_token()
    monkeypatch.setenv("GITHUB_TOKEN", "  ghp_secret  ")
    assert cfg.resolve_token() == "ghp_secret"


@pytest.mark.parametrize(
    "include,exclude,name,expected",
    [
        ([], [], "me/app", True),
        (["me/*"], [], "me/app", True),
        (["you/*"], [], "me/app", False),
        ([], ["*/secret*"], "me/secrets", False),
        (["me/*"], ["me/app"], "me/app", False),
        ([], [], "ME/App", True),
    ],
)
def test_repo_selection_globs(include, exclude, name, expected):
    cfg = Config.load(None)
    cfg.data["github"]["include"] = include
    cfg.data["github"]["exclude"] = exclude
    assert cfg.repo_selected(name, fork=False, archived=False, private=False) is expected


def test_attribute_filters():
    cfg = Config.load(None)
    assert cfg.repo_selected("me/f", fork=True, archived=False, private=False) is False
    cfg.data["github"]["include_forks"] = True
    assert cfg.repo_selected("me/f", fork=True, archived=False, private=False) is True
    cfg.data["github"]["include_private"] = False
    assert cfg.repo_selected("me/p", fork=False, archived=False, private=True) is False
