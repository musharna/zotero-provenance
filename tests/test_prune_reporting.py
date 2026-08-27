"""What prune SAYS it did has to match what it did.

`prune` appended every URL the rules rejected to one list, before attempting the
trash, and the CLI printed that list as "trashed: <url>". So an item the CAS
guard refused -- the guard that exists precisely because the walk completes
minutes before the write, and the item may have moved underneath us -- was
itemised as trashed while the summary counted zero:

    trashed: https://evil.example.com/x
    trashed   : 0

Both lines from the same run. And `skipped` was never printed at all, so the
safety guard fired in silence: the operator was told an item was removed, and
never told that the refusal had happened.

test_selection_toctou.py already promised this in its own docstring -- "a
refused delete must not be reported as one" -- and asserted it only of the
counters. The itemised list, one layer up, went on breaking that promise.

The list is now split by OUTCOME rather than by selection, so there is no
ambiguous list left for a reader to misinterpret.
"""

from __future__ import annotations

import sys

from conftest import PLUGIN_ROOT

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from zotero_capture.prune import format_prune_report, prune  # noqa: E402

EXCLUDED = "https://evil.example.com/x"
OTHER = "https://evil.example.com/y"


class _Zotero:
    """A collection whose trash outcome is decided per key."""

    def __init__(self, items: list[tuple[str, str]], outcome) -> None:
        self._items = items
        self._outcome = outcome
        self.asked: list[str] = []

    def iter_collection_items(self):
        for key, url in self._items:
            yield {"key": key, "data": {"url": url}}

    def trash_item(self, key, *, expect_url=None):
        self.asked.append(key)
        result = self._outcome(key)
        if isinstance(result, Exception):
            raise result
        return result


def test_a_refused_item_is_not_listed_as_trashed() -> None:
    """The defect. The CAS guard refused, so nothing was removed."""
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: False)

    result = prune(zotero, sleep_s=0)

    assert result.trashed_urls == [], "a refused item was listed as trashed"
    assert result.skipped_urls == [EXCLUDED]
    assert result.trashed == 0 and result.skipped == 1


def test_a_trashed_item_is_listed_as_trashed() -> None:
    """Positive control. Without it, a result that listed nothing anywhere would
    satisfy every assertion above."""
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: True)

    result = prune(zotero, sleep_s=0)

    assert result.trashed_urls == [EXCLUDED]
    assert result.skipped_urls == []


def test_an_item_that_raised_is_not_listed_as_trashed() -> None:
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: RuntimeError("boom"))

    result = prune(zotero, sleep_s=0)

    assert result.trashed_urls == []
    assert result.error_urls == [EXCLUDED]
    assert result.errors == 1


def test_a_mixed_run_attributes_each_url_to_its_own_outcome() -> None:
    zotero = _Zotero(
        [("GOOD1", EXCLUDED), ("MOVED", OTHER)],
        lambda key: key == "GOOD1",
    )

    result = prune(zotero, sleep_s=0)

    assert result.trashed_urls == [EXCLUDED]
    assert result.skipped_urls == [OTHER]


def test_the_report_does_not_claim_a_refused_item_was_trashed() -> None:
    """The line a human actually reads."""
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: False)

    lines = format_prune_report(prune(zotero, sleep_s=0), dry_run=False)
    body = "\n".join(lines)

    assert f"trashed: {EXCLUDED}" not in body, body
    assert f"refused: {EXCLUDED}" in body, body


def test_the_report_shows_the_refusal_count() -> None:
    """A safety guard that fires in silence is not a safety guard."""
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: False)

    body = "\n".join(format_prune_report(prune(zotero, sleep_s=0), dry_run=False))

    assert "refused" in body and "1" in body, body


def test_the_report_still_names_what_was_trashed() -> None:
    """Positive control for both report tests above."""
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: True)

    body = "\n".join(format_prune_report(prune(zotero, sleep_s=0), dry_run=False))

    assert f"trashed: {EXCLUDED}" in body, body


def test_a_dry_run_lists_what_it_would_remove_and_writes_nothing() -> None:
    zotero = _Zotero([("ITEM1", EXCLUDED)], lambda key: True)

    result = prune(zotero, dry_run=True, sleep_s=0)
    body = "\n".join(format_prune_report(result, dry_run=True))

    assert zotero.asked == [], "a dry run wrote"
    assert result.selected == [EXCLUDED]
    assert result.trashed_urls == []
    assert f"would trash: {EXCLUDED}" in body, body
