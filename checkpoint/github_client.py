"""A small, dependency-light GitHub API client.

Handles pagination, primary/secondary rate limits, retries with backoff and
GraphQL queries.  It is safe to share one instance across worker threads.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

USER_AGENT = "Checkpoint-GitHub-Backup/1.0"
RETRY_STATUS = {500, 502, 503, 504, 522, 524}


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class NotFound(GitHubError):
    pass


class Forbidden(GitHubError):
    """403/404 caused by insufficient scopes rather than a missing object."""


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        graphql_url: str = "https://api.github.com/graphql",
        timeout: int = 60,
        max_retries: int = 6,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._token = token
        self._local = threading.local()
        self._rate_lock = threading.Lock()
        self._pause_until = 0.0

    # -- plumbing -------------------------------------------------------
    @property
    def session(self) -> requests.Session:
        """One :class:`requests.Session` per thread (Sessions are not thread-safe)."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": USER_AGENT,
                }
            )
            self._local.session = session
        return session

    def _wait_if_paused(self) -> None:
        while True:
            with self._rate_lock:
                remaining = self._pause_until - time.time()
            if remaining <= 0:
                return
            log.info("rate limited, sleeping %.0fs", remaining)
            time.sleep(min(remaining, 30))

    def _pause(self, seconds: float, reason: str) -> None:
        seconds = max(1.0, min(seconds, 3600.0))
        with self._rate_lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)
        log.warning("pausing %.0fs (%s)", seconds, reason)

    def _note_rate_headers(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            if int(remaining) <= 1:
                self._pause(float(reset) - time.time() + 2, "primary rate limit exhausted")
        except ValueError:
            pass

    def _retry_after(self, response: requests.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                return 60.0
        reset = response.headers.get("X-RateLimit-Reset")
        if response.status_code == 403 and reset:
            try:
                return max(1.0, float(reset) - time.time() + 2)
            except ValueError:
                return 60.0
        return None

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        allow_status: tuple[int, ...] = (),
    ) -> requests.Response:
        if not url.startswith("http"):
            url = f"{self.api_url}/{url.lstrip('/')}"

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_if_paused()
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                    stream=stream,
                )
            except requests.RequestException as exc:  # network hiccup
                last_error = exc
                delay = min(2**attempt, 60) + random.uniform(0, 1)
                log.warning("%s %s failed (%s); retrying in %.1fs", method, url, exc, delay)
                time.sleep(delay)
                continue

            self._note_rate_headers(response)

            if response.status_code in allow_status or response.ok:
                return response

            if response.status_code in (403, 429):
                body = response.text[:400]
                # Secondary rate limit / abuse detection -> back off and retry.
                if "rate limit" in body.lower() or "secondary" in body.lower() or response.status_code == 429:
                    delay = self._retry_after(response) or min(2**attempt, 120)
                    self._pause(delay, f"{response.status_code} on {url}")
                    continue
                raise Forbidden(f"403 for {url}: {body}", 403, url)

            if response.status_code == 404:
                raise NotFound(f"404 for {url}", 404, url)

            if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                delay = min(2**attempt, 60) + random.uniform(0, 1)
                log.warning("%s %s -> %s; retrying in %.1fs", method, url, response.status_code, delay)
                time.sleep(delay)
                continue

            raise GitHubError(
                f"{response.status_code} for {url}: {response.text[:400]}",
                response.status_code,
                url,
            )

        raise GitHubError(f"exhausted retries for {url}: {last_error}", None, url)

    # -- high level helpers ---------------------------------------------
    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a paginated REST collection, following Link headers."""
        query = dict(params or {})
        query.setdefault("per_page", per_page)
        count = 0
        next_url: str | None = url

        while next_url:
            response = self.request("GET", next_url, params=query)
            query = {}  # subsequent Link URLs already carry the query string
            payload = response.json()
            if isinstance(payload, dict):
                # Some endpoints wrap the collection (e.g. check runs, workflows).
                for key in ("items", "workflows", "workflow_runs", "artifacts", "check_runs"):
                    if key in payload:
                        payload = payload[key]
                        break
                else:
                    payload = [payload]
            for item in payload:
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            next_url = response.links.get("next", {}).get("url")

    def paginate_list(self, url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.paginate(url, **kwargs))

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request(
            "POST", self.graphql_url, json={"query": query, "variables": variables or {}}
        )
        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in payload["errors"])
            types = {e.get("type") for e in payload["errors"]}
            if "NOT_FOUND" in types or "FORBIDDEN" in types:
                raise NotFound(f"graphql: {messages}", 404, self.graphql_url)
            raise GitHubError(f"graphql: {messages}", None, self.graphql_url)
        return payload.get("data") or {}

    def graphql_paginate(
        self,
        query: str,
        variables: dict[str, Any],
        path: list[str],
        *,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Walk a GraphQL connection at ``path`` (list of keys) collecting nodes."""
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self.graphql(query, {**variables, "cursor": cursor, "first": page_size})
            node: Any = data
            for key in path:
                if node is None:
                    break
                node = node.get(key)
            if not node:
                break
            nodes.extend(node.get("nodes") or [])
            page_info = node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return nodes

    # -- misc -----------------------------------------------------------
    def viewer(self) -> dict[str, Any]:
        return self.get_json("/user")

    def rate_limit(self) -> dict[str, Any]:
        return self.get_json("/rate_limit")

    def token_scopes(self) -> list[str]:
        response = self.request("GET", "/user")
        raw = response.headers.get("X-OAuth-Scopes", "")
        return [s.strip() for s in raw.split(",") if s.strip()]
