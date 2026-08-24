"""The bare-URL tokenizer admits what the URI grammar permits, nothing else.

URL_RE's general branch used to be a character *blacklist* — anything absent
from ``[^\\s<>"'`\\]]`` was taken as URL data. A blacklist excludes only what
someone thought of, so characters RFC 3986 never permits unencoded were carried
into the stored address: ``https://example.org/a|`` from prose, ``{ID}.pdb|.cif``
from a template, a raw ANSI escape from pasted terminal output. A URL that
cannot resolve degrades into exactly the URL-as-title junk this plugin removes.

This is producer-side, not consumer-side: lengthening TRAILING_PUNCT was
rejected in 0.11.0 because ``_`` is unreserved and ``*`` a sub-delimiter, and it
would only ever fix the trailing position anyway. The grammar decides instead.

Measured over 1,370 real assistant messages / 4,009 extracted URLs, the switch
changes **nothing** — every illegal character in that corpus already sat inside
a code span or fence, which the AST skips. It removes the mechanism rather than
a measured defect rate, at zero cost to real traffic. A deliberately broken
variant that admits a space changed 218 of those messages, so the corpus
harness could tell the difference.

Found 2026-08-22 while verifying that v0.11.0 was executing.
"""

from __future__ import annotations

from zotero_capture.url_processing import extract_urls

# --- characters the grammar forbids must never enter the match ---


def test_a_trailing_pipe_in_prose_is_not_part_of_the_url():
    """No table involved: the blacklist simply had no opinion about "|"."""
    assert extract_urls("see https://example.org/a| next") == ["https://example.org/a"]


def test_an_unpadded_table_cell_does_not_leak_its_delimiters():
    """A padded row was clean only because whitespace stopped the match."""
    assert extract_urls("|repo|https://github.com/musharna/ARFDSynInt.git|") == [
        "https://github.com/musharna/ARFDSynInt.git"
    ]


def test_a_brace_placeholder_is_dropped_rather_than_absorbed():
    """Not stored as "https://example.org/" either — see _stopped_mid_literal."""
    assert extract_urls("https://example.org/{ID}.pdb|.cif here") == []


def test_a_raw_ansi_escape_terminates_the_url():
    """Pasted terminal output stored the reset sequence as part of the path."""
    assert extract_urls("https://sqlalche.me/e/20/e3q8\x1b[0m\x1b[4;94m") == [
        "https://sqlalche.me/e/20/e3q8"
    ]


def test_a_backslash_is_not_url_data():
    assert extract_urls(r"https://cloud.r-project.org\ mirror") == [
        "https://cloud.r-project.org"
    ]


# --- positive controls: everything the grammar DOES permit must survive ---
#
# Without these the tests above pass on a tokenizer that matches nothing at all.


def test_every_sub_delimiter_survives():
    """RFC 3986 sub-delims are URL data; trimming them corrupts real addresses."""
    url = "https://example.org/p!$&*+,;=/x_y-z.~q?a=1&b=2#frag"
    assert extract_urls(f"see {url} ok") == [url]


def test_an_apostrophe_ends_the_url_although_the_grammar_allows_it():
    """A narrowing departure from RFC 3986, taken on evidence rather than taste.

    Across 1,370 real assistant messages every apostrophe adjacent to a URL was
    a delimiter -- a shell quote, a Python string, an English possessive -- and
    none was URL data. Admitting it would corrupt those seven; excluding it only
    costs a URL that could have written %27.
    """
    assert extract_urls("cloned https://github.com/musharna/figcite.git' today") == [
        "https://github.com/musharna/figcite.git"
    ]


def test_percent_encoding_survives():
    url = "https://example.org/a%20b%2Fc?q=%7Bx%7D"
    assert extract_urls(f"see {url} ok") == [url]


