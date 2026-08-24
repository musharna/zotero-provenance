"""Regressions from the external audit of 0.11.5, one test per finding.

Each of these was reproduced against HEAD before the fix was written. They are
kept together because they share a cause: three successive attempts to decide
where a bare URL ends in prose with a character class of our own. Boundaries now
come from linkify-it-py, and the URI grammar validates the result instead of
delimiting it.
"""

from __future__ import annotations

import pytest

from zotero_capture.repair import repaired_url
from zotero_capture.url_processing import extract_urls

WIKI = "https://en.wikipedia.org/wiki/People's_Republic_of_China"


def test_an_apostrophe_url_is_not_truncated_into_a_different_page():
    """The worst of the findings: the prefix is ITSELF a real Wikipedia page.

    Storing ".../wiki/People" resolves a plausible title and reads as a citation
    that was never made — quiet wrong data, which is the failure mode the whole
    0.11.2 guard existed to prevent. The apostrophe is an RFC 3986 sub-delimiter;
    excluding it on seven corpus observations did not survive one live example.
    """
    assert extract_urls(f"See {WIKI} here") == [WIKI]


def test_a_shell_quoted_url_still_loses_only_the_quote():
    """The pairing test: the opener in front proves the trailing quote is syntax."""
    assert extract_urls(f"'{WIKI}'") == [WIKI]


def test_repair_does_not_truncate_an_apostrophe_url_either():
    assert repaired_url(WIKI) == ""


@pytest.mark.parametrize(
    "text",
    [
        "fetch https://files.rcsb.org/download/{ID}.pdb now",
        "fetch https://files.rcsb.org/download/{{ID}}.pdb now",
    ],
)
def test_a_template_is_dropped_however_many_braces_it_has(text):
    """A one-character lookahead was defeated by a doubled delimiter: the second
    "{" made it conclude that URL text had not resumed."""
    assert extract_urls(text) == []


def test_a_curly_quote_is_not_url_data():
    assert extract_urls("See “https://example.org/path” next") == [
        "https://example.org/path"
    ]


def test_a_non_breaking_space_ends_the_url():
    """The IRI range began AT U+00A0, so a match could cross a visible word
    boundary and swallow the next word."""
    assert extract_urls("See https://example.org/a next") == [
        "https://example.org/a"
    ]


def test_an_ansi_sequence_inside_a_url_is_removed_not_cut_at():
    """Escape codes wrap an address rather than end one, so cutting at the ESC
    invents a shorter URL that resolves."""
    assert extract_urls("see https://example.org/a\x1b[31mcontinued now") == [
        "https://example.org/acontinued"
    ]


def test_repair_cannot_emit_what_capture_would_reject():
    """Repair turned "…/filter[name]|" into "…/filter" — shorter, resolvable,
    and something the tokenizer would never have produced."""
    assert repaired_url("https://host.example/filter[name]|") == ""


def test_an_unbalanced_closer_is_not_evidence_the_url_ended():
    """The old rule treated any closer as proof, so this stored the prefix."""
    assert extract_urls("https://example.org/path}suffix here") == []
