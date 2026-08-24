"""Retirement removes rows that can never be a source — and nothing else.

This is the destructive pass, so the tests are weighted towards what it must
REFUSE. A predicate that over-fires here trashes a real citation, and the index
is the only record that the citation was ever made.

The reason to have it at all: the repair pass correctly skips a row it cannot
correct, but skipping leaves the item in the library forever. 32 rows in the
live index are templates or control-byte junk, and another 62 are addresses
today's exclusion rules would refuse outright — fixtures, infrastructure, badge
assets — captured before those rules existed.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from zotero_capture.retire import apply_retire, plan_retire, retire_reason
from zotero_capture.sqlite_cache import init_db


def _row(url: str, key: str = "KEY1") -> dict:
    return {"url_canonical": url, "zotero_key": key}


@pytest.mark.parametrize(
    "url",
    [
        "https://files.rcsb.org/download/{ID}.pdb",
        "https://atted.jp/api/coex/Ath-u/{locus}/{top_n",
        "https://ftp.ebi.ac.uk/pub/${VERSION}/interproscan.tar.gz",
        "https://sketchfab.com/3d-models/{asset[",
    ],
)
def test_a_template_is_retired(url):
    assert retire_reason(url) == "template placeholder, not an address"


def test_a_control_byte_with_nothing_in_front_of_it_is_retired():
    """Cutting at the escape leaves "https://" — no host, nothing to repair."""
    assert retire_reason("https://\x1b[0m") == (
        "control character, from pasted terminal output"
    )


def test_a_control_byte_after_a_real_address_goes_to_repair_instead():
    """The interaction that decides whether a citation survives.

    "https://sqlalche.me/e/20/e3q8" is a real page with an ANSI reset stuck to
    it. Retirement stands down because repair can recover the address; running
    the passes in the wrong order, or letting each judge alone, would trash it.
    """
    assert retire_reason("https://sqlalche.me/e/20/e3q8\x1b[0m\x1b[4;94m") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://cited.example",  # RFC 2606 fixture name
        "http://evil.example.com/creativecommons.org",  # another project's fixture
        "https://cloudflare-dns.com/dns-query",  # infrastructure
        "https://codecov.io/gh/o/r/badge.svg",  # an asset, not a document
    ],
)
def test_an_address_todays_rules_refuse_is_retired(url):
    assert retire_reason(url).startswith("an address today's rules refuse")


# --- what it must refuse: the half that keeps this pass safe ---


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.1016/s0092-8674(00)80876-3",
        "https://en.wikipedia.org/wiki/Aestivation_(botany)",
        "https://de.wikipedia.org/wiki/München",
        "https://example.org.uk/a/b",  # NOT the reserved example.org
        "https://foo.example.io/release_",
        "https://github.com/musharna/figcite",
    ],
)
def test_a_real_citation_is_left_alone(url):
    """Positive control. Without this the suite passes on a predicate that
    retires everything, which would empty the library."""
    assert retire_reason(url) == ""


def test_a_regex_row_is_retired_for_its_host_not_its_asterisk():
    """The "*" is not the reason, and that distinction is the safeguard.

    "*" is a legal sub-delimiter, so a rule keyed on it would also retire real
    URLs. These rows qualify on the address test instead: "data\\.gramene\\.org"
    is not a hostname any resolver could look up, and ".example" is reserved.
    Repair refuses the same rows for a different and equally correct reason —
    there is no correction to make that would not invent an address.
    """
    assert retire_reason(r"https://data\.gramene\.org/v69/genes.*").startswith(
        "an address today's rules refuse"
    )
    assert retire_reason("https://lepanthes.example/lepanthes*.htm").startswith(
        "an address today's rules refuse"
    )


def test_a_real_url_containing_an_asterisk_is_kept():
    """The control for the test above: the asterisk itself decides nothing.

    The host has to be a registrable one — "example.org" is reserved by RFC
    2606, so writing the control that way would have passed for the wrong
    reason.
    """
    assert retire_reason("https://files.plantcad.io/glob/a*b.txt") == ""


def test_a_reserved_wildcard_host_is_retired_for_the_name_not_the_star():
    """ "*.example.com" goes, but because example.com is reserved."""
    assert retire_reason("https://*.example.com/*").startswith(
        "an address today's rules refuse"
    )


# --- applying a plan ---


class _FakeZotero:
    def __init__(self):
        self.trashed: list[str] = []

    def trash_item(self, key: str) -> None:
        self.trashed.append(key)


def test_apply_trashes_the_item_and_drops_the_row(tmp_path):
    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    with closing(connect(db)) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES (?, ?, '2026-01-01', '2026-01-01')",
            ("https://files.rcsb.org/download/{ID}.pdb", "JUNKKEY1"),
        )
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES (?, ?, '2026-01-01', '2026-01-01')",
            ("https://example.org.uk/real", "GOODKEY1"),
        )
        rows = [dict(r) for r in conn.execute("SELECT * FROM url_index")]

    steps = plan_retire(rows)
    assert [s.zotero_key for s in steps] == ["JUNKKEY1"]

    zotero = _FakeZotero()
    counts = apply_retire(steps, db_path=db, zotero=zotero, connect=connect)
    assert counts == {"trashed": 1, "row_only": 0, "failed": 0}
    assert zotero.trashed == ["JUNKKEY1"]

    with closing(connect(db)) as conn:
        surviving = [r[0] for r in conn.execute("SELECT url_canonical FROM url_index")]
    # The negative assertion and the positive one in the same test: the junk is
    # gone AND the real row is still there. Either alone reads as success on a
    # broken pass.
    assert surviving == ["https://example.org.uk/real"]


def test_a_row_whose_claim_never_completed_is_dropped_without_a_zotero_call(tmp_path):
    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    steps = plan_retire([_row("https://a.example/{X}", key="")])
    zotero = _FakeZotero()
    counts = apply_retire(steps, db_path=db, zotero=zotero, connect=connect)
    assert counts == {"trashed": 0, "row_only": 1, "failed": 0}
    assert zotero.trashed == []
