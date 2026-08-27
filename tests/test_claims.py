"""A captured URL, and what it was cited FOR — kept on this machine.

The library says a source was consulted. It cannot say what for, and that is the
question an audit of your own bibliography actually asks. Wave 6 records the
sentence around each captured URL.

**The privacy property is the point of this file.** The claim is a fragment of a
conversation and the Zotero library SYNCS, so the design chosen was local-only:
the text goes into the sqlite index and nowhere else. That property cannot rest
on "no module here imports the Zotero client", because the next person to touch
this code would have to know that. So it is asserted the only way that survives a
future edit: run a real capture and check every outbound payload.

A privacy test that passes because nothing was recorded is worthless, so the
positive control lives inside the same test.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import capture_message
from zotero_capture.claims import (
    CLAIM_MAX_CHARS,
    CLAIMS_PER_URL,
    claim_counts,
    claim_for,
    claims_for_url,
    record_claim,
    search_claims,
)
from zotero_capture.sqlite_cache import init_db

URL = "https://fixturehost.org/paper"


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    return tmp_db


@pytest.fixture
def fake_zotero():
    z = MagicMock()
    z.post_webpage_item.return_value = "NEWKEY"
    z.add_tags.return_value = True
    return z


@pytest.fixture
def fake_title_fetcher():
    return lambda url: f"Title-of-{url}"


# --- what the sentence is ---------------------------------------------------


def test_the_sentence_around_the_url_is_the_claim() -> None:
    message = f"Ants use pheromone gradients to recruit nestmates, per {URL}."
    assert claim_for(message, URL).startswith("Ants use pheromone gradients")


def test_only_the_containing_sentence_is_kept() -> None:
    """The paragraph is not the claim. A neighbouring sentence is a different
    assertion, and folding it in would attribute it to this source."""
    message = f"Rats do not do this. Ants recruit nestmates, per {URL}. Bees differ."
    claim = claim_for(message, URL)
    assert "Ants recruit nestmates" in claim
    assert "Rats" not in claim and "Bees" not in claim


def test_a_markdown_link_keeps_its_label() -> None:
    message = f"The [recruitment study]({URL}) measured trail fidelity."
    claim = claim_for(message, URL)
    assert "recruitment study" in claim and "trail fidelity" in claim


def test_a_bullet_is_its_own_claim() -> None:
    message = f"Findings:\n- Trail fidelity rises with colony size, {URL}\n- Other.\n"
    claim = claim_for(message, URL)
    assert "Trail fidelity rises" in claim
    assert "Findings" not in claim and "Other" not in claim


def test_a_url_alone_on_a_line_has_no_claim() -> None:
    """Storing the bare URL back as its own justification would answer the
    question with the question."""
    assert claim_for(f"Sources:\n{URL}\n", URL) == ""


def test_the_url_is_not_split_at_its_own_dots() -> None:
    """Positive control for the scanning rule: a URL is full of `.` and would
    otherwise be read as ending a sentence partway through itself."""
    url = "https://sub.host.example.net/a.b.c/paper.html"
    message = f"Colony size predicts fidelity, see {url} for the dataset."
    claim = claim_for(message, url)
    assert "Colony size predicts fidelity" in claim
    assert "for the dataset" in claim


def test_a_url_that_is_not_in_the_message_yields_nothing() -> None:
    assert claim_for("No links here.", URL) == ""


def test_a_long_sentence_is_truncated_and_says_so() -> None:
    filler = "colony recruitment dynamics " * 40
    message = f"{filler}{URL} closes the argument."
    claim = claim_for(message, URL)
    assert len(claim) <= CLAIM_MAX_CHARS + 4
    assert claim.endswith("[…]")


def test_a_short_sentence_is_not_marked_as_truncated() -> None:
    """Positive control for the test above: the marker must mean something."""
    assert not claim_for(f"Short one, {URL}.", URL).endswith("[…]")


# --- storage ----------------------------------------------------------------


def _record(db: Path, claim: str, *, url: str = URL, now: str = "2026-08-27T10:00:00"):
    return record_claim(
        db,
        url_canonical=url,
        claim=claim,
        project="home",
        context="general",
        origin="assistant",
        now=now,
    )


def test_a_claim_can_be_read_back(db: Path) -> None:
    assert _record(db, "Ants recruit nestmates.") is True
    rows = claims_for_url(db, URL)
    assert len(rows) == 1
    assert rows[0]["claim"] == "Ants recruit nestmates."
    assert rows[0]["project"] == "home"


def test_the_same_claim_twice_is_one_link_seen_twice(db: Path) -> None:
    _record(db, "Ants recruit nestmates.", now="2026-08-01T00:00:00")
    _record(db, "Ants recruit nestmates.", now="2026-08-27T00:00:00")
    rows = claims_for_url(db, URL)
    assert len(rows) == 1
    assert rows[0]["times_seen"] == 2
    assert rows[0]["first_seen"].startswith("2026-08-01")
    assert rows[0]["last_seen"].startswith("2026-08-27")


def test_two_different_claims_are_two_links(db: Path) -> None:
    _record(db, "Ants recruit nestmates.")
    _record(db, "Trail fidelity rises with colony size.")
    assert len(claims_for_url(db, URL)) == 2


def test_an_empty_claim_is_not_stored(db: Path) -> None:
    assert _record(db, "") is False
    assert claims_for_url(db, URL) == []


def test_the_cap_refuses_rather_than_evicting(db: Path) -> None:
    """The retry queue makes the same choice, for the same reason: the earliest
    claim is the provenance, and a later mention is not a better record of it."""
    for i in range(CLAIMS_PER_URL):
        assert _record(db, f"Distinct claim number {i}.") is True
    assert _record(db, "One claim too many.") is False
    rows = claims_for_url(db, URL)
    assert len(rows) == CLAIMS_PER_URL
    assert any(r["claim"] == "Distinct claim number 0." for r in rows)
    assert not any(r["claim"] == "One claim too many." for r in rows)


def test_at_the_cap_an_existing_claim_still_updates(db: Path) -> None:
    """The cap bounds DISTINCT claims. A full URL that is cited again for a
    reason already on record must still have its recency updated, or the cap
    silently freezes the row's history too."""
    for i in range(CLAIMS_PER_URL):
        _record(db, f"Distinct claim number {i}.", now="2026-08-01T00:00:00")
    assert _record(db, "Distinct claim number 0.", now="2026-08-27T00:00:00") is True
    rows = [
        r for r in claims_for_url(db, URL) if r["claim"] == "Distinct claim number 0."
    ]
    assert rows[0]["times_seen"] == 2
    assert rows[0]["last_seen"].startswith("2026-08-27")


