"""A filter that reached one engine and not the other.

`--only-host` and `--only-outcome` were parsed, documented, and passed to
`snapshot(...)` only. On the `--verify` path they were accepted in silence and
dropped, so `--verify --only-host github.com` re-read all 3,813 rows while the
report called it scoped. That is the `--sleep` defect again -- the CLI complete
at one end, the engine complete at the other, and the whole fault living in the
gap between them.

The guard that was supposed to stop this derives its obligations from argparse,
and it could not see this one: `args.only_host` IS read, on the branch that did
not run. "Every flag is used somewhere" is a weaker property than "every flag is
honoured on the path it was given to", and only the second is what a user means.

So this file asserts the second, and the CLI now REFUSES the two flags that
genuinely have no meaning for a re-read rather than ignoring them -- the same
choice made everywhere else here: loud absence over quiet corruption.
"""

from __future__ import annotations

import ast
import datetime
import subprocess
import sys
from pathlib import Path

from zotero_capture.snapshot import PageRead, verify
from zotero_capture.sqlite_cache import (
    init_db,
    insert_url,
    set_content_hash,
    set_verify_outcome,
)

CLI = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_pages.py"


def _seed(db, urls, outcome=""):
    init_db(db)
    for i, url in enumerate(urls):
        insert_url(db, url, f"K{i}", datetime.date(2026, 5, 5))
        set_content_hash(
            db,
            url,
            content_hash="OLD",
            hashed_at=f"T{i}",
            covers_bytes=64,
            complete=True,
            sketch="", sketch_algo="",
        )
        if outcome:
            set_verify_outcome(db, url, outcome=outcome, at="EARLIER")
    return db


def _recording_hasher(seen):
    def hasher(url: str, max_bytes: int) -> PageRead:
        seen.append(url)
        return PageRead(digest="OLD", final_url=url, covers_bytes=64, complete=True)

    return hasher


def test_verify_honours_only_host_on_a_boundary(tmp_path) -> None:
    """The control is the whole test.

    `notgithub.com` returning 0 is what makes `github.com` returning 2 mean
    something; a filter that matched a substring would take it, and this project
    has shipped a substring where it meant a token three times.
    """
    db = _seed(
        tmp_path / "i.db",
        [
            "https://github.com/a/b",
            "https://gist.github.com/a/b",
            "https://notgithub.com/a/b",
            "https://github.com.evil.test/a/b",
            "https://arxiv.org/abs/1",
        ],
    )
    seen: list[str] = []
    verify(
        db, hasher=_recording_hasher(seen), clock=lambda: "NOW", only_host="github.com"
    )
    assert sorted(seen) == [
        "https://gist.github.com/a/b",
        "https://github.com/a/b",
    ]


def test_verify_honours_only_outcome(tmp_path) -> None:
    db = _seed(
        tmp_path / "i.db", ["https://a.test/1", "https://b.test/1"], outcome="unstable"
    )
    _seed(db, ["https://c.test/1"], outcome="unchanged")
    seen: list[str] = []
    verify(
        db, hasher=_recording_hasher(seen), clock=lambda: "NOW", only_outcome="unstable"
    )
    assert sorted(seen) == ["https://a.test/1", "https://b.test/1"]


def test_an_unscoped_run_still_reads_everything(tmp_path) -> None:
    """Positive control: the filters must not narrow a run nobody scoped."""
    db = _seed(tmp_path / "i.db", ["https://a.test/1", "https://b.test/1"])
    seen: list[str] = []
    verify(db, hasher=_recording_hasher(seen), clock=lambda: "NOW")
    assert len(seen) == 2


def test_snapshot_only_flags_are_refused_rather_than_ignored() -> None:
    for flag in ("--dry-run", "--retry-failed"):
        done = subprocess.run(
            [sys.executable, str(CLI), "--verify", flag],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode != 0, f"{flag} was accepted with --verify"
        assert "cannot be combined with --verify" in done.stderr


def _cli_tree() -> ast.AST:
    return ast.parse(CLI.read_text(encoding="utf-8"))


def _argparse_dests(tree) -> set[str]:
    dests = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    dests.add(str(arg.value)[2:].replace("-", "_"))
    return dests


def _args_read_by(tree, callee: str) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ):
            for kw in node.keywords:
                v = kw.value
                if (
                    isinstance(v, ast.Attribute)
                    and isinstance(v.value, ast.Name)
                    and v.value.id == "args"
                ):
                    out.add(v.attr)
    return out


