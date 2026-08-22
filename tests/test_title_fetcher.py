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
        assert fetch_title("https://fixturehost.org/foo", client=client) == "Hello World"


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
            fetch_title("https://fixturehost.org/foo", client=client)
            == "https://fixturehost.org/foo"
        )


def test_fetch_title_falls_back_to_url_on_timeout():
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=req)

    transport = httpx.MockTransport(boom)
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://fixturehost.org/foo", client=client)
            == "https://fixturehost.org/foo"
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
            fetch_title("https://fixturehost.org/foo.pdf", client=client)
            == "https://fixturehost.org/foo.pdf"
        )


@pytest.mark.live
def test_fetch_title_real_well_known_url():
    """Real-execution check at the HTTP-fetch boundary (per global rule).

    Deliberately uses example.com even though capture excludes it: this probes
    fetch_title, which never consults is_excluded, and the page is the one URL
    on the web whose title is guaranteed stable by standard.
    """
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
        assert fetch_title("https://fixturehost.org/", client=client) == "foo bar"


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
            fetch_title("https://fixturehost.org/", client=client) == "https://fixturehost.org/"
        )


def test_fetch_title_returns_url_on_4xx():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            404, headers={"content-type": "text/html"}, content=b"<title>nope</title>"
        )
    )
    with httpx.Client(transport=transport) as client:
        assert (
            fetch_title("https://fixturehost.org/missing", client=client)
            == "https://fixturehost.org/missing"
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
        assert fetch_title("https://fixturehost.org/page", client=client) == "Ordinary Page"
    assert not any(CSL_ACCEPT in a for a in seen), "only DOIs should negotiate"


@pytest.fixture
def live_client():
    """Live identifier tests verify the API integration, not the hook's 1s budget.

    Sharing a generous client keeps a slow-but-working API from failing the suite;
    the 1s budget itself is covered by the mocked tests above.
    """
    with httpx.Client(timeout=8.0, follow_redirects=True) as c:
        yield c


@pytest.mark.live
def test_live_real_doi_resolves_to_its_article_title(live_client):
    """Real-execution check: the plugin's own User-Agent against the real DOI resolver."""
    title = fetch_title(
        "https://doi.org/10.1371/journal.pcbi.1009935", client=live_client
    )
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
    # Exact, not a substring: the substring form passed happily while the DOI
    # still carried a "v1" suffix, which is how the defect below survived.
    assert seen == ["https://doi.org/10.1101/2025.05.30.656746"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v1",
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v2",
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v1.full",
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v1.full.pdf",
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746v1.supplementary-material",
        "https://www.biorxiv.org/content/10.1101/2025.05.30.656746",
        "https://www.medrxiv.org/content/10.1101/2025.05.30.656746v3.article-info",
    ],
)
def test_preprint_version_suffix_is_not_part_of_the_doi(url: str):
    """A preprint URL carries a version suffix that is NOT part of its DOI.

    Sending "10.1101/2025.05.30.656746v1" makes doi.org 404 — correctly, since
    that is not a DOI. Measured live 2026-08-21: the bare DOI returns 200 for
    the same papers. This is why the backfill only moved bioRxiv 55 -> 53, and
    it was misattributed to preprint DOIs being unregistered.
    """
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={"title": "Bamboos flower after the return"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title(url, client=client)

    assert got == "Bamboos flower after the return"
    assert seen == ["https://doi.org/10.1101/2025.05.30.656746"], (
        f"requested a malformed DOI for {url}"
    )


@pytest.mark.parametrize(
    "url, doi",
    [
        # bioRxiv minted a new DOI prefix for 2026 papers; hardcoding 10.1101
        # silently skips them. Both verified live 2026-08-21.
        (
            "https://www.biorxiv.org/content/10.64898/2026.02.05.703842v1.full.pdf",
            "10.64898/2026.02.05.703842",
        ),
        (
            "https://www.biorxiv.org/content/10.64898/2026.01.13.699201v1",
            "10.64898/2026.01.13.699201",
        ),
        # Older preprints carry a bare serial rather than a dated identifier.
        (
            "https://www.biorxiv.org/content/10.1101/269415.full.pdf",
            "10.1101/269415",
        ),
        (
            "https://www.biorxiv.org/content/10.1101/2025.10.10.681754.full.pdf",
            "10.1101/2025.10.10.681754",
        ),
    ],
)
def test_preprint_doi_prefix_is_not_hardcoded(url: str, doi: str):
    """The registrant prefix is data, not a constant — bioRxiv changed it."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={"title": "A preprint"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title(url, client=client)

    assert got == "A preprint"
    assert seen == [f"https://doi.org/{doi}"]


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
def test_live_arxiv_resolves_to_the_paper_title(live_client):
    # The API is the intended path, but if it exceeds the 1s budget the abs-page
    # scrape returns "[id] Title" — both are acceptable resolutions, neither is the URL.
    title = fetch_title("https://arxiv.org/abs/1706.03762", client=live_client)
    assert "Attention Is All You Need" in title, title


@pytest.mark.live
def test_live_pubmed_resolves_to_the_article_title(live_client):
    title = fetch_title("https://pubmed.ncbi.nlm.nih.gov/22817898/", client=live_client)
    assert "whole-cell computational model" in title.lower(), title


@pytest.mark.live
def test_live_github_resolves_to_owner_repo(live_client):
    title = fetch_title("https://github.com/psf/requests", client=live_client)
    assert title.startswith("psf/requests"), title


@pytest.mark.live
def test_live_dead_github_repo_stays_unresolved(live_client):
    """Positive control for the negative: a live 404 must not invent a title."""
    url = "https://github.com/musharna/definitely-not-a-real-repo-zp"
    assert fetch_title(url, client=live_client) == url


@pytest.mark.live
def test_live_arxiv_pdf_link_resolves_via_the_api(live_client):
    """122 of 128 arXiv items in the backlog are /pdf/ links, which never scrape."""
    title = fetch_title("https://arxiv.org/pdf/1706.03762", client=live_client)
    assert "Attention Is All You Need" in title, title


def test_fetch_title_sends_contactable_user_agent():
    """Wikimedia 403s any User-Agent without a contact URL — including a browser's.

    Verified live 2026-08-21: 'zotero-provenance/0.1' and 'Mozilla/5.0' both got
    403 from en.wikipedia.org, while a string carrying the project URL got 200.
    """
    seen: list[str] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("user-agent", ""))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>Ok</title></head></html>",
        )

    transport = httpx.MockTransport(record)
    with httpx.Client(transport=transport) as client:
        fetch_title("https://en.wikipedia.org/wiki/Thismia_americana", client=client)

    assert seen, "no request was made"
    ua = seen[0]
    assert "zotero-provenance" in ua, f"UA must identify the project: {ua!r}"
    assert "https://" in ua, f"UA must carry a contact URL (Wikimedia policy): {ua!r}"


@pytest.mark.live
def test_live_wikipedia_resolves_to_the_article_title(live_client):
    """Real-execution check: Wikimedia enforces its UA policy at the network edge.

    The mocked UA test above asserts the header we send; only a live request can
    catch Wikimedia tightening what it accepts. Pre-fix this returned the URL.
    """
    title = fetch_title(
        "https://en.wikipedia.org/wiki/Thismia_americana", client=live_client
    )
    assert "Thismia americana" in title, title


# --- title normalisation ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Publisher CSL metadata carries inline markup. Observed live 2026-08-21
        # on 10.1101/2024.01.15.575765.
        (
            "Combining RAS\n    <sup>G12C</sup>\n    (ON) inhibitors",
            "Combining RAS G12C (ON) inhibitors",
        ),
        ("H<sub>2</sub>O uptake", "H2O uptake"),
        ("<i>Arabidopsis thaliana</i> growth", "Arabidopsis thaliana growth"),
        ("Genes &amp; Development", "Genes & Development"),
        ("Spaced   out\ttitle\n", "Spaced out title"),
        ("<scp>DNA</scp> repair", "DNA repair"),
        # Negative controls: a bare "<" is not markup and must survive.
        ("Growth when a < b in plants", "Growth when a < b in plants"),
        ("Cost < 5% of baseline", "Cost < 5% of baseline"),
        ("A normal title", "A normal title"),
    ],
)
def test_titles_are_normalised(raw: str, expected: str):
    """Markup and stray whitespace must not reach the Zotero item."""
    from zotero_capture.title_fetcher import _clean_title

    assert _clean_title(raw) == expected


def test_fetch_title_normalises_what_a_resolver_returns():
    """The clean step belongs at the boundary, so every route benefits."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "Ras\n  <sup>G12C</sup>\n  binding"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_title("https://doi.org/10.1101/2024.01.15.575765", client=client)
    assert got == "Ras G12C binding"


def test_fetch_title_still_returns_the_url_unchanged_on_failure():
    """The URL-as-sentinel contract: callers test equality with the URL."""
    url = "https://fixturehost.org/a_(b)?x=1&y=2"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_title(url, client=client) == url
