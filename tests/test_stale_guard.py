"""A stale plugin root must not write to the library.

On 2026-08-23 a session whose plugin root was still v0.3.0 captured eight junk
URLs into the collection — "https://example.org/bar" among them. v0.3.0 predates
every exclusion rule, so nothing stopped it: the reserved-name test that would
have refused all eight did not exist in the code that was running.

That is the deployment model biting rather than a parser defect. The cache is
keyed by version and a session holds whichever root it resolved at its own
start, so a long-lived session goes on executing an old release indefinitely
while the repo, the clone and the tests all say the bug is fixed.

The guard's honest limit, stated here because it is the first thing to wonder:
it cannot save a version that predates it. v0.3.0 will never refuse itself. It
closes the door from this release forward, and the pre-guard roots have to be
removed rather than reasoned with.
"""

from __future__ import annotations

import pytest

from zotero_capture.staleness import is_stale, stale_reason


@pytest.mark.parametrize(
    "running, installed",
    [
        ("0.3.0", "0.11.7"),
        ("0.11.5", "0.11.6"),
        ("0.9.0", "0.10.0"),
        ("1.2.3", "1.10.0"),  # numeric, not lexicographic: 10 > 2
    ],
)
def test_an_older_running_version_is_stale(running, installed):
    assert is_stale(running, installed) is True


@pytest.mark.parametrize(
    "running, installed",
    [
        ("0.11.6", "0.11.6"),
        ("0.11.7", "0.11.6"),  # a dev checkout ahead of the clone is fine
        ("1.10.0", "1.2.3"),
    ],
)
def test_a_current_or_newer_version_is_not_stale(running, installed):
    """Positive control: the guard must not refuse an up-to-date session."""
    assert is_stale(running, installed) is False


@pytest.mark.parametrize(
    "running, installed",
    [
        ("", "0.11.6"),
        ("0.11.6", ""),
        ("nonsense", "0.11.6"),
        ("0.11.6", "nonsense"),
    ],
)
def test_an_unreadable_version_never_blocks_capture(running, installed):
    """Fail OPEN here, deliberately, and it is the opposite of the usual rule.

    Everywhere else in this plugin an unanswerable question means refuse. Not
    here: the cost of a false positive is that capture silently stops for a user
    whose install layout we simply could not read, which is worse than the rare
    stale write this misses. The guard only fires when both versions parse and
    the comparison is unambiguous.
    """
    assert is_stale(running, installed) is False


def test_the_reason_names_both_versions():
    """The log line has to be actionable: "restart" is only obvious if it says
    what is running and what is installed."""
    reason = stale_reason("0.3.0", "0.11.7")
    assert "0.3.0" in reason and "0.11.7" in reason
    assert stale_reason("0.11.7", "0.11.7") == ""
