"""URL processing tests — extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import pytest

from zotero_capture.url_processing import canonicalize, extract_urls, is_excluded


# --- canonicalize ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://fixturehost.org/foo#bar", "https://fixturehost.org/foo"),
        ("https://fixturehost.org/foo/", "https://fixturehost.org/foo"),
        ("https://FIXTUREHOST.org/Foo", "https://fixturehost.org/Foo"),
        (
            "https://fixturehost.org/foo?utm_source=x&q=keep",
            "https://fixturehost.org/foo?q=keep",
        ),
        ("https://fixturehost.org/foo?fbclid=abc&gclid=def", "https://fixturehost.org/foo"),
        (
            "https://FIXTUREHOST.org/foo/?utm_campaign=x&id=42#frag",
            "https://fixturehost.org/foo?id=42",
        ),
        ("https://fixturehost.org/", "https://fixturehost.org"),
    ],
)
def test_canonicalize(raw: str, expected: str):
    assert canonicalize(raw) == expected


# --- extract_urls ---


def test_extract_basic_url():
    text = "See https://fixturehost.org/foo for details."
    assert extract_urls(text) == ["https://fixturehost.org/foo"]


def test_extract_strips_trailing_punctuation():
    text = "Check (https://fixturehost.org/foo), and [https://fixturehost.org/bar]."
    assert extract_urls(text) == ["https://fixturehost.org/foo", "https://fixturehost.org/bar"]


def test_extract_markdown_link():
    text = "See [the docs](https://fixturehost.org/docs)."
    assert extract_urls(text) == ["https://fixturehost.org/docs"]


def test_extract_source_line():
    text = "Source: https://fixturehost.org/article"
    assert extract_urls(text) == ["https://fixturehost.org/article"]


def test_extract_dedups_within_message():
    text = "https://fixturehost.org/x and again https://fixturehost.org/x"
    assert extract_urls(text) == ["https://fixturehost.org/x"]


def test_extract_no_urls():
    assert extract_urls("nothing here") == []


def test_extract_strips_backtick_fence():
    text = "See `https://fixturehost.org/foo` for details."
    assert extract_urls(text) == ["https://fixturehost.org/foo"]


# --- parenthesised URLs ---
#
# A closing paren is a legal, load-bearing URL character: Cell Press PII links
# and the DOIs behind them carry one (10.1016/s0092-8674(00)80876-3), as do
# Wikipedia disambiguation pages. Truncating it stores a URL that 404s forever
# and that no title backfill can repair, because the item's own URL is wrong.


def test_extract_keeps_a_balanced_trailing_paren():
    text = "See https://en.wikipedia.org/wiki/Aestivation_(botany) here"
    assert extract_urls(text) == ["https://en.wikipedia.org/wiki/Aestivation_(botany)"]


def test_extract_keeps_parens_inside_a_cell_press_identifier():
    url = "https://www.cell.com/cell/fulltext/S0092-8674(25)00123-4"
    assert extract_urls(f"paper: {url}") == [url]


def test_extract_keeps_parens_inside_a_doi():
    url = "https://doi.org/10.1016/s0092-8674(00)80876-3"
    assert extract_urls(f"cited {url} today") == [url]


def test_extract_strips_a_paren_that_wraps_the_url():
    text = "Ref (https://fixturehost.org/foo) and more"
    assert extract_urls(text) == ["https://fixturehost.org/foo"]


def test_extract_strips_only_the_wrapping_paren_from_a_parenthesised_url():
    """Both rules at once: the URL owns one paren, the prose owns the other."""
    text = "(https://en.wikipedia.org/wiki/Volcano_plot_(statistics))"
    assert extract_urls(text) == [
        "https://en.wikipedia.org/wiki/Volcano_plot_(statistics)"
    ]


def test_extract_strips_sentence_punctuation_after_a_balanced_paren():
    text = "See https://en.wikipedia.org/wiki/Aestivation_(botany)."
    assert extract_urls(text) == ["https://en.wikipedia.org/wiki/Aestivation_(botany)"]


# --- is_excluded ---


@pytest.mark.parametrize(
    "url, excluded",
    [
        ("https://fixturehost.org/foo", False),
        ("https://localhost:3000/x", True),
        ("http://127.0.0.1/x", True),
        ("http://0.0.0.0:8080/x", True),
        ("https://homelab.tail-abc12.ts.net/grafana", True),
        ("https://100.113.204.41:8765/jobs", True),
        ("http://10.0.0.110/", True),
        ("http://192.168.1.1/", True),
        ("http://172.16.5.5/", True),
        ("https://172.32.0.1/", False),
        ("http://[::1]/", True),
        ("http://[fe80::1]/", True),
        ("http://[fc00::1]/", True),
        ("https://[2606:4700:4700::1111]/", False),
        # Infrastructure hosts: fonts, DoH endpoints, analytics beacons. These are
        # never sources — they are machinery a page loaded, captured incidentally.
        ("https://fonts.googleapis.com/css2?family=Inter", True),
        ("https://fonts.gstatic.com/s/inter/v12/x.woff2", True),
        ("https://cloudflare-dns.com/dns-query?name=fixturehost.org", True),
        ("https://mozilla.cloudflare-dns.com/dns-query", True),
        ("https://dns.google/resolve?name=fixturehost.org", True),
        ("https://static.cloudflareinsights.com/beacon.min.js", True),
        # Asset paths: the bytes a page references, not the page itself.
        ("https://inaturalist-open-data.s3.amazonaws.com/photos/28969484/medium.jpg", True),
        ("https://upload.wikimedia.org/wikipedia/commons/3/3e/A_rose_bush.jpg", True),
        ("https://raw.githubusercontent.com/musharna/stackhealth/main/stackhealth.py", False),
        ("https://fixturehost.org/theme.css", True),
        ("https://fixturehost.org/bundle.min.js", True),
        ("https://fixturehost.org/logo.SVG", True),
        # Negative controls: HTML pages whose path merely ends in an asset
        # extension. Both resolved to real titles in production, so a naive
        # extension match would silently drop genuine sources.
        ("https://github.com/mrdoob/three.js/blob/dev/examples/jsm/loaders/GLTFLoader.js", False),
        ("https://github.com/musharna/stackhealth/actions/workflows/smoke.yml/badge.svg", False),
        ("https://commons.wikimedia.org/wiki/File:Glycine_max_kz01.jpg", False),
        # A query string must not smuggle an asset extension past the check.
        ("https://fixturehost.org/article?ref=x.css", False),
        # Reserved names (IANA Special-Use Domain Names registry, RFC 6761 /
        # RFC 2606). Standards guarantee these never resolve to anything real,
        # so they can never be a source. They matter because test fixtures
        # across the ecosystem use them: another project's fixture URLs, echoed
        # into a session, became five real rows in the citation library.
        ("https://example.com/licenses/by/4.0", True),
        ("https://example.net/x", True),
        ("https://example.org/x", True),
        ("https://evil.example.com", True),
        ("https://anything.example/x", True),
        ("https://foo.test/x", True),
        ("https://foo.invalid/x", True),
        ("https://api.localhost/x", True),
        ("https://printer.local/x", True),
        ("https://home.arpa/x", True),
        ("https://foo.alt/x", True),
        ("https://wiki.internal/x", True),
        ("https://abc123.onion/x", True),
        # The userinfo-@-host spoof the fixtures were probing: the real host is
        # after the "@", so a reader skimming the URL sees creativecommons.org
        # while the request goes to evil.example.com.
        ("http://creativecommons.org@evil.example.com/licenses/by/4.0", True),
        ("https://creativecommons.org:8080@evil.example.com/licenses/by/4.0", True),
        # Negative controls against matching a reserved name as a substring.
        # Every one of these is a perfectly ordinary registrable host.
        ("https://example.com.evil.co/x", False),
        ("https://myexample.com/x", False),
        ("https://contest.com/x", False),
        ("https://localhostage.com/x", False),
        ("https://internal-affairs.gov/x", False),
        # A host that is not a hostname. "https://…" reached the library from a
        # display ellipsis in a message and raised "Invalid IDNA hostname" on
        # every single fetch attempt thereafter.
        ("https://\u2026", True),
        ("https://\u2026/report.html", True),
        ("https://foo\u2026bar.com/x", True),
        ("https://exa mple.com/x", True),
        # Negative controls: internationalised domains are real and must survive.
        ("https://m\u00fcnchen.de/x", False),
        ("https://\u4f8b\u3048.\u30c6\u30b9\u30c8/x", False),
        ("https://xn--mnchen-3ya.de/x", False),
    ],
)
def test_is_excluded(url: str, excluded: bool):
    assert is_excluded(url) is excluded
