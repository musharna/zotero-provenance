"""Derive the `project:` tag for a captured source from the session's cwd.

Ordered strategies, most specific first. Every strategy is pure — no subprocess,
no filesystem writes — so the whole thing is testable without a real checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

FALLBACK_SLUG = "home"


def _git_root(start: Path) -> Path | None:
    """Nearest ancestor containing `.git` (directory for a repo, file for a worktree)."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _segment_under_root(cwd: Path, root: Path) -> str | None:
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def derive_slug(
    cwd: str | os.PathLike[str] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return a stable, human-meaningful project slug for `cwd`.

    1. `ZOTERO_CAPTURE_PROJECT` override, if set.
    2. Basename of the nearest enclosing git repository.
    3. First path segment beneath `$HOME` (or a `ZOTERO_CAPTURE_PROJECT_ROOTS` entry).
    4. Basename of cwd.
    5. `"home"`.
    """
    env = os.environ if env is None else env
    override = env.get("ZOTERO_CAPTURE_PROJECT")
    if override:
        return override
    if not cwd:
        return FALLBACK_SLUG
    path = Path(cwd)

    root = _git_root(path)
    if root is not None and root.name:
        return root.name

    roots: list[Path] = []
    extra = env.get("ZOTERO_CAPTURE_PROJECT_ROOTS", "")
    roots.extend(Path(p).expanduser() for p in extra.split(os.pathsep) if p)
    home = env.get("HOME")
    if home:
        roots.append(Path(home))
    for candidate_root in roots:
        if path == candidate_root:
            return FALLBACK_SLUG
        seg = _segment_under_root(path, candidate_root)
        if seg:
            return seg

    return path.name or FALLBACK_SLUG
