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

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .health_ledger import count_open, open_incidents

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


MAX_FUTURE_SKEW = timedelta(hours=1)


def _each(lines: Iterable[str], stats: dict | None = None):
    """Yield (record, ts) one at a time. One corrupt line must not blind the rest.

    `stats` counts what was seen and what was discarded. A readable log made
    entirely of plaintext parsed to nothing and reported perfect health, which
    is the same blindness as an unreadable one.

    It also records whether the LAST line was newline-terminated. Reading while
    the writer is mid-append is ordinary, and the half-written line it leaves is
    not a fault -- but `strip()` discarded the newline, which is the only thing
    separating that from a line the writer finished and which is still not a
    record. Losing that evidence is why the threshold had to be a run of three,
    and why one or two lines of pure junk warned about nothing.
    """
    for line in lines:
        raw = line if isinstance(line, str) else ""
        text = raw.strip()
        if not text:
            continue
        if stats is not None:
            stats["lines"] = stats.get("lines", 0) + 1
            stats["tail"] = stats.get("tail", 0) + 1
            # Provisional: overwritten by any later line, so what survives
            # describes the final one.
            stats["torn_final"] = 0
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            if stats is not None and not raw.endswith("\n"):
                stats["torn_final"] = 1
            continue
        if not isinstance(record, dict):
            continue
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        if stats is not None:
            stats["records"] = stats.get("records", 0) + 1
            stats["tail"] = 0
        yield record, ts


def _wrote(record: dict) -> bool:
    """Did this run actually touch the library?

    "Capture-shaped" is not "wrote a row". A record that captured nothing became
    a permanent integrity incident needing acknowledgement — a nag about an
    event that never happened. A recurring write counts: re-tagging an existing
    row still touched it.
    """
    try:
        return int(record.get("urls_new") or 0) + int(record.get("urls_recurring") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_capture(record: dict) -> bool:
    """A run that WROTE. A refusal is not one, however similar the line looks."""
    if record.get("refused"):
        return False
    return "event" not in record and ("urls_seen" in record or "version" in record)


def _version_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1] or root


def _integrity_kind(
    record: dict, pinned_root: str | None, *, require_id: bool = True
) -> str | None:
    """"stale", "unverified", or None — decided by THIS record alone.

    Requires an `incident_id`. Without one the incident cannot be acknowledged
    individually, and an unclearable nag is worse than silence — the same
    reasoning that leaves records with no pin evidence unclassified.
    """
    root = record.get("root")
    if not isinstance(root, str) or not root:
        return None
    if require_id and not record.get("incident_id"):
        return None
    if not _wrote(record):
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
    """The writer's own id for this incident.

    Was `timestamp-to-the-second|root`, which aliased distinct incidents: a
    stale write and an unverifiable write from one root in one second produced
    ONE key, so acknowledging either silenced both — permanently, and without
    the second ever being shown.
    """
    return str(record.get("incident_id"))


def _legacy_id(record: dict) -> str:
    """An acknowledgement handle for a record that never issued one itself.

    Was `legacy:<second>|<root>`, which is the aliasing 0.19.0 deleted, walking
    back in through the migration path: two writes from one root inside the same
    second collapsed onto one key, so acknowledging either silenced both --
    permanently, and without the second ever being shown.

    A digest of the record instead. It distinguishes anything the record itself
    distinguishes, and it is STABLE across reads, which a line number or byte
    offset would not be: the id is a handle a person types back, and a rotated
    or truncated log must not rename an incident they were already shown. Two
    byte-identical records in the same second remain one id, which is correct --
    nothing about them differs.
    """
    canonical = json.dumps(record, sort_keys=True, default=str)
    return "legacy:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def incidents(
    lines: Iterable[str], *, pinned_root: str | None, require_id: bool = True
) -> list[dict]:
    """Every acknowledgeable incident, in full, so a person can see what they clear."""
    out: list[dict] = []
    for record, ts in _each(lines):
        if not _is_capture(record):
            continue
        kind = _integrity_kind(record, pinned_root, require_id=require_id)
        if not kind:
            continue
        out.append(
            {
                "id": record.get("incident_id") or _legacy_id(record),
                "url": record.get("url"),
                "ts": ts.isoformat(),
                "kind": kind,
                "root": str(record.get("root")),
                "project": str(record.get("project") or "?"),
            }
        )
    return out


def incident_keys(lines: Iterable[str], *, pinned_root: str | None) -> frozenset[str]:
    return frozenset(item["id"] for item in incidents(lines, pinned_root=pinned_root))


MAX_DISTINCT_KINDS = 16
# One COMPLETE unreadable line is already a fault: the writer finished it and it
# is not a record. The torn final append -- the genuinely ordinary case -- is
# excluded by its missing newline rather than by hiding behind a count, so this
# no longer has to be a run of three to avoid crying wolf.
MIN_BROKEN_TAIL = 1


