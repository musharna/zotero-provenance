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
        (
            "https://fixturehost.org/foo?fbclid=abc&gclid=def",
            "https://fixturehost.org/foo",
        ),
        (
            "https://FIXTUREHOST.org/foo/?utm_campaign=x&id=42#frag",
            "https://fixturehost.org/foo?id=42",
        ),
        ("https://fixturehost.org/", "https://fixturehost.org"),
    ],
)
def test_canonicalize(raw: str, expected: str):
    assert canonicalize(raw) == expected


# --- HTML entities in a URL ---
#
# A URL lifted out of rendered HTML carries the page's escaping, so "?a=1&b=2"
# arrives as "?a=1&amp;b=2", which misses the dedup lookup and creates a second
# item for one source.
#
# canonicalize used to undo that with a blanket "&amp;" -> "&" substitution. That
# is the wrong layer: a literal "&amp;" is legal URL data, so the substitution
# also corrupted URLs that genuinely contained one, and in a query it invented a
# parameter delimiter that was never cited. Deciding whether an "&amp;" is
# escaping or data needs to know how the URL was written, which only the parser
# knows — and CommonMark already specifies the answer: a link destination has its
# entity references decoded, an autolink's content does not. So the property now
# lives in extract_urls, and canonicalize passes bytes through.
# Reported by an external audit of v0.10.0 (2026-08-22).


def test_extraction_undoes_html_escaping_of_a_query_separator():
    """The dedup property, now asserted where the parser decides it."""
    assert extract_urls("[x](https://fixturehost.org/s?a=1&amp;b=2)") == [
        "https://fixturehost.org/s?a=1&b=2"
    ]


def test_canonicalize_no_longer_rewrites_an_escaped_ampersand():
    """canonicalize is byte-preserving now; it cannot tell escaping from data."""
    assert (
        canonicalize("https://fixturehost.org/s?a=1&amp;b=2")
        == "https://fixturehost.org/s?a=1&amp;b=2"
    )


def test_canonicalize_leaves_an_ordinary_ampersand_alone():
    """Negative control: an already-correct separator must not be touched."""
    assert (
        canonicalize("https://fixturehost.org/s?a=1&b=2")
        == "https://fixturehost.org/s?a=1&b=2"
    )


def test_a_stored_url_redisplayed_lands_back_on_itself():
    """The dedup property the loop actually needed, stated as idempotence.

    The failure was a stored URL coming back as a second item every time it was
    displayed and re-read. What has to hold is therefore a round trip: capture a
    URL, print it the way a report prints it, capture again, land on the same
    string. Asserting instead that two DIFFERENT escapings compare equal was
    asserting a blanket fold, which is the bug — an escaped "&amp;" and a
    literal one are not the same URL and only the parser can tell them apart.
    """
    stored = canonicalize(extract_urls("See https://fixturehost.org/p?q=%22x")[0])
    redisplayed = canonicalize(extract_urls(f"[title]({stored})")[0])
    assert redisplayed == stored


def test_an_entity_escaped_destination_lands_on_the_unescaped_one():
    """One layer of HTML escaping is exactly what a link destination undoes."""
    escaped = extract_urls("[x](https://fixturehost.org/p?q=&quot;x)")
    literal = extract_urls("[x](https://fixturehost.org/p?q=%22x)")
    assert escaped == literal


def test_canonicalize_keeps_a_literal_ampersand_in_a_value():
    """Negative control: "&" that is data, not an entity, survives."""
    assert (
        canonicalize("https://fixturehost.org/s?q=Marks%26Spencer")
        == "https://fixturehost.org/s?q=Marks%26Spencer"
    )


# Decoding the FULL HTML entity table rewrites the URL's structure, because the
# table contains the delimiters themselves. Only "&amp;" is decoded: it is the
# one entity a URL picks up merely by being written into HTML, and folding it
# back cannot move a path segment or invent a query. Reported by an external
# audit of v0.9.0 (2026-08-22), which shipped the full-table version.


def test_canonicalize_does_not_let_an_entity_forge_a_path_separator():
    """&sol; is "/" — decoding it silently points at a different resource."""
    assert (
        canonicalize("https://fixturehost.org/x&sol;y")
        == "https://fixturehost.org/x&sol;y"
    )


def test_canonicalize_does_not_let_an_entity_forge_a_fragment():
    """&num; is "#" — decoding it truncated the URL at the invented fragment.

    Asserts the payload survives rather than an exact string: query pairs are
    re-encoded by the tracking-param pass either way, which is unrelated to the
    entity bug and would make an exact-match assertion test the wrong thing.
    """
    got = canonicalize("https://fixturehost.org/p?opaque=abc&num;def")
    assert "def" in got, "the tail was dropped as an invented fragment"
    assert "#" not in got


def test_canonicalize_does_not_let_an_entity_forge_a_query_start():
    """&quest; is "?" — decoding it moves data from the path into the query."""
    assert (
        canonicalize("https://fixturehost.org/p&quest;def")
        == "https://fixturehost.org/p&quest;def"
    )


