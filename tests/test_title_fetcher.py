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


DOI_URL = "https://doi.org/10.1371/journal.pcbi.1009935"
CSL_ACCEPT = "application/vnd.citationstyles.csl+json"


def test_doi_is_resolved_by_content_negotiation_not_by_scraping():
    """A DOI is an identifier with a metadata API, not a web page."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("accept", ""))
        if CSL_ACCEPT in req.headers.get("accept", ""):
            return httpx.Response(200, json={"title": "Consistent standards"})
        return httpx.Response(
            200,
            html="<html><title>Publisher Landing Page</title></html>",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title(DOI_URL, client=client) == "Consistent standards"
    assert any(CSL_ACCEPT in a for a in seen), "should have negotiated for CSL JSON"


def test_doi_csl_title_given_as_a_list_is_flattened():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": ["First Form", "Alt Form"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title(DOI_URL, client=client) == "First Form"


def test_doi_falls_back_to_html_when_negotiation_fails():
    """Content negotiation is an optimisation, not a new single point of failure."""

    def handler(req: httpx.Request) -> httpx.Response:
        if CSL_ACCEPT in req.headers.get("accept", ""):
            return httpx.Response(503)
        return httpx.Response(
            200,
            html="<html><title>Publisher Landing Page</title></html>",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title(DOI_URL, client=client) == "Publisher Landing Page"


def test_non_doi_url_is_not_content_negotiated():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("accept", ""))
        return httpx.Response(200, html="<html><title>Ordinary Page</title></html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title("https://example.com/page", client=client) == "Ordinary Page"
    assert not any(CSL_ACCEPT in a for a in seen), "only DOIs should negotiate"


@pytest.mark.live
def test_live_real_doi_resolves_to_its_article_title():
    """Real-execution check: the plugin's own User-Agent against the real DOI resolver."""
    title = fetch_title("https://doi.org/10.1371/journal.pcbi.1009935")
    assert "functional enrichment analysis" in title.lower(), title
