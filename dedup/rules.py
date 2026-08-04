"""Path-based exclusion rules for stripping Windows/system files.

Rules are plain-text glob-ish patterns, one per line, matched against a
file's path relative to the drive root (always using '/' separators,
case-insensitive):

  - A pattern containing '/' is anchored to the start of the relative path.
    A trailing '/**' means "this directory and everything under it".
  - A pattern with no '/' matches the basename anywhere in the tree
    (e.g. "Thumbs.db" excludes every Thumbs.db regardless of folder).
  - '*' matches any run of characters within a single path segment.
  - Blank lines and lines starting with '#' are ignored.
"""

from __future__ import annotations

import re
from pathlib import Path


def _escape_segment(segment: str) -> str:
    return re.escape(segment).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")


def _pattern_to_regex(pattern: str) -> re.Pattern:
    if "/" not in pattern:
        pattern = "**/" + pattern

    if pattern.endswith("/**"):
        base = pattern[: -len("/**")]
        body = "/".join(_escape_segment(s) for s in base.split("/"))
        regex = body + "(?:/.*)?"
    elif pattern.startswith("**/"):
        rest = pattern[len("**/") :]
        body = "/".join(_escape_segment(s) for s in rest.split("/"))
        regex = "(?:.*/)?" + body
    else:
        regex = "/".join(_escape_segment(s) for s in pattern.split("/"))

    return re.compile("^" + regex + "$", re.IGNORECASE)


def load_rules(path: Path) -> list[re.Pattern]:
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(_pattern_to_regex(line))
    return patterns


class RuleSet:
    def __init__(self, patterns: list[re.Pattern]):
        self._patterns = patterns

    @classmethod
    def from_file(cls, path: Path) -> "RuleSet":
        return cls(load_rules(path))

    def is_excluded(self, rel_path: str) -> bool:
        return any(p.match(rel_path) for p in self._patterns)

    def is_dir_pruned(self, rel_dir_path: str) -> bool:
        """Whether an entire directory can be skipped without descending into it.

        Uses a synthetic probe child so directory-scoped patterns (ending in
        '/**') are recognized as covering the directory itself, without
        needing a second set of directory-only patterns.
        """
        if not rel_dir_path:
            return False
        probe = rel_dir_path.rstrip("/") + "/__disk_dedup_probe__"
        return self.is_excluded(probe)