def test_the_cap_is_per_url(db: Path) -> None:
    for i in range(CLAIMS_PER_URL):
        _record(db, f"Distinct claim number {i}.")
    assert _record(db, "A claim about something else.", url="https://other.test/x")


def test_claims_are_searchable(db: Path) -> None:
    _record(db, "Trail fidelity rises with colony size.")
    _record(db, "Nest architecture is unrelated.")
    hits = search_claims(db, "fidelity")
    assert len(hits) == 1 and "fidelity" in hits[0]["claim"]


def test_counts_report_links_and_urls(db: Path) -> None:
    _record(db, "One.")
    _record(db, "Two.")
    _record(db, "Three.", url="https://other.test/x")
    assert claim_counts(db) == (3, 2)


# --- the privacy property ---------------------------------------------------


def test_claim_text_never_reaches_zotero(db, fake_zotero, fake_title_fetcher) -> None:
    """THE property of wave 6.

    Local-only was chosen over a Zotero child note precisely because the library
    syncs. Asserting it by reading imports would be a convention; this runs a
    real capture and inspects every outbound payload, so an edit that starts
    sending claim text fails here regardless of how it is written.
    """
    secret = "Ants recruit nestmates via pheromone gradients"
    result = capture_message(
        message=f"{secret}, per {URL}.",
        project_slug="home",
        context=None,
        today=date(2026, 8, 27),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )

    # Positive control: without this, the assertion below passes when nothing
    # was recorded at all -- which is the failure mode it exists to exclude.
    assert result.claims_recorded == 1
    stored = claims_for_url(db, URL)
    assert stored and secret in stored[0]["claim"]

    outbound = repr(fake_zotero.mock_calls)
    assert secret not in outbound
    for word in ("recruit", "pheromone", "nestmates"):
        assert word not in outbound
    # The URL and title legitimately do go: this is not a test that nothing
    # was sent.
    assert URL in outbound


def test_claim_text_never_becomes_a_tag(db, fake_zotero, fake_title_fetcher) -> None:
    capture_message(
        message=f"Pheromone gradients drive recruitment, per {URL}.",
        project_slug="home",
        context=None,
        today=date(2026, 8, 27),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    tags = fake_zotero.post_webpage_item.call_args.kwargs["tags"]
    assert all("heromone" not in t and "ecruitment" not in t for t in tags)
    assert any(t.startswith("project:") for t in tags)  # positive control


# --- integration with capture ------------------------------------------------


def test_an_excluded_url_gets_no_claim(db, fake_zotero, fake_title_fetcher) -> None:
    """Exclusion is about what the library records. A claim about a URL the
    library refused would be provenance for a row that does not exist."""
    result = capture_message(
        message="Reserved names are excluded, see https://example.com/x for why.",
        project_slug="home",
        context=None,
        today=date(2026, 8, 27),
        db_path=db,
        zotero=fake_zotero,
        title_fetcher=fake_title_fetcher,
    )
    assert result.urls_excluded == 1
    assert result.claims_recorded == 0
    assert claim_counts(db) == (0, 0)


def test_the_claim_survives_a_failed_zotero_write(db, fake_title_fetcher) -> None:
    """The claim is written before any network call, and is the one part of a
    capture that does not need Zotero to be reachable. A run in which every
    write fails should still leave a record of what was being cited."""
    from zotero_capture.zotero_client import ZoteroError

    broken = MagicMock()
    broken.post_webpage_item.side_effect = ZoteroError("upstream is down")

    result = capture_message(
        message=f"Colony size predicts fidelity, per {URL}.",
        project_slug="home",
        context=None,
        today=date(2026, 8, 27),
        db_path=db,
        zotero=broken,
        title_fetcher=fake_title_fetcher,
    )

    assert result.errors, "positive control: the write really did fail"
    assert result.claims_recorded == 1
    assert "Colony size predicts fidelity" in claims_for_url(db, URL)[0]["claim"]
