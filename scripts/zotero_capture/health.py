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


def _version_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1] or root


def evaluate(
    lines: Iterable[str],
    *,
    pinned_root: str | None,
    now: datetime,
    installed_at: datetime | None = None,
    fires: Iterable[str] = (),
    max_fires: int = 200,
) -> list[str]:
    """Human-readable warnings, or an empty list when there is nothing to say."""
    records = _records(lines)
    if not records:
        return []

    captures = [r for r in records if _is_capture(r)]
    last = max(captures, key=lambda r: r["_ts"]) if captures else None
    warnings: list[str] = []

    # 1. Executing code that is not the installed code. This is the shape of both
    #    the junk-writing root and the 29-hour outage, and it is the one signal
    #    that identifies them on the first session after they begin.
    #
    #    `installed_at` is what keeps this from crying wolf on every release. For
    #    a few minutes after an upgrade the newest capture legitimately came from
    #    the PREVIOUS root, because it happened before the new one was pinned —
    #    which is not stale code, just a clock ordering. The real question is
    #    whether anything has captured from an unpinned root SINCE the upgrade.
    #    Caught by running the check against the live log right after shipping
    #    0.14.0, where it fired on a capture 30 seconds too old to be a fault.
    #    EVERY capture since the upgrade is examined, not just the newest. Taking
    #    only the newest let one good write erase the evidence of a bad one: two
    #    sessions run concurrently, the lingering stale one captures at 11:40 and
    #    the current one at 11:41, and the stale write became invisible for good
    #    while that session was still alive and still writing. That made the
    #    check decorative against the precise failure it exists to find.
    #    A record that carries `pinned_root` decides its own staleness: it says
    #    what the registry pinned at the moment it wrote, so an upgrade cannot
    #    retroactively forgive a write that was ALREADY stale when it happened.
    #    `installed_at` remains the fallback for lines written before 0.15.0.
    def _was_stale(record: dict) -> bool:
        root = record.get("root")
        if not isinstance(root, str) or not root:
            return False
        observed = record.get("pinned_root")
        if isinstance(observed, str) and observed:
            return root != observed
        if root == pinned_root:
            return False
        return not (installed_at is not None and record["_ts"] <= installed_at)

    if pinned_root:
        stale = [r for r in captures if _was_stale(r)]
        if stale:
            roots = sorted({_version_of(r["root"]) for r in stale})
            latest = max(stale, key=lambda r: r["_ts"])
            warnings.append(
                f"{len(stale)} capture(s) since the current version was installed "
                f"ran from plugin {', '.join(roots)}, but {_version_of(pinned_root)} "
                f"is installed — a live session is executing superseded code "
                f"(most recent {latest['_ts'].isoformat()}, {latest['root']})"
            )

    # 2. Hooks are firing and declining. Only refusals AFTER the last successful
    #    capture count; older ones describe a problem that has since resolved.
    since = last["_ts"] if last else None
    refusals = [
        r
        for r in records
        if (r.get("event") in REFUSAL_EVENTS or r.get("refused"))
        and (since is None or r["_ts"] > since)
    ]
    if refusals:
        kinds = sorted(
            {str(r.get("event") or r.get("refused")) for r in refusals}
        )
        warnings.append(
            f"{len(refusals)} refusal event(s) since the last successful capture: "
            f"{', '.join(kinds)} — capture is declining rather than writing"
        )

    # 3. The hooks are running and nothing is being captured.
    #
    #    This replaces a wall-clock threshold, which could not tell a broken
    #    plugin from a quiet weekend: a Friday capture and a Monday session is a
    #    60-70 hour gap with nothing whatsoever wrong, and SessionStart fires on
    #    resume/clear/compact/fork as well as startup, so the same false alarm
    #    repeated. Elapsed time measures the user's habits, not the plugin's
    #    health.
    #
    #    Hook fires do measure the plugin. Many fires with no successful capture
    #    between them means the entry point is running and producing nothing,
    #    which is a fault; few fires means nobody was working, which is not.
    since_fires = last["_ts"] if last else None
    recent = [
        t
        for t in (_parse_ts(raw) for raw in fires)
        if t is not None and (since_fires is None or t > since_fires)
    ]
    if len(recent) > max_fires:
        tail = (
            f" (last capture {last['_ts'].isoformat()})"
            if last
            else " and nothing has ever been captured"
        )
        warnings.append(
            f"the hooks have run {len(recent)} times with no successful capture"
            f"{tail} — they are firing but writing nothing"
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
