"""A minimal unified-diff-style line diff: ``- `` for a removed line, ``+ ``
for an added line, ``  `` (two spaces) for an unchanged line, aligned via a
classic LCS dynamic-programming table. Good enough for the short texts
(single SQL statements) this is used for - not optimized for large inputs.

returns ``[""]`` for an
empty string (one blank line), but Python's ``str.splitlines()`` returns
``[]`` (zero lines) - already the behavior we want here, since an empty
previous/new SQL genuinely means "no lines".
"""

from __future__ import annotations


def line_diff(old: str, new: str) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    n, m = len(old_lines), len(new_lines)

    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if old_lines[i] == new_lines[j]:
                lcs[i][j] = lcs[i + 1][j + 1] + 1
            else:
                lcs[i][j] = max(lcs[i + 1][j], lcs[i][j + 1])

    result: list[str] = []
    i = j = 0
    while i < n and j < m:
        if old_lines[i] == new_lines[j]:
            result.append(f"  {old_lines[i]}")
            i += 1
            j += 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            result.append(f"- {old_lines[i]}")
            i += 1
        else:
            result.append(f"+ {new_lines[j]}")
            j += 1
    while i < n:
        result.append(f"- {old_lines[i]}")
        i += 1
    while j < m:
        result.append(f"+ {new_lines[j]}")
        j += 1

    return "\n".join(result)
