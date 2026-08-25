"""Capture health: report only when capture is actually broken.

Every serious failure this plugin has had was silent. A v0.3.0 root wrote junk
for weeks; capture stopped dead for 29 hours; both were found by an audit rather
than by the plugin noticing anything. staleness.py already states the principle
— "loud absence beats quiet corruption" — but until now nothing was watching for
the absence, so the corruption stayed quiet anyway.

This reads what the log has recorded since 0.12.0 (`version`, `root`, `errors`)
rather than adding new bookkeeping, which means it can answer for the past as
well as the present.

The design constraint is silence. A check that speaks on a healthy session gets
tuned out, and a tuned-out check is worse than none: that is exactly how the
measurement canary printed its warning for four consecutive releases without
anyone acting on it. So there are four things worth interrupting for and nothing
else, and each names both what is wrong and the number that shows it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable

# What the hooks write when they decline to capture. Any of these appearing
# after the last successful capture means capture is off right now.
REFUSAL_EVENTS = (
    "stale-root-refused",
    "forward-unresolved",
    "forward-loop-refused",
    "missing-dependencies",
)


def _parse_ts(raw: Any) -> datetime | None:
    """Log timestamps are ISO-8601 with an offset, written as -0400 or -04:00."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _records(lines: Iterable[str]) -> list[dict]:
    """Tolerant parse: one corrupt line must not blind the whole check."""
    out: list[dict] = []
    for line in lines:
        text = line.strip() if isinstance(line, str) else ""
        if not text:
            continue
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        record["_ts"] = ts
        out.append(record)
    return out


def _is_capture(record: dict) -> bool:
    """A run that WROTE. A refusal is not one, however similar the line looks.

    `refused` is what separates them. Before it existed, a refused run emitted
    `urls_seen: 0, errors: []` — the same bytes as a healthy message with no
    citable URL — so a refusal reset the silence clock instead of raising it.
    """
    if record.get("refused"):
        return False
    return "event" not in record and ("urls_seen" in record or "version" in record)


def latest_record_ts(lines: Iterable[str]) -> datetime | None:
    """The newest timestamp in the log the caller just read.

    Used as the acknowledgement cursor. Acknowledging "now" would swallow any
    record written between reading and acknowledging; acknowledging the newest
    record actually examined cannot.
    """
    records = _records(lines)
    return max((r["_ts"] for r in records), default=None)


def _version_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1] or root


def evaluate(
    lines: Iterable[str],
    *,
    pinned_root: str | None,
    now: datetime,
    installed_at: datetime | None = None,
    acknowledged_before: datetime | None = None,
) -> list[str]:
    """Human-readable warnings, or an empty list when there is nothing to say.

    Three signals, deliberately fewer than before. Two rounds of audit killed
    the others: counting hook fires chattered after about a hundred URL-free
    turns, and elapsed time measured the user's habits rather than the plugin.

    What survives is what the log can actually prove.
    """
    records = _records(lines)
    if not records:
        return []

    captures = [r for r in records if _is_capture(r)]
    last = max(captures, key=lambda r: r["_ts"]) if captures else None
    warnings: list[str] = []

    # 1. A write happened from a root that was not the pinned one AT THE TIME.
    #
    #    This claim used to be "a live session is executing superseded code",
    #    which the evidence never supported: a record proves a write occurred,
    #    not that its author still exists. One stale record in an unbounded log
    #    then warned at every session start, forever, long after the session
    #    that wrote it had gone — the false negative traded for a false
    #    positive. So the claim shrank to what is provable, and an
    #    acknowledgement cursor means it is said once rather than repeatedly.
    #
    #    A record carrying `pinned_root` decides its own staleness and is read
    #    WITHOUT the current registry: it was already proof, and gating it on a
    #    readable registry threw that proof away. `installed_at` remains the
    #    fallback for lines written before that field existed.
    def _was_stale(record: dict) -> bool:
        root = record.get("root")
        if not isinstance(root, str) or not root:
            return False
        observed = record.get("pinned_root")
        if isinstance(observed, str) and observed:
            return root != observed
        if pinned_root is None or root == pinned_root:
            return False
        return not (installed_at is not None and record["_ts"] <= installed_at)

    stale = [r for r in captures if _was_stale(r)]
    if acknowledged_before is not None:
        stale = [r for r in stale if r["_ts"] > acknowledged_before]
    if stale:
        latest = max(stale, key=lambda r: r["_ts"])
        roots = sorted({_version_of(r["root"]) for r in stale})
        warnings.append(
            f"{len(stale)} capture(s) occurred from plugin {', '.join(roots)}, "
            f"which was not the installed version at the time "
            f"(most recent {latest['_ts'].isoformat()}, {latest['root']})"
        )

    # 2. Hooks fired and declined. A successful capture clears these, so unlike
    #    the incident above they need no cursor: if they persist, capture really
    #    is still off. The count is capped in the message because a long-broken
    #    install produces an absurd number that says nothing extra.
    since = last["_ts"] if last else None
    refusals = [
        r
        for r in records
        if (r.get("event") in REFUSAL_EVENTS or r.get("refused"))
        and (since is None or r["_ts"] > since)
    ]
    if refusals:
        kinds = sorted({str(r.get("event") or r.get("refused")) for r in refusals})[:3]
        newest = max(refusals, key=lambda r: r["_ts"])
        warnings.append(
            f"{len(refusals)} refusal(s) since the last successful capture "
            f"({', '.join(k[:60] for k in kinds)}; newest "
            f"{newest['_ts'].isoformat()}) — capture is declining, not writing"
        )

    # 3. The last run did capture, but something inside it failed.
    if last:
        errors = last.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            detail = (
                f"{first.get('code')} ({first.get('message')}) for {first.get('url')}"
            )
            warnings.append(f"last capture reported {len(errors)} error(s): {detail}")

    return warnings
