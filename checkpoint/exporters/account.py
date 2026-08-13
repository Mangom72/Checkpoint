"""Account-level export: profile, gists (with content), stars, follows, orgs, keys."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..github_client import Forbidden, GitHubClient, GitHubError, NotFound
from ..util import write_json

log = logging.getLogger(__name__)


def _safe(client: GitHubClient, label: str, fn, warnings: list[str]) -> Any:
    try:
        return fn()
    except (NotFound, Forbidden) as exc:
        warnings.append(f"account.{label}: unavailable ({exc.status})")
    except GitHubError as exc:
        warnings.append(f"account.{label}: {exc}")
    return None


def export_account(client: GitHubClient, dest: Path, cfg) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    counts: dict[str, int] = {}

    if cfg.wants("account_profile"):
        profile = _safe(client, "profile", client.viewer, warnings)
        if profile:
            write_json(dest / "profile.json", profile)
        for label, url in (
            ("organizations", "/user/orgs"),
            ("public_keys", "/user/keys"),
            ("gpg_keys", "/user/gpg_keys"),
            ("emails", "/user/emails"),
        ):
            rows = _safe(client, label, lambda u=url: client.paginate_list(u), warnings)
            if rows is not None:
                write_json(dest / f"{label}.json", rows)
                counts[label] = len(rows)

    if cfg.wants("account_starred"):
        rows = _safe(client, "starred", lambda: client.paginate_list("/user/starred"), warnings)
        if rows is not None:
            write_json(dest / "starred.json", rows)
            counts["starred"] = len(rows)

    if cfg.wants("account_following"):
        for label, url in (("following", "/user/following"), ("followers", "/user/followers")):
            rows = _safe(client, label, lambda u=url: client.paginate_list(u), warnings)
            if rows is not None:
                write_json(dest / f"{label}.json", rows)
                counts[label] = len(rows)

    if cfg.wants("account_gists"):
        gists = _safe(client, "gists", lambda: client.paginate_list("/gists"), warnings)
        if gists is not None:
            counts["gists"] = len(gists)
            _export_gists(client, gists, dest / "gists", warnings)
            write_json(dest / "gists.json", gists)

    return {"counts": counts, "warnings": warnings}


def _export_gists(client: GitHubClient, gists: list[dict], dest: Path, warnings: list[str]) -> None:
    """Fetch each gist's file contents and comments (the list endpoint truncates them)."""
    from ..util import safe_name

    for gist in gists:
        gist_id = gist.get("id")
        if not gist_id:
            continue
        full = _safe(client, f"gist {gist_id}", lambda g=gist_id: client.get_json(f"/gists/{g}"), warnings)
        if not full:
            continue
        gist_dir = dest / safe_name(gist_id)
        gist_dir.mkdir(parents=True, exist_ok=True)
        for filename, meta in (full.get("files") or {}).items():
            content = meta.get("content")
            if content is None and meta.get("raw_url"):
                try:
                    content = client.request("GET", meta["raw_url"]).text
                except GitHubError as exc:
                    warnings.append(f"gist {gist_id}/{filename}: {exc}")
                    continue
            if content is not None:
                (gist_dir / safe_name(filename)).write_text(content, encoding="utf-8")
        full["_comments"] = (
            _safe(
                client,
                f"gist {gist_id} comments",
                lambda g=gist_id: client.paginate_list(f"/gists/{g}/comments"),
                warnings,
            )
            or []
        )
        write_json(gist_dir / "gist.json", full)
