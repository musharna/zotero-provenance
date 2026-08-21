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


ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query Feed</title>
  <entry><title>Attention Is All You Need</title></entry>
</feed>"""


def _json_client(payload, *, status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_arxiv_is_resolved_by_its_api_not_by_scraping():
    def handler(req: httpx.Request) -> httpx.Response:
        if "export.arxiv.org" in str(req.url):
            return httpx.Response(200, text=ARXIV_ATOM)
        return httpx.Response(200, html="<html><title>arXiv landing</title></html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title("https://arxiv.org/abs/1706.03762", client=client)
    assert got == "Attention Is All You Need"


def test_arxiv_feed_level_title_is_not_mistaken_for_the_paper():
    """The Atom feed has its own <title>; only the <entry> title is the paper."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=ARXIV_ATOM)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title("https://arxiv.org/abs/1706.03762", client=client)
    assert got != "ArXiv Query Feed"


def test_pubmed_is_resolved_by_esummary():
    payload = {"result": {"12345": {"title": "A whole-cell computational model"}}}
    with _json_client(payload) as client:
        got = fetch_title("https://pubmed.ncbi.nlm.nih.gov/12345/", client=client)
    assert got == "A whole-cell computational model"


def test_biorxiv_resolves_through_the_doi_embedded_in_its_path():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={"title": "Bamboos flower after the return"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title(
            "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v1", client=client
        )
    assert got == "Bamboos flower after the return"
    assert any("10.1101/2025.05.30.656746" in u for u in seen)


def test_github_repo_is_resolved_by_the_api():
    payload = {
        "full_name": "musharna/zotero-provenance",
        "description": "Records sources",
    }
    with _json_client(payload) as client:
        got = fetch_title(
            "https://github.com/musharna/zotero-provenance", client=client
        )
    assert got == "musharna/zotero-provenance: Records sources"


def test_github_repo_without_a_description_falls_back_to_its_name():
    payload = {"full_name": "musharna/zotero-provenance", "description": None}
    with _json_client(payload) as client:
        got = fetch_title(
            "https://github.com/musharna/zotero-provenance", client=client
        )
    assert got == "musharna/zotero-provenance"


def test_dead_github_repo_still_falls_back_to_the_url():
    """404s are dead or private repos; no API recovers them and none should pretend to."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    url = "https://github.com/someone/deleted-repo"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title(url, client=client) == url


@pytest.mark.live
def test_live_arxiv_resolves_to_the_paper_title():
    # The API is the intended path, but if it exceeds the 1s budget the abs-page
    # scrape returns "[id] Title" — both are acceptable resolutions, neither is the URL.
    title = fetch_title("https://arxiv.org/abs/1706.03762")
    assert "Attention Is All You Need" in title, title


@pytest.mark.live
def test_live_pubmed_resolves_to_the_article_title():
    title = fetch_title("https://pubmed.ncbi.nlm.nih.gov/22817898/")
    assert "whole-cell computational model" in title.lower(), title


@pytest.mark.live
def test_live_github_resolves_to_owner_repo():
    title = fetch_title("https://github.com/psf/requests")
    assert title.startswith("psf/requests"), title


@pytest.mark.live
def test_live_dead_github_repo_stays_unresolved():
    """Positive control for the negative: a live 404 must not invent a title."""
    url = "https://github.com/musharna/definitely-not-a-real-repo-zp"
    assert fetch_title(url) == url


@pytest.mark.live
def test_live_arxiv_pdf_link_resolves_via_the_api():
    """122 of 128 arXiv items in the backlog are /pdf/ links, which never scrape."""
    title = fetch_title("https://arxiv.org/pdf/1706.03762")
    assert "Attention Is All You Need" in title, title
