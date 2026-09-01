"""A 404 to a URL we mangled is a fact about our record, not about the source.

Until 2026-08-21 the tokenizer was a character blacklist:

    URL_RE = re.compile(r"https?://[^\\s<>\\"'`\\)\\]]+", re.IGNORECASE)

`)` and `]` are legal URL characters, so every cited address containing one was
stored cut off at it. Commit 1d46bd5 fixed the tokenizer, but nothing repaired
what it had already written: 34 rows in the live index still hold a prefix of the
address that was actually cited, and the newest of them is dated the day of the
fix.

That would be merely untidy if the rows sat still. They do not. `snapshot` fetches
them, the truncated address 404s, and the row is stamped `gone` -- an affirmative
claim that a source no longer exists, manufactured entirely out of our own
damage. Measured on the live index, every one of these resolves when repaired:

    stored   .../wiki/Aestivation_(botany     404
    repaired .../wiki/Aestivation_(botany)    200

This is the same doctrine as `absence_is_corroborated`, one step further back. It
asks whether we are entitled to read a 404 as absence; this asks whether we ever
asked the right question at all. A URL whose brackets do not balance may be a
prefix of the one that was cited, and a prefix is not the address.

The suspicion is deliberately not treated as proof. `(` is a legal sub-delimiter,
so `https://example.org/a(b` is a real address that this predicate calls suspect.
Being wrong there costs one downgraded claim; being wrong the other way invents
link rot, which is the exact finding this tool exists to report truthfully.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import httpx
import pytest

from zotero_capture.repair import repaired_url
from zotero_capture.snapshot import (
    GONE,
    MALFORMED,
    NOT_VISIBLE,
    snapshot,
)
from zotero_capture.sqlite_cache import init_db, insert_url, row_for_url
from zotero_capture.url_processing import unbalanced_brackets

SEEN = date(2026, 5, 5)

# The real shapes, taken from the live index rather than invented.
CUT_AT_PAREN = "https://forge.invalid/wiki/Aestivation_(botany"
CUT_AT_BRACKET = "https://forge.invalid/w/[genus-prefix"
INTACT = "https://forge.invalid/wiki/Aestivation_(botany)"


class _Stamper:
    def record_content_hash(self, item_key, digest, *, expect_url=None):
        return True


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


def _four_oh_four(url: str, max_bytes: int = 0):
    raise httpx.HTTPStatusError(
        "404",
        request=httpx.Request("GET", url),
        response=httpx.Response(404, request=httpx.Request("GET", url)),
    )


# --- the predicate -----------------------------------------------------------


@pytest.mark.parametrize(
    "url,suspect",
    [
        # What the blacklist actually produced, verbatim from the live index.
        ("https://en.wikipedia.org/wiki/Aestivation_(botany", True),
        ("https://www.cell.com/cell/fulltext/S0092-8674(16", True),
        ("https://doi.org/10.1016/0022-2836(70", True),
        ("https://www.orchidspecies.com/[genus-prefix", True),
        # ... and the addresses those were cut out of, which must NOT be suspect.
        # Without this half the predicate could return True for everything.
        ("https://en.wikipedia.org/wiki/Aestivation_(botany)", False),
        ("https://www.cell.com/cell/fulltext/S0092-8674(16)30820-X", False),
        ("https://doi.org/10.1016/0022-2836(70)90057-4", False),
        ("https://www.orchidspecies.com/[genus-prefix]", False),
        # Ordinary addresses, which are the overwhelming majority of the corpus.
        ("https://example.org/path?a=1#frag", False),
        ("https://example.org/", False),
        # Nesting has to be counted, not merely paired: a closer before its
        # opener is still damage, and "any ( and any )" would call this clean.
        ("https://example.org/a)b(c", True),
        ("https://example.org/a(b(c)d)e", False),
    ],
)
def test_unbalanced_brackets(url: str, suspect: bool) -> None:
    assert unbalanced_brackets(url) is suspect


def test_a_legal_unbalanced_paren_is_called_suspect_on_purpose() -> None:
    """Stated as a test so the false positive is a decision, not a surprise.

    `(` is a legal sub-delimiter and this address is real. Calling it suspect
    costs one downgraded claim about one source; the opposite error prints
    link rot that never happened.
    """
    assert unbalanced_brackets("https://example.org/a(b") is True


# --- what it gates -----------------------------------------------------------


def test_a_404_on_a_truncated_url_is_not_recorded_as_gone(db: Path) -> None:
    """THE defect: rows asserting a source is dead because we mangled its
    address."""
    insert_url(db, CUT_AT_PAREN, "KEY1", SEEN)

    snapshot(
        db,
        zotero=_Stamper(),
        hasher=_four_oh_four,
        clock=lambda: "NOW",
        visible=lambda u: True,
    )

    assert row_for_url(db, CUT_AT_PAREN)["last_outcome"] == MALFORMED


def test_a_404_on_an_intact_url_is_still_recorded_as_gone(db: Path) -> None:
    """The positive control, and the important one. A change that simply stopped
    saying `gone` would pass the test above while destroying the finding the
    whole subsystem exists to report."""
    insert_url(db, INTACT, "KEY1", SEEN)

    snapshot(
        db,
        zotero=_Stamper(),
        hasher=_four_oh_four,
        clock=lambda: "NOW",
        visible=lambda u: True,
    )

    assert row_for_url(db, INTACT)["last_outcome"] == GONE


def test_a_truncated_url_is_not_probed_for_containment(db: Path) -> None:
    """No request is spent asking whether the parent of a mangled address is
    visible. The answer could not mean anything, and it is a second request to a
    host we already bothered with a question we made up.
    """
    insert_url(db, CUT_AT_BRACKET, "KEY1", SEEN)
    probed: list[str] = []

    snapshot(
        db,
        zotero=_Stamper(),
        hasher=_four_oh_four,
        clock=lambda: "NOW",
        visible=lambda u: probed.append(u) or True,
    )

    assert probed == []
    assert row_for_url(db, CUT_AT_BRACKET)["last_outcome"] == MALFORMED


def test_malformed_outranks_not_visible(db: Path) -> None:
    """Both downgrades apply to the same 404, and they say different things.
    `not_visible` is about the SOURCE's permissions; `malformed` is about OUR
    record. When the address is damaged we never learned anything about the
    source at all, so that is the finding to keep."""
    insert_url(db, CUT_AT_PAREN, "KEY1", SEEN)

    snapshot(
        db,
        zotero=_Stamper(),
        hasher=_four_oh_four,
        clock=lambda: "NOW",
        visible=lambda u: False,
    )

    row = row_for_url(db, CUT_AT_PAREN)
    assert row["last_outcome"] == MALFORMED
    assert row["last_outcome"] != NOT_VISIBLE


def test_a_malformed_row_is_not_listed_among_the_dead_links(db: Path) -> None:
    insert_url(db, CUT_AT_PAREN, "KEY1", SEEN)

    result = snapshot(
        db,
        zotero=_Stamper(),
        hasher=_four_oh_four,
        clock=lambda: "NOW",
        visible=lambda u: True,
    )

    assert result.gone_at == {}
    assert result.refused_by == {}


# --- what repair must NOT do -------------------------------------------------


def test_repair_refuses_to_invent_the_missing_closer() -> None:
    """Pinned so nobody "completes" the paren rule by appending one.

    `repair` works from the stored string alone, and the string does not carry
    what was cut. Appending ")" to ".../S0092-8674(16" yields ".../S0092-8674(16)",
    a DIFFERENT address that is not the cited one -- and for
    ".../Moneymaker_tomato_plant_(Solanum_lycopersicum" the guess 404s live while
    the real address, recovered from the transcript, returns 200. A repair that
    guesses would have written the wrong URL and reported success.
    """
    for stored in (
        "https://www.cell.com/cell/fulltext/S0092-8674(16",
        "https://en.wikipedia.org/wiki/Aestivation_(botany",
        "https://www.orchidspecies.com/[genus-prefix",
    ):
        assert repaired_url(stored) == ""


def test_the_paren_rule_in_repair_says_which_direction_it_handles() -> None:
    """The docstring said "re-balance a paren" while the code only ever removed
    an EXCESS closer. That reading is why 34 damaged rows survived every cleanup
    pass: the tool built to fix URL damage reported nothing to recover for the
    entire class, and its own description implied it had looked.
    """
    from zotero_capture import repair

    doc = ast.get_docstring(
        next(
            n
            for n in ast.walk(ast.parse(Path(repair.__file__).read_text()))
            if isinstance(n, ast.FunctionDef) and n.name == "correct_url"
        )
    )
    assert doc is not None
    assert "excess" in doc.lower(), (
        "correct_url only removes a closing paren it has too many of; the "
        "docstring must not imply it can restore a missing one"
    )
