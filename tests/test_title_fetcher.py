"""title_fetcher tests — bs4-based <title> extraction with 1s timeout."""

from __future__ import annotations

import httpx
import pytest

from zotero_capture.title_fetcher import fetch_title


def test_fetch_title_extracts_title_tag():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>Hello World</title></head></html>",
        )
    )
    with httpx.Client(transport=transport) as client:
        assert fetch_title("https://example.com/foo", client=client) == "Hello World"


def test_fetch_title_falls_back_to_url_on_no_title():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>nothing</body></html>",
        )
    )
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://example.com/foo", client=client)
            == "https://example.com/foo"
        )


def test_fetch_title_falls_back_to_url_on_timeout():
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=req)

    transport = httpx.MockTransport(boom)
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://example.com/foo", client=client)
            == "https://example.com/foo"
        )


def test_fetch_title_falls_back_on_non_html():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4...",
        )
    )
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://example.com/foo.pdf", client=client)
            == "https://example.com/foo.pdf"
        )


@pytest.mark.live
def test_fetch_title_real_well_known_url():
    """Real-execution check at the HTTP-fetch boundary (per global rule)."""
    title = fetch_title("https://example.com/")
    assert title == "Example Domain"


def test_fetch_title_handles_nested_tags_in_title():
    """Common case: news sites with inline branding. Was bug C2 (silent URL fallback)."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>foo <span>bar</span></title></head></html>",
        )
    )
    with httpx.Client(transport=transport) as client:
        assert fetch_title("https://example.com/", client=client) == "foo bar"


def test_fetch_title_truncates_large_body():
    """Body > MAX_BYTES with <title> placed past truncation boundary should fall back to URL.

    Proves MAX_BYTES bound is enforced (C1).
    """
    # 40 KB padding before the title tag — past the 32 KB MAX_BYTES window
    padding = b"<p>x</p>" * 5000  # 40_000 bytes
    body = b"<html><body>" + padding + b"<title>NeverSeen</title></body></html>"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=body,
        )
    )
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://example.com/", client=client) == "https://example.com/"
        )


def test_fetch_title_returns_url_on_4xx():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            404, headers={"content-type": "text/html"}, content=b"<title>nope</title>"
        )
    )
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://example.com/missing", client=client)
            == "https://example.com/missing"
        )