def _flags_named_in_messages(tree) -> set[str]:
    """Flags named in the code's own prose, EXCLUDING their own declarations.

    Mutation-tested, and the first version was vacuous: it counted every string
    constant, so `add_argument("--only-host", ...)` made `--only-host` look like
    it was handled and the guard passed on the very defect it was written for. A
    flag's existence is not evidence that anything honours it.
    """
    declared = {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
    }
    out = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in declared
        ):
            for token in node.value.split():
                if token.startswith("--"):
                    out.add(token.strip(",.").lstrip("-").replace("-", "_"))
    return out


def test_every_flag_is_either_honoured_by_verify_or_refused_by_it() -> None:
    """Derived from argparse, and checked PER PATH.

    The previous form of this obligation asked whether each flag was read
    anywhere in the module. `--only-host` satisfied that while being dropped on
    the path a user had just selected, which is how it survived a release. The
    property that matters is that a `--verify` run either uses a flag or says
    it will not.
    """
    tree = _cli_tree()
    dests = _argparse_dests(tree)
    honoured = _args_read_by(tree, "verify") | {"verify"}
    refused = _flags_named_in_messages(tree)
    orphaned = sorted(dests - honoured - refused)
    assert orphaned == [], (
        f"these flags are accepted on a --verify run and silently ignored: {orphaned}"
    )
    # Non-vacuous in both directions: the sets must not be empty, or a module
    # with no flags at all would pass.
    assert dests and honoured & dests and refused & dests


def test_only_host_matches_the_site_root_row(tmp_path) -> None:
    """canonicalize strips a bare `/`, so `https://example.com` has an EMPTY
    path and a pattern of `https://example.com/%` never matched it: a scoped
    re-run reported complete while skipping every site root. 191 of 5,133
    live rows were site roots when this was written. The two control rows are
    what keep this a boundary and not a prefix."""
    db = _seed(
        tmp_path / "i.db",
        [
            "https://example.com",
            "https://example.com?q=1",
            "https://www.example.com",
            "https://example.com/x",
            "https://notexample.com",  # control: substring, not boundary
            "https://example.com.evil.test",  # control: suffix, not boundary
        ],
    )
    seen: list[str] = []
    verify(db, hasher=_recording_hasher(seen), clock=lambda: "NOW", only_host="example.com")
    assert sorted(seen) == [
        "https://example.com",
        "https://example.com/x",
        "https://example.com?q=1",
        "https://www.example.com",
    ]


# --- a negative limit is a mistake, not the whole corpus ----------------------


import pytest  # noqa: E402

from zotero_capture.sqlite_cache import (  # noqa: E402
    enqueue_retry,
    retry_queue_entries,
    rows_needing_hash,
    rows_with_hash,
)


def _two_of_each(tmp_path):
    db = _seed(tmp_path / "i.db", ["https://a.test/1", "https://b.test/1"])
    # rows_needing_hash wants UNhashed rows; give it two of those as well
    insert_url(db, "https://c.test/1", "K8", datetime.date(2026, 5, 5))
    insert_url(db, "https://d.test/1", "K9", datetime.date(2026, 5, 5))
    for i in range(2):
        enqueue_retry(db, url_canonical=f"https://q.test/{i}", project="home", context=None,
                      seen_date="2026-05-05", error="e", now="2026-05-05T00:00:00+00:00")
    return db


@pytest.mark.parametrize("fn", [rows_needing_hash, rows_with_hash, retry_queue_entries])
def test_a_negative_limit_is_refused_not_unlimited(tmp_path, fn) -> None:
    """SQLite reads `LIMIT -1` as no limit at all. `--limit -1` is what
    someone types to be careful, and it ran the whole corpus. Refused at the
    one place the SQL is built, so no CLI can forget to."""
    db = _two_of_each(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        fn(db, limit=-1)
    # Positive controls: 0 means none, 1 means one, None means everything.
    assert len(fn(db, limit=0)) == 0
    assert len(fn(db, limit=1)) == 1
    assert len(fn(db, limit=None)) == 2


def test_a_negative_limit_is_refused_at_the_cli(tmp_path) -> None:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("ZOTERO_")}
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path)
    for flags in (["--limit", "-1"], ["--verify", "--limit", "-1"]):
        done = subprocess.run(
            [sys.executable, str(CLI), *flags],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert done.returncode != 0, flags
        assert "limit" in done.stderr, done.stderr
