"""delta tests — bucket logic is mutually exclusive, first match wins."""

from __future__ import annotations

from datetime import date


from zotero_capture.delta import bucket_items, emit_markdown


def _make_item(key: str, url: str, title: str, tags: list[str]):
    return {
        "key": key,
        "data": {"url": url, "title": title, "tags": [{"tag": t} for t in tags]},
    }


def test_bucket_new_url_only_today():
    items = [
        _make_item(
            "A", "https://x.com/new", "New", ["context:lit-review", "seen:2026-05-05"]
        )
    ]
    buckets = bucket_items(items, run_started=date(2026, 5, 5))
    assert [i["key"] for i in buckets["new"]] == ["A"]
    assert buckets["persisting"] == []
    assert buckets["recurring_untriaged"] == []


def test_bucket_persisting_one_prior_plus_today():
    items = [
        _make_item(
            "A",
            "https://x.com/p",
            "P",
            ["context:lit-review", "seen:2026-04-29", "seen:2026-05-05"],
        )
    ]
    buckets = bucket_items(items, run_started=date(2026, 5, 5))
    assert [i["key"] for i in buckets["persisting"]] == ["A"]
    assert buckets["new"] == []
    assert buckets["recurring_untriaged"] == []


def test_bucket_recurring_untriaged_two_priors_plus_today():
    items = [
        _make_item(
            "A",
            "https://x.com/r",
            "R",
            [
                "context:lit-review",
                "seen:2026-04-16",
                "seen:2026-04-29",
                "seen:2026-05-05",
            ],
        )
    ]
    buckets = bucket_items(items, run_started=date(2026, 5, 5))
    assert [i["key"] for i in buckets["recurring_untriaged"]] == ["A"]
    assert buckets["persisting"] == []


def test_bucket_recurring_triaged_takes_priority():
    items = [
        _make_item(
            "A",
            "https://x.com/r",
            "R",
            [
                "context:lit-review",
                "triaged",
                "seen:2026-04-16",
                "seen:2026-04-29",
                "seen:2026-05-05",
            ],
        )
    ]
    buckets = bucket_items(items, run_started=date(2026, 5, 5))
    assert [i["key"] for i in buckets["recurring_triaged"]] == ["A"]
    assert buckets["recurring_untriaged"] == []


def test_bucket_dropped_prior_no_today():
    items = [
        _make_item(
            "A", "https://x.com/d", "D", ["context:lit-review", "seen:2026-04-29"]
        )
    ]
    buckets = bucket_items(items, run_started=date(2026, 5, 5))
    assert [i["key"] for i in buckets["dropped"]] == ["A"]


def test_emit_markdown_contains_all_sections():
    items = [
        _make_item(
            "N", "https://x.com/new", "New", ["context:lit-review", "seen:2026-05-05"]
        ),
        _make_item(
            "R",
            "https://x.com/recur",
            "Recur",
            [
                "context:lit-review",
                "seen:2026-04-16",
                "seen:2026-04-29",
                "seen:2026-05-05",
            ],
        ),
    ]
    md = emit_markdown(
        items, run_started=date(2026, 5, 5), context_name="lit-review", since_days=90
    )
    assert "## Source delta — context:lit-review" in md
    assert "### New (1)" in md
    assert "### Recurring (untriaged) (1)" in md
    assert "https://x.com/new" in md
    assert "seen 3×" in md