def test_an_unencoded_non_ascii_path_survives():
    """RFC 3987: a real URL may carry UTF-8 unencoded, and browsers accept it.

    A strict-ASCII whitelist truncated this to ".../wiki/M" — the same class of
    damage as the "]" truncation that once broke every IPv6 URL.
    """
    url = "https://de.wikipedia.org/wiki/München"
    assert extract_urls(f"see {url} ok") == [url]


def test_a_bracketed_ipv6_url_still_matches_through_its_own_branch():
    """"[" and "]" stay out of the general branch; the host branch owns them."""
    assert extract_urls("try https://[2001:db8::1]:8443/x now") == [
        "https://[2001:db8::1]:8443/x"
    ]


def test_a_balanced_paren_is_still_the_balance_rule_to_judge():
    url = "https://doi.org/10.1016/s0092-8674(00)80876-3"
    assert extract_urls(f"see {url}.") == [url]


# --- a truncated match is evidence, not a citation ---
#
# The whitelist changed how a template fails. 0.11.0 stored
# "https://files.rcsb.org/download/{ID}.pdb", which no exclusion rule caught but
# which could never resolve, so it failed loudly as URL-as-title junk. 0.11.1
# stops at "{" and stores "https://files.rcsb.org/download/" — a real, fetchable
# directory that will acquire a genuine title and read as a citation nobody made.
# Quiet wrong data is worse than loud junk.
#
# The signal is that the text CONTINUES past the illegal character with more URL
# material: "{" followed by "ID}.pdb" means the run was a template. A closing
# quote followed by a space means the URL simply ended and the prose resumed.


def test_a_template_in_prose_is_dropped_rather_than_stored_truncated():
    assert extract_urls("fetch https://files.rcsb.org/download/{ID}.pdb now") == []


def test_a_regex_literal_survives_because_commonmark_unescapes_it():
    r"""A known limit, recorded rather than hidden.

    CommonMark treats ``\.`` as an escaped literal, so the parser hands over
    ``https://data.gramene.org/v69/genes.*`` with no illegal character left for
    either the grammar or the truncation guard to notice — and ``*`` is a legal
    sub-delimiter. Two such rows are in the live index. They belong to the
    wildcard/regex class, which needs the code-block judgement, not the grammar.
    """
    assert extract_urls(r"matches https://data\.gramene\.org/v69/genes.* here") == [
        "https://data.gramene.org/v69/genes.*"
    ]


def test_a_two_placeholder_path_is_dropped():
    assert extract_urls("see https://atted.jp/api/coex/Ath-u/{locus}/{top_n} there") == []


def test_a_quoted_url_is_still_a_citation():
    """The terminator is illegal but nothing URL-like follows it: the URL ended."""
    assert extract_urls('he cited "https://example.org/a/b" earlier') == [
        "https://example.org/a/b"
    ]


def test_a_trailing_pipe_still_yields_the_url_not_a_drop():
    """"|" then a space: the pipe is punctuation, not the middle of a template."""
    assert extract_urls("see https://example.org/a| next") == ["https://example.org/a"]


def test_an_ansi_suffix_still_yields_the_url():
    """After the escape comes "[", also illegal — so the run did not continue."""
    assert extract_urls("https://sqlalche.me/e/20/e3q8\x1b[0m done") == [
        "https://sqlalche.me/e/20/e3q8"
    ]


def test_a_plain_url_at_the_end_of_a_sentence_is_untouched():
    assert extract_urls("see https://example.org/a/b.") == ["https://example.org/a/b"]


def test_a_bracketed_url_at_the_end_of_a_sentence_survives():
    """The guard's own regression: "]" then "." is a wrapped URL, not a template.

    "]" can only close something, so the URL had already ended; the "." that
    follows is the sentence, not resumed URL text.
    """
    text = "Check (https://fixturehost.org/foo), and [https://fixturehost.org/bar]."
    assert extract_urls(text) == [
        "https://fixturehost.org/foo",
        "https://fixturehost.org/bar",
    ]
