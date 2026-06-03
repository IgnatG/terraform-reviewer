"""Unit tests for :func:`terraform_review_agent.utils.diff.commentable_lines`."""

from __future__ import annotations

from terraform_review_agent.utils.diff import commentable_lines


def test_none_or_empty_patch_yields_no_lines() -> None:
    assert commentable_lines(None) == set()
    assert commentable_lines("") == set()


def test_added_and_context_lines_are_commentable() -> None:
    patch = "\n".join(
        [
            "@@ -1,3 +1,4 @@",
            " ctx a",  # new line 1 (context)
            "+added b",  # new line 2 (added)
            " ctx c",  # new line 3 (context)
            "+added d",  # new line 4 (added)
        ]
    )
    assert commentable_lines(patch) == {1, 2, 3, 4}


def test_deleted_lines_do_not_advance_new_side() -> None:
    patch = "\n".join(
        [
            "@@ -1,3 +1,2 @@",
            " ctx",  # new line 1
            "-removed",  # old side only; new line does NOT advance
            "+replacement",  # new line 2
        ]
    )
    assert commentable_lines(patch) == {1, 2}


def test_multiple_hunks_use_each_hunk_start() -> None:
    patch = "\n".join(
        [
            "@@ -1,1 +1,1 @@",
            "+first",  # new line 1
            "@@ -10,2 +20,2 @@",
            " ctx",  # new line 20
            "+second",  # new line 21
        ]
    )
    assert commentable_lines(patch) == {1, 20, 21}


def test_no_newline_marker_is_ignored() -> None:
    patch = "\n".join(
        [
            "@@ -1,1 +1,1 @@",
            "+only line",  # new line 1
            "\\ No newline at end of file",
        ]
    )
    assert commentable_lines(patch) == {1}


def test_lines_before_any_hunk_header_are_ignored() -> None:
    # Defensive: stray content before the first @@ must not be counted.
    patch = "diff --git a/x b/x\n+not in a hunk\n@@ -1,1 +5,1 @@\n+real"
    assert commentable_lines(patch) == {5}
