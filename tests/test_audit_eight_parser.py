"""Parser defects found in the 2026-08-25 audit, once the harness could fail again.

Each of these was captured or lost by shipped 0.11.7. They are grouped because
they share a cause: extraction trusted the CommonMark parser's output in places
where it applies its own normalisation, and trusted linkify's boundary in a
place where linkify is guessing.
"""

from __future__ import annotations


from zotero_capture.url_processing import bare_urls, canonicalize, extract_urls, is_storable_url


def test_a_template_in_a_link_destination_is_not_captured():
    """The brace rule protected prose but not `[text](url)`.

    markdown-it percent-encodes a link destination, so "{ID}" arrives as
    "%7BID%7D" — no literal brace for the guard to see, and the result passes
    is_storable_url. It reached the live library as
    "files.rcsb.org/download/%7BID%7D.pdb", a fetchable directory listing that
    nobody cited.
    """
    urls = extract_urls("[download](https://files.rcsb.org/download/{ID}.pdb)")
    assert urls == [], f"a template destination was captured: {urls}"


def test_a_real_percent_encoded_url_in_a_link_still_survives():
    """The positive control for the rule above.

    Rejecting every destination that decodes to something odd would be an easy
    way to make the previous test pass while breaking ordinary citations.
    """
    urls = extract_urls("[paper](https://example.org/a%20b/c%2Bd?q=1)")
    assert urls == ["https://example.org/a%20b/c%2Bd?q=1"]


def test_a_url_with_an_impossible_port_is_refused_not_raised():
    """One malformed link must not discard the message's real citations.

    urlsplit().port raises ValueError for a port above 65535, and capture
    canonicalises outside its per-URL guard — so a single bad link aborted the
    whole turn and every good URL in it was lost.
    """
    assert is_storable_url("https://example.com:99999/path") is False
    # And the good neighbour in the same message survives.
    urls = extract_urls(
        "[bad](https://example.com:99999/path) and [good](https://good.com/paper)"
    )
    assert urls == ["https://good.com/paper"]


def test_canonicalize_never_raises_on_a_malformed_url():
    """Defence in depth: the caller should not have to guard every call."""
    canonicalize("https://example.com:99999/path")


def test_a_url_in_a_raw_html_anchor_is_captured():
    """An anchor renders as a hyperlink; it is a citation, not a code sample."""
    urls = extract_urls('See <a href="https://example.com/paper">the paper</a>.')
    assert urls == ["https://example.com/paper"]


def test_a_code_span_is_still_not_a_citation():
    """Positive control for the rule above: literals stay excluded."""
    assert extract_urls("Run `curl https://example.com/paper` to fetch it.") == []


def test_an_unbalanced_paren_drops_the_url_rather_than_storing_a_prefix():
    """linkify balances parens, so an unbalanced "(" truncates the address.

    "(" is a legal sub-delimiter, so the truncation is silent and the prefix
    often resolves — the same manufactured-citation harm the template rule
    exists to prevent. Loud absence beats a wrong source.
    """
    out = bare_urls("See https://commons.wikimedia.org/wiki/File:Foo_(Bar_Baz.jpg here.")
    assert out == [], f"stored a truncated prefix: {out}"


def test_a_balanced_paren_url_is_untouched():
    """Positive control: the common Wikipedia/Wikimedia shape must survive."""
    url = "https://en.wikipedia.org/wiki/Foo_(disambiguation)"
    assert bare_urls(f"See {url} here.") == [url]


def test_a_trailing_paren_in_prose_is_still_not_part_of_the_url():
    """And the prose case linkify's balancer exists for keeps working."""
    assert bare_urls("see (https://example.org/foo) for more") == [
        "https://example.org/foo"
    ]
