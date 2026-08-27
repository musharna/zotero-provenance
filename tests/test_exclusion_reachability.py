"""A rule the capture path enforces must be reachable by the tools that clean up.

The rules lived in two copies. `url_processing.is_excluded` decides what capture
refuses; `retire.classify` decided what maintenance could reach. Nothing kept
them in step, and the second one was stale.

0.31.0 is the proof. It added the malformed-DOI rule and its release notes said
the junk rows it described would be swept. Six of them were still sitting in the
live collection days later -- refused by capture, invisible to every maintenance
tool -- because `classify` had never heard of the rule. The release note was
written from what the code should have implied, not from a run.

So this file does not test DOIs. It tests the PROPERTY that made that possible:
anything capture refuses must be something maintenance can name. A future rule
added to `is_excluded` and nowhere else has to fail here.
"""

from __future__ import annotations

import pytest

from zotero_capture.retire import HARD, POLICY, classify
from zotero_capture.url_processing import is_excluded

# One URL per rule inside `is_excluded`, named by the rule it exercises. When a
# rule is added there, add its spelling here -- and if the fallthrough is doing
# its job, the new entry passes without touching `classify` at all.
EXCLUDED_BY_CAPTURE = [
    ("no host at all", "file:///etc/passwd"),
    ("reserved name (RFC 2606)", "https://example.com/x"),
    ("infrastructure host", "https://fonts.googleapis.com/css"),
    ("tailnet suffix", "https://box.tail1234.ts.net/doc"),
    ("page asset", "https://fixturehost.org/static/app.min.js"),
    ("malformed DOI", "https://doi.org/10.1/ABC"),
    ("bare doi.org, no DOI at all", "https://doi.org"),
    ("private address", "http://192.168.1.10/admin"),
    ("loopback in decimal notation", "http://2130706433/"),
    ("single-label name", "http://prometheus:9090/graph"),
]

# Real sources. If `classify` ever names one of these, the cleanup tools have
# become a hazard rather than a chore -- so these are the control on every
# assertion below.
KEPT = [
    "https://doi.org/10.1002/2016jg003520",
    "https://arxiv.org/abs/2401.00001",
    "https://www.biorxiv.org/content/10.1101/2020.01.01.900001v1",
    "https://fixturehost.org/papers/recruitment.html",
]


@pytest.mark.parametrize(
    "rule,url", EXCLUDED_BY_CAPTURE, ids=[r for r, _ in EXCLUDED_BY_CAPTURE]
)
def test_capture_refuses_it(rule: str, url: str) -> None:
    """Precondition. If this fails the fixture is wrong, not the property."""
    assert is_excluded(url) is True, f"fixture for {rule!r} is not actually excluded"


@pytest.mark.parametrize(
    "rule,url", EXCLUDED_BY_CAPTURE, ids=[r for r, _ in EXCLUDED_BY_CAPTURE]
)
def test_maintenance_can_name_it(rule: str, url: str) -> None:
    """THE property. A row capture would refuse must not be invisible to the
    tools that exist to clear it: unreachable is how six rows stayed put while
    a release note said they had been swept."""
    tier, reason = classify(url)
    assert reason, f"is_excluded refuses {url!r} ({rule}) but classify leaves it alone"
    assert tier in (HARD, POLICY)


@pytest.mark.parametrize("url", KEPT)
def test_a_real_source_is_left_alone(url: str) -> None:
    """Positive control. A fallthrough that named everything would satisfy the
    test above completely while making --policy delete the library."""
    assert is_excluded(url) is False
    assert classify(url) == ("", "")


def test_a_rule_capture_does_not_have_stays_out_of_reach() -> None:
    """The fallthrough must not invent exclusions of its own. `classify` reaching
    further than `is_excluded` would be the same defect pointing the other way,
    and that direction destroys rows instead of stranding them."""
    url = "https://fixturehost.org/an/ordinary/document"
    assert is_excluded(url) is False
    assert classify(url) == ("", "")


def test_the_fallthrough_is_opt_in() -> None:
    """Reaching back to trash a row on the strength of a rule written after it
    was captured is a product decision. It must land in the tier that requires
    --policy, never in the one that applies by default."""
    tier, _ = classify("https://doi.org/10.1/ABC")
    assert tier == POLICY
