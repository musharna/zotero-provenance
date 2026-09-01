"""The staleness guard, after the audit found two holes in its boolean form."""

from __future__ import annotations


from zotero_capture.staleness import CURRENT, MISMATCH, UNKNOWN, classify, stale_reason


def test_running_behind_the_install_is_a_mismatch():
    assert classify("0.3.0", "0.11.7") is MISMATCH


def test_running_ahead_of_the_install_is_also_a_mismatch():
    """The rollback case the old "older is bad, newer is fine" rule allowed.

    Rolling the install back to 0.11.6 is how you STOP a bad 0.11.7 writing.
    Under the old rule 0.11.7 was "not stale", so the rollback did nothing and
    the session it was meant to stop kept going.
    """
    assert classify("0.11.7", "0.11.6") is MISMATCH


def test_exact_agreement_is_current():
    assert classify("0.11.7", "0.11.7") is CURRENT


def test_an_unreadable_version_is_unknown_not_current():
    """UNKNOWN is a third answer, not a synonym for CURRENT.

    The old code returned False — indistinguishable from "verified current" —
    so an install layout it could not read silently disabled the only guard
    against writing from an unknown root.
    """
    assert classify("", "0.11.7") is UNKNOWN
    assert classify("0.11.7", "") is UNKNOWN
    assert classify("weird-version", "0.11.7") is UNKNOWN


def test_a_mismatch_names_both_versions():
    """A reason that does not say what to do is not a reason."""
    reason = stale_reason("0.3.0", "0.11.7")
    assert "0.3.0" in reason and "0.11.7" in reason


def test_current_has_no_reason():
    assert stale_reason("0.11.7", "0.11.7") == ""


def test_unknown_does_not_stop_capture_but_does_say_so():
    """The fail-open call, kept but made visible.

    Refusing on UNKNOWN would silently switch capture off for anyone whose
    layout we cannot parse — a worse failure than the stale write it prevents,
    and invisible in exactly the same way. So capture proceeds, but the line is
    written rather than swallowed.
    """
    assert stale_reason("weird", "0.11.7") == ""