def evaluate(
    lines: Iterable[str],
    *,
    pinned_root: str | None,
    now: datetime,
    window: timedelta,
    acknowledged: frozenset[str] | set[str] = frozenset(),
    ledger_path: Path | None = None,
) -> list[str]:
    """Human-readable warnings, or an empty list when there is nothing to say.

    Aggregates as it goes rather than collecting matches. Collecting them was
    streaming only while nothing matched: with every record stale, half a
    million of them held 110 MB and took 10.5 s — past the hook's own timeout,
    which would have left the monitor able to report nothing but its own
    failure. The worst case is exactly when the log is longest.
    """
    cutoff = now - window
    horizon = now + MAX_FUTURE_SKEW
    stats: dict = {}
    future_n = 0

    stale_n = 0
    stale_newest: tuple[datetime, str] | None = None
    stale_roots: set[str] = set()

    unverified_n = 0
    unverified_newest: datetime | None = None

    refusal_n = 0
    refusal_newest: datetime | None = None
    refusal_kinds: set[str] = set()

    error_n = 0
    error_newest: tuple[datetime, dict] | None = None

    for record, ts in _each(lines, stats):
        if ts > horizon:
            # A future-dated record sits inside every recency window forever —
            # one dated 2099 would report for seventy-three years — so it is a
            # clock problem rather than a recent operational fault. It only
            # skips the WINDOW signals: integrity lives in the ledger and does
            # not depend on recency, so a bad clock can no longer hide it.
            future_n += 1
            continue
        if record.get("event") in REFUSAL_EVENTS or record.get("refused"):
            if ts >= cutoff:
                refusal_n += 1
                if refusal_newest is None or ts > refusal_newest:
                    refusal_newest = ts
                if len(refusal_kinds) < MAX_DISTINCT_KINDS:
                    refusal_kinds.add(str(record.get("event") or record.get("refused")))
            continue
        if not _is_capture(record):
            continue

        found = record.get("errors")
        if ts >= cutoff and isinstance(found, list) and found:
            for item in found:
                if isinstance(item, dict):
                    error_n += 1
                    if error_newest is None or ts > error_newest[0]:
                        error_newest = (ts, item)

    warnings: list[str] = []

    # A wholly unparseable log, OR a log whose TAIL stopped being parseable.
    # Warning only on the former let one old valid record bless an indefinitely
    # broken telemetry stream — the writer could stop emitting records forever
    # and the monitor stayed silent.
    # The final line, if it was cut off mid-append, is not evidence of anything.
    tail = stats.get("tail", 0) - stats.get("torn_final", 0)
    if stats.get("lines") and not stats.get("records"):
        warnings.append(
            f"the capture log has {stats['lines']} line(s) but no readable "
            f"records — it cannot be assessed, which is not the same as healthy"
        )
    elif tail >= MIN_BROKEN_TAIL:
        warnings.append(
            f"the last {tail} line(s) of the capture log are unreadable — the "
            f"writer may have stopped recording structured events"
        )

    if ledger_path is not None:
        total = count_open(ledger_path)
        if total:
            shown = open_incidents(ledger_path, limit=5)
            ids = ", ".join(i["incident_id"] for i in shown)
            more = f" (+{total - len(shown)} more)" if total > len(shown) else ""
            kinds = ", ".join(sorted({i["kind"] for i in shown}))
            warnings.append(
                f"{total} open integrity incident(s) [{kinds}]: {ids}{more} — "
                f"see --list-incidents, then --ack <id> once the rows are checked"
            )

    if future_n:
        warnings.append(
            f"{future_n} record(s) are dated in the future — a clock is wrong, "
            f"and they would otherwise be treated as recent faults indefinitely"
        )

    if stale_n and stale_newest is not None:
        newest_ts, newest_root = stale_newest
        warnings.append(
            f"{stale_n} capture(s) ran from plugin {', '.join(sorted(stale_roots))}, "
            f"which was not the installed version at the time (most recent "
            f"{newest_ts.isoformat()}, {newest_root}) — see --list-incidents, "
            f"then --ack <id> once the rows are checked"
        )

    if unverified_n and unverified_newest is not None:
        warnings.append(
            f"{unverified_n} capture(s) wrote while the installed plugin root "
            f"could not be verified (most recent {unverified_newest.isoformat()})"
        )

    if refusal_n and refusal_newest is not None:
        kinds = sorted(refusal_kinds)[:MAX_KINDS_SHOWN]
        warnings.append(
            f"{refusal_n} refusal(s) recorded in the last {_hours(window)} "
            f"({', '.join(k[:60] for k in kinds)}; newest "
            f"{refusal_newest.isoformat()}) — capture declined to write rather "
            f"than writing"
        )

    if error_n and error_newest is not None:
        first = error_newest[1]
        detail = f"{first.get('code')} ({first.get('message')}) for {first.get('url')}"
        warnings.append(
            f"{error_n} capture error(s) in the last {_hours(window)}: {detail}"
        )

    return warnings


def _hours(window: timedelta) -> str:
    hours = int(window.total_seconds() // 3600)
    return "hour" if hours == 1 else f"{hours} hours"
