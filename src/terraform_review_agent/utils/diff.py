"""Unified-diff parsing for inline PR review comments.

GitHub only accepts a review comment on a line that is part of the PR diff. This
parses a file's unified-diff ``patch`` (as returned by the PR files API) and
returns the **new-file** line numbers that are commentable on the RIGHT side —
added (``+``) and context (`` ``) lines. Deleted (``-``) lines live only on the
old side and aren't commentable on the new file.
"""

from __future__ import annotations

import re

# Hunk header: @@ -old_start,old_count +new_start,new_count @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(patch: str | None) -> set[int]:
    """New-file line numbers GitHub will accept an inline comment on.

    Returns added + context line numbers across every hunk. An absent patch
    (large/binary file, or omitted by the API) yields an empty set, so the caller
    simply posts no inline comment for that file.
    """

    if not patch:
        return set()

    lines: set[int] = set()
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        header = _HUNK_RE.match(raw)
        if header:
            new_line = int(header.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue  # old side only — new-file line number doesn't advance
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file" — not a real line
        else:
            lines.add(new_line)  # context line (commentable on RIGHT)
            new_line += 1
    return lines