# --- the query is data, not a dict ---
#
# Tracking params were dropped by running the query through parse_qsl and then
# urlencode. That round-trip does not preserve what it was not asked to change:
# a valueless field gained an "=", percent-encoding was normalised, and "+" was
# reinterpreted. A signed or opaque query survives none of that, and the URL then
# names a resource nobody cited. Filtering now splits on "&", drops the fields
# that match, and rejoins the survivors untouched.
# Reported by an external audit of v0.10.0 (2026-08-22).


def test_canonicalize_does_not_invent_a_value_for_a_bare_field():
    assert (
        canonicalize("https://fixturehost.org/x?sig=a&b")
        == "https://fixturehost.org/x?sig=a&b"
    )


def test_canonicalize_preserves_percent_encoding_in_a_signed_query():
    """Re-encoding a signature invalidates it."""
    assert (
        canonicalize("https://fixturehost.org/x?token=aGVsbG8%3D&sig=A%2FB%2BC")
        == "https://fixturehost.org/x?token=aGVsbG8%3D&sig=A%2FB%2BC"
    )


def test_canonicalize_does_not_reinterpret_a_plus():
    """ "+" means "+" to some servers and " " to others; guessing corrupts one."""
    assert (
        canonicalize("https://fixturehost.org/p?q=a+b")
        == "https://fixturehost.org/p?q=a+b"
    )


def test_canonicalize_keeps_a_repeated_field_and_its_order():
    assert (
        canonicalize("https://fixturehost.org/x?a=2&a=1&b=3")
        == "https://fixturehost.org/x?a=2&a=1&b=3"
    )


def test_canonicalize_drops_trackers_without_touching_the_survivors():
    """Positive control for the filter, inside the byte-preservation guarantee."""
    assert (
        canonicalize("https://fixturehost.org/x?utm_source=n&q=a+b&fbclid=z&sig=A%2FB")
        == "https://fixturehost.org/x?q=a+b&sig=A%2FB"
    )


def test_canonicalize_matches_a_tracker_case_insensitively():
    assert canonicalize("https://fixturehost.org/x?UTM_Source=n") == (
        "https://fixturehost.org/x"
    )


# --- extract_urls ---


def test_extract_basic_url():
    text = "See https://fixturehost.org/foo for details."
    assert extract_urls(text) == ["https://fixturehost.org/foo"]


def test_extract_strips_trailing_punctuation():
    text = "Check (https://fixturehost.org/foo), and [https://fixturehost.org/bar]."
    assert extract_urls(text) == [
        "https://fixturehost.org/foo",
        "https://fixturehost.org/bar",
    ]


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


# --- cited vs displayed ---
#
# Backticks and fences are Markdown's way of saying "this is a literal being
# shown", not "this is a source I am citing". The distinction matters because
# the capture hook reads the agent's own output: an audit that prints a bad URL
# used to re-capture it, one HTML-escape layer deeper each pass (&quot; ->
# &amp;quot;), which is unbounded. Measured over 260 assistant messages, 96% of
# real citations arrive as markdown links or bare prose, while the backticked
# form is 2.9% and is mostly internal hostnames and API endpoints.
#
# This inverts an earlier assertion that a backticked URL should be captured.


def test_extract_skips_a_url_shown_in_inline_code():
    text = "See `https://fixturehost.org/foo` for details."
    assert extract_urls(text) == []


def test_extract_skips_a_url_inside_a_fenced_block():
    text = "Output:\n\n```\nGET https://fixturehost.org/foo\n```\n\ndone."
    assert extract_urls(text) == []


def test_extract_keeps_prose_and_links_around_a_fenced_block():
    """Negative control: fencing one URL must not swallow the citations near it."""
    text = (
        "Per [the paper](https://fixturehost.org/paper) the call is:\n\n"
        "```\ncurl https://fixturehost.org/internal\n```\n\n"
        "See also https://fixturehost.org/followup"
    )
    assert extract_urls(text) == [
        "https://fixturehost.org/paper",
        "https://fixturehost.org/followup",
    ]


def test_extract_keeps_a_url_in_prose_on_a_line_that_also_has_code():
    """A code span elsewhere on the line must not suppress a cited URL."""
    text = "Run `make build`, then read https://fixturehost.org/guide"
    assert extract_urls(text) == ["https://fixturehost.org/guide"]


def test_extract_skips_an_unterminated_fence():
    """A truncated message can leave a fence open; treat the tail as displayed."""
    text = "Log follows:\n\n```\nfetching https://fixturehost.org/foo"
    assert extract_urls(text) == []


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
        ("https://100.64.0.1:8765/jobs", True),
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
        (
            "https://inaturalist-open-data.s3.amazonaws.com/photos/28969484/medium.jpg",
            True,
        ),
        ("https://upload.wikimedia.org/wikipedia/commons/3/3e/A_rose_bush.jpg", True),
        (
            "https://raw.githubusercontent.com/someone/a-tool/main/a-tool.py",
            False,
        ),
        ("https://fixturehost.org/theme.css", True),
        ("https://fixturehost.org/bundle.min.js", True),
        ("https://fixturehost.org/logo.SVG", True),
        # Negative controls: HTML pages whose path merely ends in an asset
        # extension. Both resolved to real titles in production, so a naive
        # extension match would silently drop genuine sources.
        (
            "https://github.com/mrdoob/three.js/blob/dev/examples/jsm/loaders/GLTFLoader.js",
            False,
        ),
        (
            "https://github.com/someone/a-tool/actions/workflows/smoke.yml/badge.svg",
            False,
        ),
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


