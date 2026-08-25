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
from datetime import datetime
from typing import Any, Iterable

# What the hooks write when they decline to capture. Any of these appearing
# after the last successful capture means capture is off right now.
REFUSAL_EVENTS = (
    "stale-root-refused",
    "forward-unresolved",
    "forward-loop-refused",
    "missing-dependencies",
    # Bootstrap failures. These used to be written as plaintext, which this
    # parser drops, so a fresh install with no credentials failed on every cited
    # URL and reported nothing — forever.
    "configuration-error",
    "capture-bootstrap-error",
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


def _version_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1] or root


def evaluate(
    lines: Iterable[str],
    *,
    pinned_root: str | None,
    installed_at: datetime | None = None,
) -> list[str]:
    """Human-readable warnings, or an empty list when there is nothing to say.

    Four signals, and no clock beyond the log's own. Three earlier designs were
    removed for lying: elapsed-time silence measured the user's habits, hook-fire
    counting chattered after about a hundred citation-free turns, and an
    acknowledgement cursor could not be made race-safe on one-second timestamps.
    Each was machinery added to stop the previous one's noise.

    What replaces the cursor is scope rather than state: a stale write is
    reported while it is still CURRENT — that is, since the running version was
    installed — and stops being reported when the next upgrade supersedes that
    generation. Nothing is stored, so nothing can be lost.
    """
    records = _records(lines)
    if not records:
        return []

    captures = [r for r in records if _is_capture(r)]
    last = max(captures, key=lambda r: r["_ts"]) if captures else None
    warnings: list[str] = []

    def _since_this_install(record: dict) -> bool:
        # Scoping only. `installed_at` is NOT used to decide staleness — records
        # carry the pin they observed and decide that themselves — it decides
        # whether an incident still describes the generation now running.
        return installed_at is None or record["_ts"] > installed_at

    def _was_stale(record: dict) -> bool:
        root = record.get("root")
        if not isinstance(root, str) or not root:
            return False
        observed = record.get("pinned_root")
        if isinstance(observed, str) and observed:
            return root != observed
        # The writer said it could not determine the pin. Unknown is not stale.
        if record.get("pin_observation") == "unknown":
            return False
        if pinned_root is None or root == pinned_root:
            return False
        return _since_this_install(record)

    # 1. A write happened from a root that was not the pinned one at the time.
    stale = [r for r in captures if _was_stale(r) and _since_this_install(r)]
    if stale:
        latest = max(stale, key=lambda r: r["_ts"])
        roots = sorted({_version_of(r["root"]) for r in stale})
        warnings.append(
            f"{len(stale)} capture(s) occurred from plugin {', '.join(roots)}, "
            f"which was not the installed version at the time "
            f"(most recent {latest['_ts'].isoformat()}, {latest['root']})"
        )

    # 2. A write happened while the plugin could not tell which root was pinned.
    #    Not stale — the record disclaims that — but not silence either, because
    #    "we wrote without being able to check" is exactly the uncertainty this
    #    plugin refuses to paper over everywhere else.
    unverified = [
        r
        for r in captures
        if r.get("pin_observation") == "unknown" and _since_this_install(r)
    ]
    if unverified:
        latest = max(unverified, key=lambda r: r["_ts"])
        warnings.append(
            f"{len(unverified)} capture(s) wrote while the installed plugin root "
            f"could not be verified (most recent {latest['_ts'].isoformat()})"
        )

    # 3. Hooks fired and declined, or the plugin could not start. A successful
    #    capture clears these; the count is capped because a long-broken install
    #    produces an absurd number that adds nothing.
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

    # 4. The last run did capture, but something inside it failed.
    if last:
        errors = last.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            detail = (
                f"{first.get('code')} ({first.get('message')}) for {first.get('url')}"
            )
            warnings.append(f"last capture reported {len(errors)} error(s): {detail}")

    return warnings
