"""Capture health: report only what a single record proves, on its own terms.

Every serious failure this plugin has had was silent — a superseded root wrote
junk for weeks, capture stopped dead for 29 hours — and both were found by an
audit rather than by the plugin noticing.

Four audits then found four defects in the check itself, and they shared one
mechanism: **one record's meaning depended on another record.** A later
successful capture cleared an earlier refusal. An install timestamp scoped away
a stale write. An acknowledgement cursor suppressed by timestamp. Each fix
corrected one instance and the mechanism produced the next.

So there is no cross-record inference here at all. Two rules, and a record is
judged only by its own contents:

- **Operational faults decay.** Refusals and capture errors are reported inside
  a recency window. Timing a PRESENCE is sound: "three refusals in the last day"
  is checkable and ages out by itself. Timing an ABSENCE is what failed twice
  before, and no signal here does it.
- **Integrity incidents do not decay.** A write from a root that was not the
  installed one is reported until a person acknowledges it. Nothing clears it
  automatically, because nothing automatic can know whether the row was
  repaired — and a dead session's bad write is still a bad row.

The reader streams. Materialising the whole log cost 0.9 s and ~294 MB at half a
million records, which would eventually trip the hook's own timeout and leave
the monitor reporting nothing but its own failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable

REFUSAL_EVENTS = (
    "stale-root-refused",
    "forward-unresolved",
    "forward-loop-refused",
    "missing-dependencies",
    # Bootstrap failures. Written as plaintext until 0.17.0, which this parser
    # drops, so a fresh install with no credentials failed on every cited URL
    # and said nothing at all.
    "configuration-error",
    "capture-bootstrap-error",
)

MAX_KINDS_SHOWN = 3


def _parse_ts(raw: Any) -> datetime | None:
    """Log timestamps are ISO-8601 with an offset, written -0400 or -04:00."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _each(lines: Iterable[str]):
    """Yield (record, ts) one at a time. One corrupt line must not blind the rest."""
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
        yield record, ts


def _is_capture(record: dict) -> bool:
    """A run that WROTE. A refusal is not one, however similar the line looks."""
    if record.get("refused"):
        return False
    return "event" not in record and ("urls_seen" in record or "version" in record)


def _version_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1] or root


def _integrity_kind(record: dict, pinned_root: str | None) -> str | None:
    """"stale", "unverified", or None — decided by THIS record alone."""
    root = record.get("root")
    if not isinstance(root, str) or not root:
        return None
    if record.get("pin_observation") == "unknown":
        return "unverified"
    observed = record.get("pinned_root")
    if isinstance(observed, str) and observed:
        return "stale" if root != observed else None
    # No pin evidence at all — a record written before 0.15.0. It proves nothing
    # about staleness, and comparing it to the CURRENT pin is the same unsound
    # inference this module exists to avoid: eighteen correct captures on the
    # live log looked stale purely because the pin moved after they ran. Silence
    # about the pre-field past is the honest answer; the field is why everything
    # after it can be judged on its own.
    return None


def incident_key(record: dict, ts: datetime) -> str:
    """A stable name for one integrity incident, for acknowledgement."""
    return f"{ts.isoformat()}|{record.get('root') or '?'}"


def incident_keys(lines: Iterable[str], *, pinned_root: str | None) -> frozenset[str]:
    """Every acknowledgeable incident in the log."""
    return frozenset(
        incident_key(record, ts)
        for record, ts in _each(lines)
        if _is_capture(record) and _integrity_kind(record, pinned_root)
    )


def evaluate(
    lines: Iterable[str],
    *,
    pinned_root: str | None,
    now: datetime,
    window: timedelta,
    acknowledged: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Human-readable warnings, or an empty list when there is nothing to say."""
    cutoff = now - window
    stale: list[tuple[datetime, str]] = []
    unverified: list[datetime] = []
    refusals: list[tuple[datetime, str]] = []
    errors: list[tuple[datetime, dict]] = []

    for record, ts in _each(lines):
        if record.get("event") in REFUSAL_EVENTS or record.get("refused"):
            if ts >= cutoff:
                refusals.append((ts, str(record.get("event") or record.get("refused"))))
            continue
        if not _is_capture(record):
            continue

        kind = _integrity_kind(record, pinned_root)
        if kind and incident_key(record, ts) not in acknowledged:
            if kind == "stale":
                stale.append((ts, str(record.get("root"))))
            else:
                unverified.append(ts)

        found = record.get("errors")
        if ts >= cutoff and isinstance(found, list) and found:
            for item in found:
                if isinstance(item, dict):
                    errors.append((ts, item))

    warnings: list[str] = []

    if stale:
        newest_ts, newest_root = max(stale)
        roots = sorted({_version_of(root) for _, root in stale})
        warnings.append(
            f"{len(stale)} capture(s) ran from plugin {', '.join(roots)}, which was "
            f"not the installed version at the time (most recent "
            f"{newest_ts.isoformat()}, {newest_root}) — acknowledge with --ack "
            f"once the rows are checked"
        )

    if unverified:
        warnings.append(
            f"{len(unverified)} capture(s) wrote while the installed plugin root "
            f"could not be verified (most recent {max(unverified).isoformat()})"
        )

    if refusals:
        kinds = sorted({kind for _, kind in refusals})[:MAX_KINDS_SHOWN]
        warnings.append(
            f"{len(refusals)} refusal(s) recorded in the last {_hours(window)} "
            f"({', '.join(k[:60] for k in kinds)}; newest "
            f"{max(ts for ts, _ in refusals).isoformat()}) — capture declined to "
            f"write rather than writing"
        )

    if errors:
        newest_ts, first = max(errors, key=lambda pair: pair[0])
        detail = f"{first.get('code')} ({first.get('message')}) for {first.get('url')}"
        warnings.append(
            f"{len(errors)} capture error(s) in the last {_hours(window)}: {detail}"
        )

    return warnings


def _hours(window: timedelta) -> str:
    hours = int(window.total_seconds() // 3600)
    return "hour" if hours == 1 else f"{hours} hours"
