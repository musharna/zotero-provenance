"""Bucket Zotero items into source-delta categories + emit markdown."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .url_processing import NO_CAPTURE_MARKER


SEEN_RE = re.compile(r"^seen:(\d{4}-\d{2}-\d{2})$")

BACKTICK_RUN_RE = re.compile(r"`+")

BUCKET_KEYS = (
    "new",
    "persisting",
    "recurring_untriaged",
    "recurring_triaged",
    "dropped",
)


def seen_dates(item: dict[str, Any]) -> list[date]:
    out = []
    for tag in item["data"].get("tags", []):
        m = SEEN_RE.match(tag["tag"])
        if m:
            try:
                out.append(date.fromisoformat(m.group(1)))
            except ValueError:
                pass
    return sorted(out)


def _has_tag(item: dict[str, Any], tag: str) -> bool:
    return any(t["tag"] == tag for t in item["data"].get("tags", []))


def bucket_items(items: list[dict[str, Any]], *, run_started: date) -> dict[str, list]:
    """Sort items by how their `seen:` history relates to this run."""
    buckets: dict[str, list] = {key: [] for key in BUCKET_KEYS}
    for item in items:
        seen = seen_dates(item)
        prior = [d for d in seen if d < run_started]
        today_seen = run_started in seen
        triaged = _has_tag(item, "triaged")

        if triaged and len(prior) >= 2 and today_seen:
            buckets["recurring_triaged"].append(item)
        elif not triaged and len(prior) >= 2 and today_seen:
            buckets["recurring_untriaged"].append(item)
        elif len(prior) == 1 and today_seen:
            buckets["persisting"].append(item)
        elif today_seen and not prior:
            buckets["new"].append(item)
        elif prior and not today_seen:
            buckets["dropped"].append(item)
        # else: oddities (e.g. only future seen dates) — skip silently
    return buckets


def _md_link(item: dict[str, Any]) -> str:
    """Show the source without citing it.

    Deliberately NOT a markdown link. Claude presents this report, and the Stop
    hook reads what Claude prints, so a link here re-captured every listed source
    the instant the report was displayed — including the ones being reported as
    dropped. Backticks mark a URL as shown rather than cited, which is the same
    rule extraction applies everywhere else.

    The cost is that the URL is no longer clickable from the report. These are
    items already in the library, so Zotero is where you would open them anyway.

    The title goes in a code span too, and that is not cosmetic. A title is
    whatever a fetched page put in its <title>, so it is untrusted text pasted
    into markdown, and the commonest junk title in this library is a bare URL —
    the very thing the report exists to list. Bolded as prose, those titles were
    read straight back as fresh citations. Backslash-escaping does not help
    either: escaping the brackets of a title like "[here](http://…)" still
    leaves the URL sitting in prose. Only marking it as a literal does.
    """
    return (
        f"**{_code_span(item['data'].get('title') or item['data']['url'])}**"
        f" — {_code_span(item['data']['url'])}"
    )


def _code_span(text: str) -> str:
    """Wrap text in a code span its own content cannot close.

    Backslash escapes do not apply inside a code span, so a backtick in the text
    cannot be escaped — CommonMark's answer is a longer delimiter than any run
    within, plus a space of padding when the text itself starts or ends with a
    backtick (the renderer strips one such space from each end).
    """
    longest = max((len(m.group(0)) for m in BACKTICK_RUN_RE.finditer(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def emit_markdown(
    items: list[dict[str, Any]],
    *,
    run_started: date,
    context_name: str,
    since_days: int,
) -> str:
    buckets = bucket_items(items, run_started=run_started)
    prior_runs = sorted(
        {d for item in items for d in seen_dates(item) if d < run_started}
    )

    lines = [
        NO_CAPTURE_MARKER,
        "",
        f"## Source delta — context:{context_name}",
        "",
        f"**Window:** last {since_days} days · **This run:** {run_started.isoformat()} · "
        f"**Prior runs in window:** {len(prior_runs)} "
        f"({', '.join(d.isoformat() for d in prior_runs) or 'none'})",
        "",
    ]

    def section(title: str, key: str, formatter):
        if not buckets[key]:
            return
        lines.append(f"### {title} ({len(buckets[key])})")
        lines.append("")
        for item in buckets[key]:
            lines.append(f"- {formatter(item)}")
        lines.append("")

    section("New", "new", lambda i: f"{_md_link(i)} — first surfaced today")
    section(
        "Persisting",
        "persisting",
        lambda i: (
            f"{_md_link(i)} — seen {len(seen_dates(i))}× "
            f"({', '.join(d.isoformat() for d in seen_dates(i))})"
        ),
    )
    section(
        "Recurring (untriaged)",
        "recurring_untriaged",
        lambda i: (
            f"{_md_link(i)} — seen {len(seen_dates(i))}× over "
            f"{(seen_dates(i)[-1] - seen_dates(i)[0]).days} days; "
            "consider `/triage` if no further action is expected"
        ),
    )
    section(
        "Recurring (triaged — informational)",
        "recurring_triaged",
        lambda i: f"{_md_link(i)} — seen {len(seen_dates(i))}× (triaged)",
    )
    section(
        "Dropped since last run",
        "dropped",
        lambda i: (
            f"{_md_link(i)} — last seen {seen_dates(i)[-1].isoformat()}, "
            "not surfaced this run"
        ),
    )

    if all(not v for v in buckets.values()):
        lines.append("_No URLs surfaced this run match prior runs in the window._")
        lines.append("")

    return "\n".join(lines)