# --- CommonMark delimiter rules (F4) ---
#
# v0.9.0 toggled on any line starting with three backticks or tildes and only
# understood single-backtick, same-line spans. CommonMark requires a closing
# fence to use the opener's character, be at least as long, and carry only
# trailing whitespace; and a code span pairs backtick runs of EQUAL length.
# Getting this wrong is worse than a false positive: one bogus toggle swallows
# every genuine citation after it.


def test_a_double_backtick_span_is_still_code():
    assert extract_urls("see ``https://fixturehost.org/x`` ok") == []


def test_a_tilde_line_does_not_close_a_backtick_fence():
    text = "```\ncode\n~~~\nhttps://fixturehost.org/leak\n```\n"
    assert extract_urls(text) == []


def test_a_shorter_fence_does_not_close_a_longer_one():
    text = "````\nhttps://fixturehost.org/a\n```\nhttps://fixturehost.org/b\n````\n"
    assert extract_urls(text) == []


def test_a_closing_fence_may_not_carry_trailing_content():
    """```still-code opens nothing and closes nothing; the block stays open."""
    text = "```\nhttps://fixturehost.org/a\n```still-code\nhttps://fixturehost.org/b\n"
    assert extract_urls(text) == []


def test_a_fence_inside_a_blockquote_is_recognised():
    text = "> ```\n> https://fixturehost.org/quoted\n> ```\n"
    assert extract_urls(text) == []


def test_a_code_span_may_cross_a_line_within_a_paragraph():
    text = "run `curl\nhttps://fixturehost.org/internal` then stop"
    assert extract_urls(text) == []


def test_a_stray_backtick_cannot_swallow_a_later_paragraph():
    """Bounds the damage: an unpaired tick must not mask the rest of the message."""
    text = "an unclosed ` tick here\n\nSee https://fixturehost.org/cited for details."
    assert extract_urls(text) == ["https://fixturehost.org/cited"]


def test_prose_and_links_survive_all_of_the_above():
    """Positive control for the whole block."""
    text = (
        "Per [paper](https://fixturehost.org/p) and https://fixturehost.org/q:\n\n"
        "```\nhttps://fixturehost.org/hidden\n```\n"
    )
    assert extract_urls(text) == [
        "https://fixturehost.org/p",
        "https://fixturehost.org/q",
    ]


# --- IPv6 through the whole pipeline (F8) ---
#
# The existing IPv6 cases call is_excluded() directly, so neither the tokenizer
# nor canonicalize was ever exercised. Both were broken: URL_RE stopped before
# "]", and canonicalize rebuilt the netloc without brackets.


def test_extract_keeps_a_bracketed_ipv6_url_whole():
    url = "https://[2606:4700:4700::1111]/x"
    assert extract_urls(f"see {url} here") == [url]


def test_canonicalize_keeps_the_ipv6_brackets():
    assert (
        canonicalize("https://[2606:4700:4700::1111]/x")
        == "https://[2606:4700:4700::1111]/x"
    )


def test_ipv6_loopback_is_excluded_through_the_pipeline():
    (url,) = extract_urls("see http://[::1]:8080/admin here")
    assert is_excluded(canonicalize(url))


def test_public_ipv6_survives_the_pipeline():
    """Negative control: a real address must not be dropped."""
    (url,) = extract_urls("see https://[2606:4700:4700::1111]/x here")
    assert not is_excluded(canonicalize(url))


# --- SSRF: the host filter is a boundary, not a spelling check (F6) ---
#
# inet_aton accepts every one of these and they all resolve to 127.0.0.1, so a
# textual "does it look like 127.0.0.1" test never sees them.


@pytest.mark.parametrize(
    "host",
    ["2130706433", "0x7f000001", "017700000001", "127.1", "0177.0.0.1"],
)
def test_obfuscated_loopback_forms_are_excluded(host: str):
    assert is_excluded(canonicalize(f"http://{host}/admin"))


@pytest.mark.parametrize(
    "host",
    ["3232235777", "0xa000001", "10.1", "192.168.1.1"],
)
def test_obfuscated_private_forms_are_excluded(host: str):
    assert is_excluded(canonicalize(f"http://{host}/admin"))


@pytest.mark.parametrize("url", ["https://8.8.8.8/x", "https://fixturehost.org/x"])
def test_public_addresses_and_names_survive(url: str):
    """Negative control: the boundary must not swallow the legitimate case."""
    assert not is_excluded(canonicalize(url))
