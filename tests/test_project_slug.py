"""Project-slug derivation — the tag that says which project cited a source."""

from __future__ import annotations

from zotero_capture.project_slug import derive_slug

HOME_ENV = {"HOME": "/home/someone"}


def test_explicit_override_wins_over_everything(tmp_path):
    env = {**HOME_ENV, "ZOTERO_CAPTURE_PROJECT": "chosen"}
    assert derive_slug(tmp_path, env=env) == "chosen"


def test_first_segment_under_home():
    assert derive_slug("/home/someone/agrigen", env=HOME_ENV) == "agrigen"


def test_nested_path_collapses_to_top_level_project():
    assert derive_slug("/home/someone/agrigen/backend/api", env=HOME_ENV) == "agrigen"


def test_home_itself_is_the_fallback_slug():
    assert derive_slug("/home/someone", env=HOME_ENV) == "home"


def test_extra_roots_are_honoured():
    env = {**HOME_ENV, "ZOTERO_CAPTURE_PROJECT_ROOTS": "/mnt/c/Users/someone"}
    assert derive_slug("/mnt/c/Users/someone/antgame/wt/x", env=env) == "antgame"


def test_git_root_beats_home_segment(tmp_path):
    """A repo checked out at ~/code/thing must tag as 'thing', not 'code'."""
    repo = tmp_path / "code" / "thing"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert derive_slug(nested, env={"HOME": str(tmp_path)}) == "thing"


def test_git_worktree_file_counts_as_a_repo(tmp_path):
    """`git worktree` writes .git as a FILE, not a directory."""
    repo = tmp_path / "wt-checkout"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert derive_slug(repo, env={"HOME": str(tmp_path)}) == "wt-checkout"


def test_unrecognised_path_uses_basename():
    assert derive_slug("/srv/data/scratchpad", env=HOME_ENV) == "scratchpad"


def test_empty_cwd_falls_back():
    assert derive_slug("", env=HOME_ENV) == "home"
    assert derive_slug(None, env=HOME_ENV) == "home"
