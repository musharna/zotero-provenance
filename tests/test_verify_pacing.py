"""`--sleep` reached snapshot and not verify, so a verify pass had no manners.

`snapshot_pages.py --verify --sleep 2` parses, and the delay is applied to
exactly nothing: the verify branch calls `verify(db_path, hasher=..., limit=...)`
and `verify()` had no pacing parameter at all. A full pass is 3,744 pages,
including hosts that are already rate-limiting us, fetched back to back.

This is the FOURTH appearance of one defect class in this repository: a flag the
CLI accepts, describes in its help text, and never delivers. Neither end looks
wrong on its own -- the parser is complete, the function is complete -- and the
defect lives only in the gap, which is why review misses it.

A guard for it already existed and did not fire, and the reason is the finding.
It derived its FLAGS by matching the literal prefix `--only-`, and its CONSUMERS
by matching the literal name `snapshot`. Both are hand-written lists wearing the
costume of a derivation, so it could not fail on a flag not called `--only-*`
reaching a function not called `snapshot`. That is the same defect in a guard
that the guard exists to prevent in the code -- exactly what the User-Agent guard
did by naming one test per known call site.

So the consumer set is derived from BEHAVIOUR instead: a function that calls
`hasher(...)` makes outbound requests, therefore it must accept pacing, and every
CLI call to it must pass it. Renaming a function, or adding a third fetching
pass, cannot slip past that.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from zotero_capture import snapshot as snapshot_mod

CLI = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_pages.py"

# The parameter a fetching function must take. `--sleep` is the CLI's name for
# it; `sleep_s` is the module's.
PACING_PARAM = "sleep_s"
PACING_FLAG = "sleep"


def _functions_that_fetch() -> list[str]:
    """Every top-level function in snapshot.py that calls `hasher(...)`.

    Derived from what the function DOES, not from what it is called. This is the
    part the previous guard got wrong: it matched the name `snapshot`, so a
    second fetching pass named anything else was invisible to it.
    """
    tree = ast.parse(Path(snapshot_mod.__file__).read_text())
    out = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hasher"
            ):
                out.append(fn.name)
                break
    return out


def test_the_derivation_finds_more_than_one_fetching_pass() -> None:
    """Positive control. If this found only `snapshot`, every assertion below
    would pass while saying nothing about `verify` -- which is precisely how the
    original guard read as green."""
    found = _functions_that_fetch()
    assert "snapshot" in found
    assert "verify" in found, (
        "the derivation missed a fetching pass; the guard would be vacuous"
    )


@pytest.mark.parametrize("name", _functions_that_fetch())
def test_every_fetching_pass_accepts_pacing(name: str) -> None:
    """A function that makes outbound requests must be able to space them.

    `verify` could not. The flag existed, the help text promised politeness, and
    3,744 refetches would have gone out with no delay.
    """
    params = inspect.signature(getattr(snapshot_mod, name)).parameters
    assert PACING_PARAM in params, (
        f"{name}() fetches pages but cannot be paced; --sleep can only be "
        f"honoured by a function that accepts it"
    )


@pytest.mark.parametrize("name", _functions_that_fetch())
def test_every_cli_call_site_passes_pacing(name: str) -> None:
    """...and the CLI must actually hand it over. Accepting a parameter nobody
    passes is the same dead flag one layer along."""
    tree = ast.parse(CLI.read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]
    assert calls, f"positive control: no call to {name}() found in the CLI"
    for call in calls:
        passed = {kw.arg for kw in call.keywords}
        assert PACING_PARAM in passed, (
            f"{name}() at snapshot_pages.py:{call.lineno} is called without "
            f"{PACING_PARAM}; --{PACING_FLAG} is parsed and then dropped"
        )


def test_the_cli_still_defines_the_flag() -> None:
    """The other direction: a guard that passed because the flag was deleted
    would be worse than the bug."""
    tree = ast.parse(CLI.read_text())
    flags = {
        str(a.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_argument"
        for a in n.args
        if isinstance(a, ast.Constant)
    }
    assert f"--{PACING_FLAG}" in flags


# --- behaviour ----------------------------------------------------------------


def _seed(db, rows):
    from zotero_capture.sqlite_cache import init_db, insert_url, set_content_hash
    import datetime

    init_db(db)
    for i, url in enumerate(rows):
        insert_url(db, url, f"K{i}", datetime.date(2026, 5, 5))
        set_content_hash(
            db, url, content_hash="OLD", hashed_at="THEN",
            covers_bytes=64, complete=True,
        )
    return db


def test_verify_spaces_requests_to_the_same_host(tmp_db) -> None:
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, [
        "https://a.test/1", "https://a.test/2", "https://a.test/3",
    ])
    slept: list[float] = []
    clock = [0.0]

    def monotonic() -> float:
        return clock[0]

    def sleeper(s: float) -> None:
        slept.append(s)
        clock[0] += s

    verify(db, hasher=lambda u, n: PageRead(
               digest="OLD", final_url=u, covers_bytes=64, complete=True),
           sleep_s=2.0, sleeper=sleeper, monotonic=monotonic)

    assert slept == [2.0, 2.0], "same-host requests were not spaced"


def test_verify_does_not_space_requests_to_DIFFERENT_hosts(tmp_db) -> None:
    """The test that carries the property. The one above passes under a naive
    per-REQUEST delay too; only this one distinguishes per-host pacing from
    sleeping between every fetch, and getting that wrong makes a 3,744-page
    pass take hours longer for no politeness gain."""
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, [
        "https://a.test/1", "https://b.test/1", "https://c.test/1",
    ])
    slept: list[float] = []

    verify(db, hasher=lambda u, n: PageRead(
               digest="OLD", final_url=u, covers_bytes=64, complete=True),
           sleep_s=2.0, sleeper=lambda s: slept.append(s), monotonic=lambda: 0.0)

    assert slept == [], "delayed between different hosts for no reason"


def test_verify_reports_WHY_a_page_could_not_be_re_read(tmp_db) -> None:
    """A paywall and a dead citation are different findings. Collapsing both
    into "unreachable" is the conflation 0.36.0 removed from snapshot, and a
    verify pass over this corpus is mostly paywalls."""
    import httpx
    from zotero_capture.snapshot import verify

    db = _seed(tmp_db, ["https://a.test/1"])

    def refuse(url: str, max_bytes: int = 0):
        raise httpx.HTTPStatusError(
            "403",
            request=httpx.Request("GET", url),
            response=httpx.Response(403, request=httpx.Request("GET", url)),
        )

    result = verify(db, hasher=refuse)
    assert result.unreachable == 1
    assert result.by_outcome == {"blocked": 1}, result.by_outcome


def test_a_changed_page_keeps_its_original_hash(tmp_db) -> None:
    """The core promise, pinned: the stored hash is the record of what was
    consulted. Overwriting it with what the page says today would destroy the
    finding at the moment it was made."""
    from zotero_capture.snapshot import PageRead, verify
    from zotero_capture.sqlite_cache import row_for_url

    db = _seed(tmp_db, ["https://a.test/1"])
    result = verify(
        db,
        hasher=lambda u, n: PageRead(
            digest="NEW", final_url=u, covers_bytes=64, complete=True),
    )

    assert result.changed == 1
    assert row_for_url(db, "https://a.test/1")["content_hash"] == "OLD"


# --- a change must be corroborated before it is reported ----------------------


def test_a_page_that_differs_from_ITSELF_is_not_reported_as_changed(tmp_db) -> None:
    """MEASURED, not hypothesised: 5 of 8 sampled pages produced a different
    digest when read twice THREE SECONDS apart. Nonces, ad tokens, build ids and
    timestamps all move; none of them is the source changing.

    A first verify sample called 29 of 60 pages CHANGED over five days. Without
    this check that headline counts byte-instability as provenance drift, which
    is the same error as reading a 404 as absence: an affirmative claim from a
    single observation that cannot support it.
    """
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, ["https://a.test/1"])
    reads = iter(["NEW1", "NEW2"])  # the page never reads the same way twice

    result = verify(
        db,
        hasher=lambda u, n: PageRead(
            digest=next(reads), final_url=u, covers_bytes=64, complete=True),
    )

    assert result.changed == 0, "an unstable page was reported as changed"
    assert result.unstable == 1


def test_a_stable_page_that_really_moved_is_still_reported(tmp_db) -> None:
    """The positive control, and the one that matters most. A fix that simply
    stopped saying CHANGED would pass the test above while destroying the only
    finding this pass exists to produce."""
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, ["https://a.test/1"])
    result = verify(
        db,
        hasher=lambda u, n: PageRead(
            digest="NEW", final_url=u, covers_bytes=64, complete=True),
    )

    assert result.changed == 1
    assert result.unstable == 0


def test_an_unchanged_page_is_not_re_read(tmp_db) -> None:
    """Corroboration costs a second request, so it must be spent only on
    candidates. Re-reading all 3,744 rows twice would double a pass that is
    already thousands of requests."""
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, ["https://a.test/1"])
    seen: list[str] = []

    def hasher(u: str, max_bytes: int = 0):
        seen.append(u)
        return PageRead(digest="OLD", final_url=u, covers_bytes=64, complete=True)

    verify(db, hasher=hasher)
    assert seen == ["https://a.test/1"], "spent a second fetch on an unchanged page"


def test_the_corroborating_read_is_paced_too(tmp_db) -> None:
    """It is a second request to the SAME host we just fetched, which is the
    case pacing exists for."""
    from zotero_capture.snapshot import PageRead, verify

    db = _seed(tmp_db, ["https://a.test/1"])
    reads = iter(["NEW1", "NEW2"])
    slept: list[float] = []

    verify(db, hasher=lambda u, n: PageRead(
               digest=next(reads), final_url=u, covers_bytes=64, complete=True),
           sleep_s=2.0, sleeper=lambda s: slept.append(s), monotonic=lambda: 0.0)

    assert slept == [2.0], "the corroborating re-read skipped the host delay"
