"""Configuration loading, defaults and validation."""

from __future__ import annotations

import copy
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULTS: dict[str, Any] = {
    "github": {
        "api_url": "https://api.github.com",
        "graphql_url": "https://api.github.com/graphql",
        "token_env": "GITHUB_TOKEN",
        "token": None,
        # --- repository selection ---
        "include_owned": True,
        "include_collaborator": False,
        "include_orgs": [],
        "include": [],
        "exclude": [],
        "include_forks": False,
        "include_archived": True,
        "include_private": True,
    },
    "collect": {
        "git": True,
        "git_pull_refs": False,
        "git_lfs": False,
        "wiki": True,
        "repo_meta": True,
        "issues": True,
        "issue_comments": True,
        "issue_events": True,
        "pulls": True,
        "pull_reviews": True,
        "pull_review_comments": True,
        "pull_commits": True,
        "releases": True,
        "release_assets": True,
        "release_asset_max_mb": 200,
        "discussions": True,
        "labels": True,
        "milestones": True,
        "tags": True,
        "branches": True,
        "commit_comments": True,
        "contributors": True,
        "collaborators": True,
        "stargazers": False,
        "watchers": False,
        "forks": False,
        "webhooks": False,
        "workflows": True,
        "workflow_runs": False,
        "workflow_runs_limit": 200,
        "deployments": False,
        "environments": False,
        "projects_v2": False,
        "traffic": False,
        "readme_html": False,
        "account_profile": True,
        "account_gists": True,
        "account_starred": True,
        "account_following": True,
    },
    "output": {
        "mode": "snapshot",
        "dir": "./backups",
        "work_dir": "./work",
        "archive": True,
        "compression": "gz",
        "snapshot_name": "%Y-%m-%dT%H-%M-%SZ",
        "keep_local": True,
        "stream_upload": False,
    },
    "storage": {
        "backend": "rclone",
        "rclone": {
            "binary": "rclone",
            "remote": "gdrive:",
            "path": "Checkpoint/github-backups",
            "extra_args": ["--transfers=4", "--checkers=8", "--drive-chunk-size=64M"],
        },
        "local": {"path": "./remote-backups"},
    },
    "retention": {
        "keep_last": 12,
        "keep_daily": 7,
        "keep_weekly": 4,
        "keep_monthly": 6,
        "prune_remote": True,
        "prune_local": True,
    },
    "runtime": {
        "concurrency": 4,
        "request_timeout": 60,
        "max_retries": 6,
        "incremental": False,
        "state_file": ".checkpoint-state.json",
        "log_level": "INFO",
        "redact_repo_names": False,
        "fail_fast": False,
    },
}


class ConfigError(RuntimeError):
    pass


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` references in strings."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Thin wrapper around the merged configuration dictionary."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self.data = data
        self.source = source

    # -- access helpers -------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, path: str) -> Any:
        sentinel = object()
        value = self.get(path, sentinel)
        if value is sentinel:
            raise KeyError(path)
        return value

    @property
    def github(self) -> dict[str, Any]:
        return self.data["github"]

    @property
    def collect(self) -> dict[str, Any]:
        return self.data["collect"]

    def wants(self, name: str) -> bool:
        return bool(self.collect.get(name, False))

    # -- construction ---------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None) -> "Config":
        raw: dict[str, Any] = {}
        source: Path | None = None
        if path:
            source = Path(path)
            if not source.exists():
                raise ConfigError(f"config file not found: {source}")
            loaded = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"config root must be a mapping: {source}")
            raw = loaded
        merged = _deep_merge(DEFAULTS, _expand_env(raw))
        cfg = cls(merged, source)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        backend = self.get("storage.backend")
        if backend not in ("rclone", "local", "none"):
            raise ConfigError(f"storage.backend must be rclone|local|none, got {backend!r}")
        mode = self.get("output.mode")
        if mode not in ("snapshot", "mirror"):
            raise ConfigError(f"output.mode must be snapshot|mirror, got {mode!r}")
        compression = self.get("output.compression")
        if compression not in ("gz", "bz2", "xz", "none"):
            raise ConfigError(f"output.compression must be gz|bz2|xz|none, got {compression!r}")
        if int(self.get("runtime.concurrency", 1)) < 1:
            raise ConfigError("runtime.concurrency must be >= 1")
        if backend == "rclone" and not self.get("storage.rclone.remote"):
            raise ConfigError("storage.rclone.remote is required when backend is rclone")

    # -- derived values -------------------------------------------------
    def resolve_token(self) -> str:
        token = self.get("github.token") or os.environ.get(
            self.get("github.token_env", "GITHUB_TOKEN"), ""
        )
        token = (token or "").strip()
        if not token:
            raise ConfigError(
                "No GitHub token. Set the environment variable named by "
                f"github.token_env ({self.get('github.token_env')}) or github.token."
            )
        return token

    def repo_selected(self, full_name: str, *, fork: bool, archived: bool, private: bool) -> bool:
        """Apply include/exclude globs and attribute filters to a repository."""
        include = self.get("github.include", []) or []
        exclude = self.get("github.exclude", []) or []
        name = full_name.lower()

        if any(fnmatch.fnmatch(name, pat.lower()) for pat in exclude):
            return False
        if include and not any(fnmatch.fnmatch(name, pat.lower()) for pat in include):
            return False
        if fork and not self.get("github.include_forks", False):
            return False
        if archived and not self.get("github.include_archived", True):
            return False
        if private and not self.get("github.include_private", True):
            return False
        return True
