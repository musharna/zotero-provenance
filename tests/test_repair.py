"""The repair pass must fix the damage without inventing URLs nobody cited.

116 rows in the deployed index end in markdown emphasis the old extractor kept.
They can never resolve a title, so they sit in the library as URL-as-title junk.
The extractor rewrite stops new ones; these tests cover cleaning up the old.

The interesting part is what it refuses to touch. Some mangled-looking rows are
not URLs at all — a regex like "https://data\\.gramene\\.org/v69/genes.*" was
captured as one — and "fixing" the trailing ".*" would fabricate a URL that was
never cited. Others gained an "=" from the old canonicaliser, and "?a=1&b=" and
"?a=1&b" cannot be told apart after the fact.
"""

from __future__ import annotations

import pytest

from zotero_capture.repair import correct_url, plan_repair


def _row(url: str, key: str = "KEY1") -> dict:
    return {"url_canonical": url, "zotero_key": key}


@pytest.mark.parametrize(
    "mangled, expected",
    [
        ("https://h.example/abs/2602.06718)**", "https://h.example/abs/2602.06718"),
        (
            "https://h.example/artifact/022b9b86**",
            "https://h.example/artifact/022b9b86",
        ),
        ("https://h.example/p)", "https://h.example/p"),
        # Stripping "**" can uncover a trailing slash, which is not canonical.
        # Left as-is the row would never match a lookup, so the repair would add
        # a permanent near-duplicate instead of removing one.
        ("https://h.example/bios/**", "https://h.example/bios"),
    ],
)
def test_correct_url_strips_the_markdown_that_was_kept(mangled, expected):
    assert correct_url(mangled) == expected


@pytest.mark.parametrize(
    "clean",
    [
        "https://h.example/wiki/Aestivation_(botany)",
        "https://h.example/pii/S0092867400808763",
        "https://h.example/s?q=a+b",
        # RFC 3986 makes "_" unreserved and "*" a sub-delimiter. An earlier cut
        # of this repair stripped both and proposed to "fix" the first of these,
        # which is a real URL in the live index.
        "https://foo.example/release_",
        "https://h.example/path*",
    ],
)
def test_correct_url_leaves_a_healthy_url_alone(clean):
    """Negative control: these characters are URL data, not markdown."""
    assert correct_url(clean) == clean


def test_a_url_legitimately_ending_in_a_bold_run_would_be_altered():
    """The limit of this repair, asserted rather than left as a surprise.

    "<https://h.example/s?q=**>" is a legal autolink whose query really does end
    in two asterisks, and nothing in the stored string distinguishes it from a
    URL that picked them up from bold markdown. The repair cannot tell, which is
    why it is a one-off over an inspected set rather than a general cleaner: the
    dry run lists every change so the set can be checked before it is applied.
    No row in the live index is of this kind.
    """
    assert correct_url("https://h.example/s?q=**") == "https://h.example/s?q="


def test_a_single_trailing_asterisk_is_reported_not_repaired():
    """One "*" is ambiguous; in the live index it marks wildcards, not damage."""
    steps = plan_repair([_row("https://h.example/path*")])
    assert [s.action for s in steps] == ["skip"]
    assert steps[0].corrected == "", "an ambiguous row must not carry a correction"


def test_a_wildcard_pattern_is_reported_not_repaired():
    steps = plan_repair([_row("https://*.example.com/*")])
    assert [s.action for s in steps] == ["skip"]
    assert "not a URL" in steps[0].reason


def test_a_mangled_row_whose_correction_is_new_is_rewritten():
    steps = plan_repair([_row("https://h.example/p**")])
    assert [s.action for s in steps] == ["rewrite"]
    assert steps[0].corrected == "https://h.example/p"


def test_a_mangled_row_whose_correction_exists_is_merged():
    steps = plan_repair(
        [_row("https://h.example/p**", "BAD1"), _row("https://h.example/p", "GOOD1")]
    )
    merges = [s for s in steps if s.action == "merge"]
    assert len(merges) == 1
    assert merges[0].zotero_key == "BAD1", "the mangled copy is the one to retire"


def test_a_healthy_row_produces_no_step():
    """Positive control: the pass must not churn rows that are already right."""
    assert plan_repair([_row("https://h.example/wiki/X_(y)")]) == []


def test_a_regex_captured_as_a_url_is_reported_not_repaired():
    steps = plan_repair([_row("https://data\\.gramene\\.org/v69/genes.*")])
    assert [s.action for s in steps] == ["skip"]
    assert "not a URL" in steps[0].reason


def test_an_escaped_string_is_never_rewritten():
    """The dangerous case: stripping ".*" would fabricate a plausible URL."""
    steps = plan_repair([_row("https://rest\\.uniprot\\.org/uniprotkb/search.*")])
    assert steps[0].action == "skip"
    assert steps[0].corrected == ""


def test_a_query_that_gained_an_equals_is_left_alone():
    """Ambiguous after the fact, and both forms are valid: guessing has no upside."""
    assert plan_repair([_row("https://h.example/x?sig=a&b=")]) == []


def test_a_row_with_no_item_yet_is_skipped():
    steps = plan_repair([_row("https://h.example/p**", "")])
    assert steps[0].action == "skip"
    assert "claim" in steps[0].reason
