"""Unit tests for :mod:`terraform_review_agent.github_client`.

We drive the client through a fake transport so no real HTTP traffic is
involved. The transport records every call (method + url + payload) and
returns canned JSON responses, which is enough to verify the sticky-comment
upsert logic deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from terraform_review_agent.github_client import GitHubClient, ReviewComment, inline_marker

MARKER = "<!-- terraform-review-agent:v1 -->"


@dataclass
class _Call:
    method: str
    url: str
    json: Any | None
    params: dict[str, Any] | None


@dataclass
class FakeTransport:
    """Records requests and returns scripted responses."""

    responses: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[_Call] = field(default_factory=list)

    def queue(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        json_body: Any = None,
    ) -> None:
        self.responses.setdefault((method, url), []).append({"status": status, "json": json_body})

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self.calls.append(_Call(method=method, url=url, json=json, params=params))
        queue = self.responses.get((method, url))
        if not queue:
            raise AssertionError(f"unexpected request: {method} {url}")
        spec = queue.pop(0)
        return httpx.Response(
            status_code=spec["status"],
            json=spec["json"],
            request=httpx.Request(method, url),
        )


def _client(transport: FakeTransport) -> GitHubClient:
    return GitHubClient(token="t0ken", transport=transport, marker=MARKER)


def test_upsert_creates_comment_when_none_exists() -> None:
    tx = FakeTransport()
    tx.queue("GET", "/repos/acme/example/issues/7/comments", json_body=[])
    tx.queue(
        "POST",
        "/repos/acme/example/issues/7/comments",
        json_body={"id": 1234},
    )

    comment_id = _client(tx).upsert_sticky_comment("acme/example", 7, "Hello world")

    assert comment_id == 1234
    assert tx.calls[1].method == "POST"
    body = tx.calls[1].json["body"]
    assert body.startswith(MARKER)
    assert "Hello world" in body


def test_upsert_updates_existing_comment_keyed_by_marker() -> None:
    tx = FakeTransport()
    tx.queue(
        "GET",
        "/repos/acme/example/issues/7/comments",
        json_body=[
            {"id": 11, "body": "unrelated"},
            {"id": 22, "body": f"{MARKER}\nprevious"},
        ],
    )
    tx.queue(
        "PATCH",
        "/repos/acme/example/issues/comments/22",
        json_body={"id": 22},
    )

    comment_id = _client(tx).upsert_sticky_comment("acme/example", 7, "fresh body")

    assert comment_id == 22
    assert tx.calls[1].method == "PATCH"
    assert tx.calls[1].url.endswith("/22")
    assert tx.calls[1].json["body"].startswith(MARKER)
    assert "fresh body" in tx.calls[1].json["body"]


def test_upsert_does_not_double_prepend_marker() -> None:
    tx = FakeTransport()
    tx.queue("GET", "/repos/acme/example/issues/7/comments", json_body=[])
    tx.queue(
        "POST",
        "/repos/acme/example/issues/7/comments",
        json_body={"id": 9},
    )

    body_with_marker = f"{MARKER}\nalready-prefixed"
    _client(tx).upsert_sticky_comment("acme/example", 7, body_with_marker)

    posted = tx.calls[1].json["body"]
    assert posted.count(MARKER) == 1


def test_upsert_paginates_when_searching_for_existing_comment() -> None:
    tx = FakeTransport()
    first_page = [{"id": i, "body": "noise"} for i in range(100)]
    second_page = [{"id": 999, "body": f"{MARKER}\nold"}]
    tx.queue(
        "GET",
        "/repos/acme/example/issues/7/comments",
        json_body=first_page,
    )
    tx.queue(
        "GET",
        "/repos/acme/example/issues/7/comments",
        json_body=second_page,
    )
    tx.queue(
        "PATCH",
        "/repos/acme/example/issues/comments/999",
        json_body={"id": 999},
    )

    comment_id = _client(tx).upsert_sticky_comment("acme/example", 7, "body")

    assert comment_id == 999
    assert tx.calls[0].params == {"per_page": 100, "page": 1}
    assert tx.calls[1].params == {"per_page": 100, "page": 2}


def test_fetch_pr_context_populates_changed_files() -> None:
    tx = FakeTransport()
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7",
        json_body={
            "base": {"sha": "base-sha", "ref": "main"},
            "head": {"sha": "head-sha", "ref": "feature/x"},
            "title": "Add bucket",
            "user": {"login": "alice"},
        },
    )
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7/files",
        json_body=[
            {
                "filename": "infra/main.tf",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "patch": "@@ -1 +1 @@",
            },
            {
                "filename": "README.md",
                "status": "added",
                "additions": 5,
                "deletions": 0,
            },
        ],
    )

    pr = _client(tx).fetch_pr_context("acme/example", 7)

    assert pr.repository == "acme/example"
    assert pr.base_sha == "base-sha"
    assert pr.head_sha == "head-sha"
    assert pr.title == "Add bucket"
    assert pr.author == "alice"
    assert [f.path for f in pr.changed_files] == ["infra/main.tf", "README.md"]
    assert pr.has_terraform_changes is True


def test_fetch_pr_context_captures_previous_filename_for_rename() -> None:
    tx = FakeTransport()
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7",
        json_body={
            "base": {"sha": "base-sha", "ref": "main"},
            "head": {"sha": "head-sha", "ref": "feature/x"},
        },
    )
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7/files",
        json_body=[
            {
                "filename": "infra/main.txt",
                "status": "renamed",
                "previous_filename": "infra/main.tf",
                "additions": 0,
                "deletions": 0,
            },
        ],
    )

    pr = _client(tx).fetch_pr_context("acme/example", 7)

    renamed = pr.changed_files[0]
    assert renamed.status == "renamed"
    assert renamed.previous_path == "infra/main.tf"
    # Renaming a .tf away from a Terraform suffix must still trigger review.
    assert pr.has_terraform_changes is True


def test_split_repo_validates_slug() -> None:
    tx = FakeTransport()
    with pytest.raises(ValueError):
        _client(tx).fetch_pr_context("not-a-slug", 1)


# ---------------------------------------------------------------------------
# inline review comments (Phase 10)
# ---------------------------------------------------------------------------


def test_post_review_comments_posts_new_and_skips_already_present() -> None:
    tx = FakeTransport()
    # keyA already exists on the PR; keyB is new.
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7/comments",
        json_body=[{"body": f"old {inline_marker('keyA')} body"}],
    )
    tx.queue("POST", "/repos/acme/example/pulls/7/reviews", json_body={"id": 5})

    comments = [
        ReviewComment(path="main.tf", line=1, body=f"{inline_marker('keyA')}\nA"),
        ReviewComment(path="main.tf", line=2, body=f"{inline_marker('keyB')}\nB"),
    ]
    posted = _client(tx).post_review_comments("acme/example", 7, comments)

    assert posted == 1  # only the new one
    review = tx.calls[1]
    assert review.method == "POST" and review.url.endswith("/pulls/7/reviews")
    assert review.json["event"] == "COMMENT"
    body_comments = review.json["comments"]
    assert [c["line"] for c in body_comments] == [2]
    assert body_comments[0]["side"] == "RIGHT"
    assert body_comments[0]["path"] == "main.tf"
    assert "keyB" in body_comments[0]["body"]


def test_post_review_comments_noop_when_all_already_present() -> None:
    tx = FakeTransport()
    tx.queue(
        "GET",
        "/repos/acme/example/pulls/7/comments",
        json_body=[{"body": inline_marker("keyA")}],
    )
    # No POST queued — if the client tried to post, FakeTransport would raise.
    posted = _client(tx).post_review_comments(
        "acme/example", 7, [ReviewComment(path="m.tf", line=1, body=inline_marker("keyA"))]
    )
    assert posted == 0


def test_post_review_comments_empty_makes_no_requests() -> None:
    tx = FakeTransport()
    assert _client(tx).post_review_comments("acme/example", 7, []) == 0
    assert tx.calls == []
