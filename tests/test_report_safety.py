"""A generated report must not become a citation, whatever is in an item's title.

Two layers keep the plugin's own reports out of the library. The structural one
is that a report shows each URL in a code span, which extraction treats as
displayed rather than cited. The backup is an explicit marker on the report.

Both had a hole. Item titles come from fetched web pages, so they are attacker-
influenceable text pasted straight into markdown: one stray backtick in a title
closed the span early and left the report's own URL as ordinary prose. And the
marker was honoured in any message from anyone, so its literal text worked as an
unauthenticated kill switch for capture — quoting it in a prompt silenced the
whole message.

Reported by an external audit of v0.10.0 (2026-08-22).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zotero_capture.capture import CaptureResult, capture_message
from zotero_capture.delta import _md_link
from zotero_capture.url_processing import NO_CAPTURE_MARKER, extract_urls

REPORTED = "https://fixturehost.org/reported-source"


@pytest.fixture
def fake_zotero():
    z = MagicMock()
    z.post_webpage_item.return_value = "NEWKEY"
    z.add_tags.return_value = True
    return z


def _item(title: str) -> dict:
    return {"data": {"title": title, "url": REPORTED}}


def test_a_clean_title_leaves_the_report_url_uncited():
    """Positive control: the ordinary case must work, or the rest proves nothing."""
    assert extract_urls(_md_link(_item("An Ordinary Title"))) == []


def test_a_stray_backtick_in_a_title_cannot_uncover_the_report_url():
    assert extract_urls(_md_link(_item("Tool `name"))) == []


def test_emphasis_in_a_title_cannot_uncover_the_report_url():
    assert extract_urls(_md_link(_item("A **bold** claim"))) == []


def test_a_title_that_is_itself_a_code_span_cannot_uncover_the_report_url():
    assert extract_urls(_md_link(_item("`already spanned`"))) == []


def test_a_title_containing_a_link_cannot_add_a_citation():
    assert extract_urls(_md_link(_item("See [here](https://fixturehost.org/x)"))) == []


def test_the_title_text_survives_escaping():
    """Escaping must not eat the title — the report still has to be readable."""
    assert "Tool" in _md_link(_item("Tool `name"))
    assert "name" in _md_link(_item("Tool `name"))


# --- the marker is not a global kill switch ---


def _capture(message: str, *, origin: str, zotero, db_path) -> CaptureResult:
    return capture_message(
        message=message,
        project_slug="p",
        context="c",
        today=date(2026, 8, 22),
        db_path=db_path,
        zotero=zotero,
        title_fetcher=lambda _url: "T",
        origin=origin,
    )


def test_a_report_from_the_assistant_is_still_skipped(tmp_path, fake_zotero):
    """Positive control: the marker must keep doing its actual job."""
    msg = f"{NO_CAPTURE_MARKER}\n\nSee https://fixturehost.org/a\n"
    result = _capture(
        msg, origin="assistant", zotero=fake_zotero, db_path=tmp_path / "d"
    )
    assert result.urls_seen == 0


def test_a_user_prompt_quoting_the_marker_is_still_captured(tmp_path, fake_zotero):
    """A prompt is not a generated report, so the marker carries no authority."""
    msg = f"why does {NO_CAPTURE_MARKER} suppress https://fixturehost.org/a ?"
    result = _capture(msg, origin="user", zotero=fake_zotero, db_path=tmp_path / "d")
    assert result.urls_seen == 1


def test_an_assistant_message_merely_quoting_the_marker_is_still_captured(
    tmp_path, fake_zotero
):
    """Only a report LEADS with the marker; discussing it must not silence a turn."""
    msg = f"The marker is {NO_CAPTURE_MARKER}, and I cited https://fixturehost.org/a\n"
    result = _capture(
        msg, origin="assistant", zotero=fake_zotero, db_path=tmp_path / "d"
    )
    assert result.urls_seen == 1
