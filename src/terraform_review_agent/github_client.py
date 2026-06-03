"""Thin GitHub REST client used by the review graph.

Two responsibilities:

* Fetch the PR metadata and the list of changed files (with patches).
* Upsert a single "sticky" review comment, identified by a hidden HTML marker
  so subsequent runs edit the same comment instead of stacking up.

The client deliberately uses ``httpx`` directly rather than ``PyGithub`` to
keep the dependency surface narrow and to make request shapes obvious in
tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

from terraform_review_agent.config import Settings, settings
from terraform_review_agent.utils.state import ChangedFile, PRContext

log = structlog.get_logger(__name__)

# Hidden per-comment marker so re-runs don't repost the same inline comment.
# The body carries `<!-- tra-inline:<key> -->`; a key already present on the PR
# is skipped. Kept distinct from the sticky-comment marker.
_INLINE_MARKER_RE = re.compile(r"<!-- tra-inline:([^\s>]+) -->")


def inline_marker(key: str) -> str:
    """The hidden marker embedded in an inline comment body, keyed by ``key``."""

    return f"<!-- tra-inline:{key} -->"


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment: a body anchored to a new-file line."""

    path: str
    line: int
    body: str


class _HTTPTransport(Protocol):
    """Subset of ``httpx.Client`` we depend on — eases unit testing."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = ...,
        params: dict[str, Any] | None = ...,
    ) -> httpx.Response: ...


def _split_repo(repository: str) -> tuple[str, str]:
    owner, _, repo = repository.partition("/")
    if not owner or not repo:
        raise ValueError(f"Expected 'owner/repo', got {repository!r}")
    return owner, repo


class GitHubClient:
    """Minimal client for the operations this agent needs."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.github.com",
        transport: _HTTPTransport | None = None,
        marker: str = "<!-- terraform-review-agent:v1 -->",
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._marker = marker
        self._client = transport or httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "terraform-review-agent/1.0",
            },
            timeout=30.0,
        )

    @classmethod
    def from_settings(
        cls,
        settings_obj: Settings | None = None,
        *,
        transport: _HTTPTransport | None = None,
    ) -> GitHubClient:
        cfg = settings_obj or settings
        if cfg.github_token is None:
            raise RuntimeError("GITHUB_TOKEN is not set; cannot construct GitHubClient")
        return cls(
            token=cfg.github_token.get_secret_value(),
            base_url=cfg.github_api_url,
            transport=transport,
            marker=cfg.sticky_comment_marker,
        )

    @property
    def marker(self) -> str:
        return self._marker

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self._client.request(method, url, json=json, params=params)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def fetch_pr_context(self, repository: str, pr_number: int) -> PRContext:
        """Return :class:`PRContext` populated with PR metadata and changed files."""

        owner, repo = _split_repo(repository)
        pr_payload = self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        files_payload = self._fetch_changed_files(owner, repo, pr_number)

        return PRContext(
            repository=repository,
            pr_number=pr_number,
            base_sha=pr_payload["base"]["sha"],
            head_sha=pr_payload["head"]["sha"],
            base_ref=pr_payload["base"]["ref"],
            head_ref=pr_payload["head"]["ref"],
            title=pr_payload.get("title") or "",
            author=(pr_payload.get("user") or {}).get("login") or "",
            changed_files=files_payload,
        )

    def _fetch_changed_files(self, owner: str, repo: str, pr_number: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            for item in batch:
                files.append(
                    ChangedFile(
                        path=item["filename"],
                        status=_normalize_status(item.get("status", "modified")),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                        patch=item.get("patch"),
                        previous_path=item.get("previous_filename"),
                    )
                )
            if len(batch) < 100:
                break
            page += 1
        return files

    def upsert_sticky_comment(self, repository: str, pr_number: int, body: str) -> int:
        """Create or edit the review comment, keyed by the hidden marker.

        Returns the comment id.
        """

        owner, repo = _split_repo(repository)
        existing = self._find_existing_comment(owner, repo, pr_number)
        full_body = body if self._marker in body else f"{self._marker}\n{body}"

        if existing is None:
            log.info("creating new sticky comment", repo=repository, pr=pr_number)
            created = self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                json={"body": full_body},
            )
            return int(created["id"])

        log.info(
            "updating existing sticky comment",
            repo=repository,
            pr=pr_number,
            comment_id=existing,
        )
        updated = self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{existing}",
            json={"body": full_body},
        )
        return int(updated["id"])

    def post_review_comments(
        self, repository: str, pr_number: int, comments: list[ReviewComment]
    ) -> int:
        """Post a single PR review with one inline comment per ``ReviewComment``.

        Idempotent: comments whose hidden ``tra-inline`` marker already exists on
        the PR are skipped, so re-running on a new push doesn't duplicate them.
        Returns the number of *new* comments posted (0 when there's nothing new).

        Callers must pass only comments whose ``line`` sits on the PR diff (see
        :func:`utils.diff.commentable_lines`); GitHub rejects the whole review
        otherwise.
        """

        if not comments:
            return 0
        owner, repo = _split_repo(repository)
        seen = self._existing_inline_markers(owner, repo, pr_number)
        fresh = [c for c in comments if _marker_key(c.body) not in seen]
        if not fresh:
            log.info("no new inline comments", repo=repository, pr=pr_number)
            return 0
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json={
                "event": "COMMENT",
                "comments": [
                    {"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in fresh
                ],
            },
        )
        log.info("posted inline review comments", repo=repository, pr=pr_number, count=len(fresh))
        return len(fresh)

    def _existing_inline_markers(self, owner: str, repo: str, pr_number: int) -> set[str]:
        """Marker keys already present on the PR's review comments (paginated)."""

        seen: set[str] = set()
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                return seen
            for comment in batch:
                for match in _INLINE_MARKER_RE.finditer(comment.get("body") or ""):
                    seen.add(match.group(1))
            if len(batch) < 100:
                return seen
            page += 1

    def _find_existing_comment(self, owner: str, repo: str, pr_number: int) -> int | None:
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                return None
            for comment in batch:
                if self._marker in (comment.get("body") or ""):
                    return int(comment["id"])
            if len(batch) < 100:
                return None
            page += 1


def _marker_key(body: str) -> str | None:
    """Extract the ``tra-inline`` marker key from a comment body, if present."""

    match = _INLINE_MARKER_RE.search(body)
    return match.group(1) if match else None


def _normalize_status(raw: str) -> str:
    if raw in {"added", "modified", "removed", "renamed"}:
        return raw
    if raw == "changed":
        return "modified"
    return "modified"
