"""REST/GraphQL export of everything GitHub knows about a repository."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from ..github_client import Forbidden, GitHubClient, GitHubError, NotFound
from ..util import write_json

log = logging.getLogger(__name__)

DISCUSSIONS_QUERY = """
query($owner:String!, $name:String!, $first:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    discussions(first:$first, after:$cursor, orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url createdAt updatedAt lastEditedAt locked isAnswered upvoteCount
        author { login url }
        category { name slug description emoji }
        labels(first:50) { nodes { name color description } }
        answer { id body createdAt author { login } }
        comments(first:50) {
          totalCount
          nodes {
            id body createdAt updatedAt isAnswer upvoteCount author { login }
            replies(first:50) {
              totalCount
              nodes { id body createdAt updatedAt author { login } }
            }
          }
        }
      }
    }
  }
}
"""

PROJECTS_QUERY = """
query($owner:String!, $name:String!, $first:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    projectsV2(first:$first, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title shortDescription url closed createdAt updatedAt
        readme
        fields(first:50) { nodes { ... on ProjectV2FieldCommon { name dataType } } }
        items(first:100) {
          totalCount
          nodes {
            type
            content {
              ... on Issue { number title url state }
              ... on PullRequest { number title url state }
              ... on DraftIssue { title body }
            }
            fieldValues(first:50) {
              nodes {
                ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
                ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
                ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
                ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              }
            }
          }
        }
      }
    }
  }
}
"""


class RepoApiExporter:
    """Dumps a single repository's API-visible data into ``dest``."""

    def __init__(self, client: GitHubClient, repo: dict[str, Any], dest: Path, cfg) -> None:
        self.client = client
        self.repo = repo
        self.full_name = repo["full_name"]
        self.owner, self.name = self.full_name.split("/", 1)
        self.dest = dest
        self.cfg = cfg
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}

    # -- helpers --------------------------------------------------------
    def _base(self, suffix: str = "") -> str:
        return f"/repos/{self.owner}/{self.name}{suffix}"

    def _step(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run one export step, downgrading permission/absence errors to warnings."""
        try:
            return fn()
        except (NotFound, Forbidden) as exc:
            msg = f"{label}: unavailable ({exc.status}) - missing scope or feature disabled"
            log.info("%s [%s] %s", self.full_name, label, "skipped (unavailable)")
            self.warnings.append(msg)
        except GitHubError as exc:
            msg = f"{label}: {exc}"
            log.warning("%s [%s] failed: %s", self.full_name, label, exc)
            self.warnings.append(msg)
        except Exception as exc:  # one bad endpoint must not sink the whole repo
            msg = f"{label}: unexpected {type(exc).__name__}: {exc}"
            log.warning("%s [%s] %s", self.full_name, label, msg)
            log.debug("traceback for %s", label, exc_info=True)
            self.warnings.append(msg)
        return None

    def _dump(self, label: str, filename: str, rows: Any) -> Any:
        write_json(self.dest / filename, rows)
        if isinstance(rows, list):
            self.counts[label] = len(rows)
        return rows

    # -- individual collections -----------------------------------------
    def repo_meta(self) -> None:
        self._dump("repo", "repo.json", self.repo)
        self._step(
            "languages",
            lambda: write_json(self.dest / "languages.json", self.client.get_json(self._base("/languages"))),
        )
        self._step(
            "topics",
            lambda: write_json(self.dest / "topics.json", self.client.get_json(self._base("/topics"))),
        )
        self._step("readme", self._readme)

    def _readme(self) -> None:
        try:
            data = self.client.get_json(self._base("/readme"))
        except NotFound:
            return
        import base64

        if isinstance(data, dict) and data.get("encoding") == "base64":
            content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
            (self.dest / f"README_{data.get('name', 'README.md')}").write_text(content, encoding="utf-8")

    def issues_and_pulls(self) -> None:
        wants_issues = self.cfg.wants("issues")
        wants_pulls = self.cfg.wants("pulls")
        if not (wants_issues or wants_pulls):
            return

        raw = self._step(
            "issues",
            lambda: self.client.paginate_list(
                self._base("/issues"),
                params={"state": "all", "direction": "asc", "sort": "created"},
            ),
        )
        if raw is None:
            return

        issues = [i for i in raw if "pull_request" not in i]
        pull_stubs = [i for i in raw if "pull_request" in i]

        comments_by_number: dict[int, list[dict]] = defaultdict(list)
        if self.cfg.wants("issue_comments"):
            all_comments = self._step(
                "issue_comments",
                lambda: self.client.paginate_list(
                    self._base("/issues/comments"), params={"sort": "created", "direction": "asc"}
                ),
            ) or []
            for comment in all_comments:
                number = _number_from_url(comment.get("issue_url", ""))
                if number:
                    comments_by_number[number].append(comment)

        events_by_number: dict[int, list[dict]] = defaultdict(list)
        if self.cfg.wants("issue_events"):
            all_events = self._step(
                "issue_events",
                lambda: self.client.paginate_list(self._base("/issues/events")),
            ) or []
            for event in all_events:
                number = (event.get("issue") or {}).get("number")
                if number:
                    events_by_number[number].append(event)

        for issue in issues:
            number = issue["number"]
            issue["_comments"] = comments_by_number.get(number, [])
            issue["_events"] = events_by_number.get(number, [])

        if wants_issues:
            self._dump("issues", "issues.json", issues)

        if wants_pulls:
            self._export_pulls(pull_stubs, comments_by_number, events_by_number)

    def _export_pulls(
        self,
        pull_stubs: list[dict],
        comments_by_number: dict[int, list[dict]],
        events_by_number: dict[int, list[dict]],
    ) -> None:
        pulls = self._step(
            "pulls",
            lambda: self.client.paginate_list(
                self._base("/pulls"),
                params={"state": "all", "direction": "asc", "sort": "created"},
            ),
        )
        if pulls is None:
            # Fall back to the (thinner) issue representation of each PR.
            pulls = pull_stubs

        review_comments: dict[int, list[dict]] = defaultdict(list)
        if self.cfg.wants("pull_review_comments"):
            rows = self._step(
                "pull_review_comments",
                lambda: self.client.paginate_list(
                    self._base("/pulls/comments"), params={"sort": "created", "direction": "asc"}
                ),
            ) or []
            for comment in rows:
                number = _number_from_url(comment.get("pull_request_url", ""))
                if number:
                    review_comments[number].append(comment)

        want_reviews = self.cfg.wants("pull_reviews")
        want_commits = self.cfg.wants("pull_commits")

        for pull in pulls:
            number = pull["number"]
            pull["_comments"] = comments_by_number.get(number, [])
            pull["_events"] = events_by_number.get(number, [])
            pull["_review_comments"] = review_comments.get(number, [])
            if want_reviews:
                pull["_reviews"] = (
                    self._step(
                        f"pull#{number} reviews",
                        lambda n=number: self.client.paginate_list(self._base(f"/pulls/{n}/reviews")),
                    )
                    or []
                )
            if want_commits:
                pull["_commits"] = (
                    self._step(
                        f"pull#{number} commits",
                        lambda n=number: self.client.paginate_list(self._base(f"/pulls/{n}/commits")),
                    )
                    or []
                )

        self._dump("pull_requests", "pull_requests.json", pulls)

    def releases(self, token: str) -> None:
        rows = self._step("releases", lambda: self.client.paginate_list(self._base("/releases")))
        if rows is None:
            return
        self._dump("releases", "releases.json", rows)

        if not self.cfg.wants("release_assets"):
            return
        max_bytes = int(self.cfg.collect.get("release_asset_max_mb", 200)) * 1024 * 1024
        asset_root = self.dest / "release_assets"
        for release in rows:
            tag = release.get("tag_name") or f"release-{release.get('id')}"
            for asset in release.get("assets", []) or []:
                size = int(asset.get("size") or 0)
                if max_bytes and size > max_bytes:
                    self.warnings.append(
                        f"release asset skipped (>{max_bytes // 1024 // 1024}MB): {tag}/{asset.get('name')}"
                    )
                    continue
                target = asset_root / _safe_component(tag) / _safe_component(asset.get("name", "asset"))
                self._step(
                    f"asset {tag}/{asset.get('name')}",
                    lambda a=asset, t=target: self._download_asset(a, t),
                )

    def _download_asset(self, asset: dict[str, Any], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        response = self.client.request(
            "GET",
            asset["url"],
            headers={"Accept": "application/octet-stream"},
            stream=True,
        )
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)

    def discussions(self) -> None:
        nodes = self._step(
            "discussions",
            lambda: self.client.graphql_paginate(
                DISCUSSIONS_QUERY,
                {"owner": self.owner, "name": self.name},
                ["repository", "discussions"],
                page_size=25,
            ),
        )
        if nodes is None:
            return
        for node in nodes:
            comments = node.get("comments") or {}
            if comments.get("totalCount", 0) > len(comments.get("nodes") or []):
                self.warnings.append(
                    f"discussion #{node.get('number')}: comment list truncated at 50"
                )
        self._dump("discussions", "discussions.json", nodes)

    def projects_v2(self) -> None:
        nodes = self._step(
            "projects_v2",
            lambda: self.client.graphql_paginate(
                PROJECTS_QUERY,
                {"owner": self.owner, "name": self.name},
                ["repository", "projectsV2"],
                page_size=10,
            ),
        )
        if nodes is not None:
            self._dump("projects_v2", "projects_v2.json", nodes)

    def simple_collections(self) -> None:
        """Endpoints that are a straight paginated list -> one JSON file."""
        specs: list[tuple[str, str, str, dict[str, Any]]] = [
            ("labels", "labels", "/labels", {}),
            ("milestones", "milestones", "/milestones", {"state": "all"}),
            ("tags", "tags", "/tags", {}),
            ("branches", "branches", "/branches", {}),
            ("commit_comments", "comments", "/comments", {}),
            ("contributors", "contributors", "/contributors", {"anon": "1"}),
            ("collaborators", "collaborators", "/collaborators", {"affiliation": "all"}),
            ("stargazers", "stargazers", "/stargazers", {}),
            ("watchers", "watchers", "/subscribers", {}),
            ("forks", "forks", "/forks", {}),
            ("webhooks", "webhooks", "/hooks", {}),
            ("workflows", "workflows", "/actions/workflows", {}),
            ("deployments", "deployments", "/deployments", {}),
            ("environments", "environments", "/environments", {}),
        ]
        for flag, filename, suffix, params in specs:
            if not self.cfg.wants(flag):
                continue
            rows = self._step(
                flag,
                lambda s=suffix, p=params: self.client.paginate_list(self._base(s), params=p),
            )
            if rows is not None:
                self._dump(flag, f"{filename}.json", rows)

        if self.cfg.wants("workflow_runs"):
            limit = int(self.cfg.collect.get("workflow_runs_limit", 200)) or None
            rows = self._step(
                "workflow_runs",
                lambda: self.client.paginate_list(self._base("/actions/runs"), limit=limit),
            )
            if rows is not None:
                self._dump("workflow_runs", "workflow_runs.json", rows)

        if self.cfg.wants("traffic"):
            traffic: dict[str, Any] = {}
            for key, suffix in (
                ("views", "/traffic/views"),
                ("clones", "/traffic/clones"),
                ("popular_paths", "/traffic/popular/paths"),
                ("popular_referrers", "/traffic/popular/referrers"),
            ):
                value = self._step(f"traffic.{key}", lambda s=suffix: self.client.get_json(self._base(s)))
                if value is not None:
                    traffic[key] = value
            if traffic:
                write_json(self.dest / "traffic.json", traffic)

    # -- entry point ----------------------------------------------------
    def run(self, token: str) -> dict[str, Any]:
        if self.cfg.wants("repo_meta"):
            self.repo_meta()
        self.issues_and_pulls()
        if self.cfg.wants("releases"):
            self.releases(token)
        if self.cfg.wants("discussions") and self.repo.get("has_discussions", True):
            self.discussions()
        if self.cfg.wants("projects_v2"):
            self.projects_v2()
        self.simple_collections()
        return {"counts": self.counts, "warnings": self.warnings}


def _number_from_url(url: str) -> int | None:
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, AttributeError):
        return None


def _safe_component(name: str) -> str:
    from ..util import safe_name

    return safe_name(name)
