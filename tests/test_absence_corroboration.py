"""A 404 to an anonymous request is not proof the page is gone.

A status code is a fact about THIS REQUESTER's view of a resource, not about the
resource. We fetch with no credentials, so 404 confounds "no longer there" with
"there, and not visible to you" -- and hosts answer the second with the first on
purpose. GitHub does it for private repositories so it does not leak which ones
exist, and 219 rows in the live index recorded `gone` for the owner's own private
pull requests. Every one returns 200 and a real title to an authenticated
request; the citations were never dead.

The discriminator is containment, needs no credentials, and costs one request.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    GONE,
    NOT_VISIBLE,
    absence_is_corroborated,
    page_is_visible,
    parent_url,
    snapshot,
)
from zotero_capture.sqlite_cache import init_db, insert_url, row_for_url

LEAF = "https://forge.invalid/owner/repo/pull/3"
PARENT = "https://forge.invalid/owner/repo/pull"
SEEN = date(2026, 5, 5)


class _Stamper:
    def record_content_hash(self, item_key, digest, *, expect_url=None):
        return True


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


# --- the path arithmetic -----------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (LEAF, PARENT),
        (LEAF + "/", PARENT),
        ("https://h.invalid/a/b?x=1#f", "https://h.invalid/a"),
        # One segment or none: the parent would be the site root, and a site root
        # that answers says nothing about a page beneath it -- github.com/ is up
        # for everybody.
        ("https://h.invalid/only", None),
        ("https://h.invalid/", None),
        ("https://h.invalid", None),
    ],
)
def test_parent_url(url: str, expected: str | None) -> None:
    assert parent_url(url) == expected


# --- the discriminator -------------------------------------------------------


def test_a_missing_leaf_under_a_visible_parent_is_really_gone() -> None:
    assert absence_is_corroborated(LEAF, visible=lambda u: True) is True


def test_a_missing_leaf_under_an_invisible_parent_is_not_proof() -> None:
    assert absence_is_corroborated(LEAF, visible=lambda u: False) is False


def test_only_the_IMMEDIATE_parent_is_consulted() -> None:
    """The subtlety, measured on the real thing before it was written. For the
    private repo, `/musharna` answers 200 because a user profile is public, so a
    rule that walked up to the topmost reachable ancestor would have called the
    absence corroborated and re-made the same false claim."""
    asked: list[str] = []

    def visible(url: str) -> bool:
        asked.append(url)
        return url == "https://forge.invalid/owner"  # grandparent is public

    assert absence_is_corroborated(LEAF, visible=visible) is False
    assert asked == [PARENT]


def test_a_url_with_no_parent_keeps_its_absence() -> None:
    """Nothing left to ask. Refusing to ever say `gone` for a site root would
    throw away the real finding to avoid a rarer one."""
    assert absence_is_corroborated("https://h.invalid/only", visible=lambda u: False) is True


# --- what "visible" means ----------------------------------------------------


def _client(status: int) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(status)),
        follow_redirects=True,
    )


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, True),
        (404, False),
        (410, False),
        # We did not SEE absence. A 403 says the opposite -- something is there.
        (403, True),
        (500, True),
    ],
)
def test_page_is_visible(status: int, expected: bool) -> None:
    with _client(status) as http:
        assert page_is_visible(LEAF, client=http) is expected


def test_a_probe_that_cannot_connect_is_not_evidence_of_anything() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with httpx.Client(transport=httpx.MockTransport(boom)) as http:
        assert page_is_visible(LEAF, client=http) is False


# --- end to end --------------------------------------------------------------


def _four_oh_four(url: str):
    raise httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", url),
        response=httpx.Response(404, request=httpx.Request("GET", url)),
    )


def test_a_private_page_is_not_recorded_as_gone(db: Path) -> None:
    """THE defect: 219 rows asserting the owner's own private work no longer
    exists."""
    insert_url(db, LEAF, "KEY1", SEEN)

    snapshot(
        db, zotero=_Stamper(), hasher=_four_oh_four, clock=lambda: "NOW",
        visible=lambda u: False,
    )

    assert row_for_url(db, LEAF)["last_outcome"] == NOT_VISIBLE


def test_a_genuinely_dead_page_is_still_recorded_as_gone(db: Path) -> None:
    """The positive control, and it is the important one: a change that simply
    stopped saying `gone` would satisfy the test above completely while
    destroying the finding the whole subsystem exists to report."""
    insert_url(db, LEAF, "KEY1", SEEN)

    snapshot(
        db, zotero=_Stamper(), hasher=_four_oh_four, clock=lambda: "NOW",
        visible=lambda u: True,
    )

    assert row_for_url(db, LEAF)["last_outcome"] == GONE


def test_without_a_prober_absence_is_not_claimed(db: Path) -> None:
    """The default has to fail toward the weaker claim. Absence is the thing we
    would be inventing, so a caller that forgets to corroborate must not get to
    assert it."""
    insert_url(db, LEAF, "KEY1", SEEN)

    snapshot(db, zotero=_Stamper(), hasher=_four_oh_four, clock=lambda: "NOW")

    assert row_for_url(db, LEAF)["last_outcome"] == NOT_VISIBLE


def test_not_visible_is_not_listed_among_the_dead_links(db: Path) -> None:
    insert_url(db, LEAF, "KEY1", SEEN)

    result = snapshot(
        db, zotero=_Stamper(), hasher=_four_oh_four, clock=lambda: "NOW",
        visible=lambda u: False,
    )

    assert result.gone_at == {}
    assert result.refused_by == {}


def test_a_url_with_no_parent_keeps_gone_even_without_a_prober(db: Path) -> None:
    """The distinction the existing suite caught: "no prober" is not the same as
    "not corroborated". A single-segment URL is decided without ever consulting
    the prober, so its absence does not become unprovable just because one was
    not supplied."""
    rootish = "https://forge.invalid/only"
    insert_url(db, rootish, "KEY1", SEEN)

    snapshot(db, zotero=_Stamper(), hasher=_four_oh_four, clock=lambda: "NOW")

    assert row_for_url(db, rootish)["last_outcome"] == GONE


# --- the outcome filter, and the flag reaching every path --------------------


def test_only_outcome_selects_just_those_rows(db: Path) -> None:
    from zotero_capture.sqlite_cache import rows_needing_hash, set_fetch_outcome

    for name, outcome in (("gone", GONE), ("blocked", "blocked"), ("fresh", "")):
        url = f"https://h.invalid/{name}/x"
        insert_url(db, url, f"K{name}", SEEN)
        if outcome:
            set_fetch_outcome(db, url, outcome=outcome, at="NOW", final_url=url)

    picked = [r["url_canonical"] for r in rows_needing_hash(db, only_outcome=GONE)]

    assert picked == ["https://h.invalid/gone/x"]


def test_only_outcome_is_parameterised_not_interpolated(db: Path) -> None:
    """It reaches the CLI surface. A quote in the value must not become SQL."""
    from zotero_capture.sqlite_cache import rows_needing_hash

    insert_url(db, "https://h.invalid/a/b", "K1", SEEN)

    assert rows_needing_hash(db, only_outcome="' OR 1=1 --") == []


def test_every_snapshot_call_in_the_cli_passes_the_filter() -> None:
    """The `--sleep` defect, reproduced while writing this feature: the flag was
    threaded into the real call and not the dry-run one, so `--dry-run
    --only-outcome gone` silently reported the unattempted set instead. Both ends
    looked complete; only the gap was wrong. Derived from the source rather than
    from the call sites I happened to remember."""
    import ast

    source = Path("scripts/snapshot_pages.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "snapshot"
    ]

    assert len(calls) >= 2, "positive control: the discovery found the call sites"
    for call in calls:
        assert "only_outcome" in {kw.arg for kw in call.keywords}, (
            f"snapshot() call at line {call.lineno} does not pass only_outcome"
        )
