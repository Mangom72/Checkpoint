"""Work out which repositories should be backed up."""

from __future__ import annotations

import logging
from typing import Any

from .github_client import GitHubClient, GitHubError, NotFound

log = logging.getLogger(__name__)

GLOB_CHARS = set("*?[]")


def discover_repos(client: GitHubClient, cfg) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    affiliations: list[str] = []
    if cfg.get("github.include_owned", True):
        affiliations.append("owner")
    if cfg.get("github.include_collaborator", False):
        affiliations.append("collaborator")

    if affiliations:
        params = {"affiliation": ",".join(affiliations), "sort": "full_name"}
        for repo in client.paginate("/user/repos", params=params):
            found[repo["full_name"]] = repo

    for org in cfg.get("github.include_orgs", []) or []:
        try:
            for repo in client.paginate(f"/orgs/{org}/repos", params={"type": "all"}):
                found.setdefault(repo["full_name"], repo)
        except (NotFound, GitHubError) as exc:
            log.warning("could not list repos for org %s: %s", org, exc)

    # Explicit "owner/repo" entries are fetched directly so they work even when
    # they would not show up in the listings above.
    for pattern in cfg.get("github.include", []) or []:
        if any(ch in pattern for ch in GLOB_CHARS) or "/" not in pattern:
            continue
        if pattern in found:
            continue
        try:
            found[pattern] = client.get_json(f"/repos/{pattern}")
        except GitHubError as exc:
            log.warning("could not fetch %s: %s", pattern, exc)

    selected = [
        repo
        for repo in found.values()
        if cfg.repo_selected(
            repo["full_name"],
            fork=bool(repo.get("fork")),
            archived=bool(repo.get("archived")),
            private=bool(repo.get("private")),
        )
    ]
    selected.sort(key=lambda r: r["full_name"].lower())
    log.info("selected %d of %d visible repositories", len(selected), len(found))
    return selected
