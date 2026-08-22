"""Extraction reads a CommonMark parse, not a hand-rolled scan of the text.

The heuristic version drew the cited/displayed line by stripping fences with a
line-oriented state machine and pairing backtick runs by hand. Measured against
a CommonMark reference over 1,415 real assistant messages, that lost a genuine
citation on 0.14% of them — but mangled the *boundary* of 10.72% of the URLs it
did find, because the trailing-trim rule ran on markdown it had never parsed.
A bolded link, `**[text](url)**`, kept its emphasis: the `*` was in neither
TRAILING_PUNCT nor the paren-balance rule, and by ending the URL in `*` it also
stopped the paren rule from ever firing. 116 of 4,893 rows in the live index
were stored that way and can never resolve a title.

The fix is not a longer punctuation list — `*` and `_` are legal URL characters
(RFC 3986 makes `_` unreserved and `*` a sub-delimiter), so trimming them
unconditionally corrupts real URLs. It is to let a parser say where the link
destination ends, and to run the bare-URL matcher only on text the parser has
already stripped of markdown.

Reported by an external audit of v0.10.0 (2026-08-22).
"""

from __future__ import annotations

from zotero_capture.url_processing import extract_urls

# --- the boundary bug that dominated real traffic ---


def test_a_bolded_link_does_not_keep_its_emphasis():
    """The 10.72% case: `**[text](url)**` used to store the URL with ")**"."""
    text = "See **[the paper](https://fixturehost.org/abs/2602.06718)** for detail."
    assert extract_urls(text) == ["https://fixturehost.org/abs/2602.06718"]


def test_a_bolded_bare_url_does_not_keep_its_emphasis():
    text = "Live at **https://fixturehost.org/artifact/022b9b86** now."
    assert extract_urls(text) == ["https://fixturehost.org/artifact/022b9b86"]


def test_an_italic_link_does_not_keep_its_emphasis():
    text = "See *[the paper](https://fixturehost.org/x)* for detail."
    assert extract_urls(text) == ["https://fixturehost.org/x"]


def test_an_asterisk_that_is_url_data_survives():
    """Negative control: `*` is a legal sub-delimiter, so trimming it corrupts.

    This is why the fix cannot be `TRAILING_PUNCT += "*_"`.
    """
    assert extract_urls("<https://fixturehost.org/s?q=**>") == [
        "https://fixturehost.org/s?q=**"
    ]


def test_an_underscore_that_is_url_data_survives():
    """Negative control: `_` is unreserved, and real paths end in one."""
    assert extract_urls("Tag <https://fixturehost.org/release_> shipped.") == [
        "https://fixturehost.org/release_"
    ]


# --- container bugs the state machine got backwards ---


def test_a_fence_inside_a_list_item_is_still_code():
    """The state machine returned the DISPLAYED url and dropped the real one."""
    text = (
        "- ```\n"
        "  GET https://fixturehost.org/displayed\n"
        "  ```\n"
        "\n"
        "Cited https://fixturehost.org/real\n"
    )
    assert extract_urls(text) == ["https://fixturehost.org/real"]


def test_a_blockquote_fence_closes_at_the_container_boundary():
    """CommonMark closes an unclosed fence when its container ends."""
    text = "> ```\n> displayed output\n\nCited https://fixturehost.org/real\n"
    assert extract_urls(text) == ["https://fixturehost.org/real"]


def test_a_stray_backtick_cannot_mask_a_later_citation():
    text = (
        "Before ` stray\n```\ncode\n```\nCited https://fixturehost.org/real ` later\n"
    )
    assert extract_urls(text) == ["https://fixturehost.org/real"]


# --- what the parser tells us that a scan could not ---


def test_a_link_label_that_looks_like_a_url_is_not_cited():
    """`[displayed](cited)` cites the destination; the label is just text."""
    text = "[https://fixturehost.org/displayed](https://fixturehost.org/cited)"
    assert extract_urls(text) == ["https://fixturehost.org/cited"]


def test_an_image_is_not_a_source():
    """A badge is a page asset, not something the message cited."""
    text = "![build](https://fixturehost.org/badge.svg) and https://fixturehost.org/p"
    assert extract_urls(text) == ["https://fixturehost.org/p"]


def test_a_url_inside_inline_html_is_not_cited():
    text = '<a href="https://fixturehost.org/raw">x</a> and https://fixturehost.org/p'
    assert extract_urls(text) == ["https://fixturehost.org/p"]


def test_an_autolink_keeps_its_entity_unchanged():
    """CommonMark autolink content IS the URL — it is not entity-decoded."""
    assert extract_urls("<https://fixturehost.org/?x=a&amp;b>") == [
        "https://fixturehost.org/?x=a&amp;b"
    ]


def test_a_link_destination_has_its_entity_decoded():
    """CommonMark requires entity references in a destination to be decoded.

    This is the property `canonicalize` used to fake with a blanket &amp; fold,
    which also corrupted a literal `&amp;` that was genuine URL data.
    """
    assert extract_urls("[x](https://fixturehost.org/?x=a&amp;b)") == [
        "https://fixturehost.org/?x=a&b"
    ]
