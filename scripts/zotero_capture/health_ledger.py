"""The set of OPEN integrity incidents, written before the writes they describe.

Two findings force this module to exist.

**The journal used to begin after the damage.** The incident id was created
inside `_emit_log`, which runs after every Zotero write has already happened. A
hook timeout — and the hooks impose ten and fifteen seconds — could leave a row
written to the library from a superseded root with no record that it happened at
all. The health check would then have nothing to find, which is the exact
failure the health check exists to prevent, one layer beneath where anyone was
looking. Worse, "did this write" was inferred from `urls_new + urls_recurring`,
which are COMPLETION counters incremented well after the POST returns; a commit
followed by an ambiguous failure reported zero writes.

**And suppression could not be bounded.** Replaying an immutable, unbounded log
and filtering out acknowledged incidents requires remembering an unbounded set
of ids — there is no bounded lossless version of that. So the bounded thing is
the set of OPEN incidents. Acknowledgement resolves a row instead of
accumulating a key forever, and a healthy install stores nothing at all, because
a healthy capture never opens an incident.

The asymmetry that justifies writing first: an intent recorded for a mutation
that never happens is a false positive a person can close in one command. A
mutation that happens with no record is corruption nobody can find.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

# A fixed namespace, so a mutation id is a pure function of the capture it
# belongs to and the URL it writes. Never regenerate it: the derivation is the
# reason a replayed write cannot resurrect an acknowledged incident.
MUTATION_NS = uuid.UUID("8b1f2c6a-4d3e-5f70-9a21-6c0d7e4b8f13")


def mutation_id(capture_id: str, url: str | None) -> str:
    """The id of ONE write of ONE url.

    The capture id was used directly as the ledger key, and `incident_id` is
    this table's PRIMARY KEY with ON CONFLICT DO NOTHING. So a message citing
    three sources from a superseded root wrote three rows onto one key: the
    ledger kept the first URL and silently dropped the rest, and the `--ack` it
    offered closed evidence nobody was ever shown. A journal that records one
    entry per BATCH cannot describe a batch that partly succeeded, which is the
    only interesting case.

    Derived rather than random, because DO NOTHING is load-bearing: the same
    write may be retried and the log it came from is replayed at every start.
    A fresh id per call would make that clause dead code and reopen incidents
    that were already resolved.
    """
    return uuid.uuid5(MUTATION_NS, f"{capture_id}\n{url or ''}").hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    opened_at   TEXT NOT NULL,
    url         TEXT,
    root        TEXT NOT NULL,
    pinned_root TEXT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents(status, opened_at);
"""


def _connect(db_path: Path, *, create: bool) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(SCHEMA)
    return conn


def open_incident(
    db_path: Path,
    *,
    incident_id: str,
    url: str | None,
    root: str,
    pinned_root: str | None,
    kind: str,
    ts: str,
) -> None:
    """Record an incident BEFORE the mutation it describes.

    Idempotent, and it will not resurrect an acknowledged incident: the same
    write may be retried, and the log it came from is replayed on every start.
    """
    conn = _connect(db_path, create=True)
    assert conn is not None
    try:
        with conn:
            conn.execute(
                "INSERT INTO incidents"
                " (incident_id, opened_at, url, root, pinned_root, kind, status)"
                " VALUES (?, ?, ?, ?, ?, ?, 'open')"
                " ON CONFLICT(incident_id) DO NOTHING",
                (incident_id, ts, url, root, pinned_root, kind),
            )
    finally:
        conn.close()


def open_incidents(db_path: Path, *, limit: int | None = None) -> list[dict]:
    conn = _connect(db_path, create=False)
    if conn is None:
        return []
    try:
        sql = (
            "SELECT incident_id, opened_at, url, root, pinned_root, kind"
            " FROM incidents WHERE status = 'open' ORDER BY opened_at, incident_id"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def count_open(db_path: Path) -> int:
    conn = _connect(db_path, create=False)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE status = 'open'"
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def acknowledge(db_path: Path, incident_ids: list[str], *, now: str = "") -> list[str]:
    """Resolve the named incidents. Returns exactly those that existed and were open.

    An unknown id changes nothing and is reported as unresolved. The previous
    implementation appended whatever string it was given to a file, printed
    "acknowledged 1 incident(s)", and left the real incident open — a typo that
    looked like success.
    """
    conn = _connect(db_path, create=False)
    if conn is None:
        return []
    resolved: list[str] = []
    try:
        with conn:
            for incident_id in incident_ids:
                cur = conn.execute(
                    "UPDATE incidents SET status = 'acknowledged', closed_at = ?"
                    " WHERE incident_id = ? AND status = 'open'",
                    (now, incident_id),
                )
                if cur.rowcount:
                    resolved.append(incident_id)
    finally:
        conn.close()
    return resolved


def acknowledge_all(db_path: Path, *, now: str = "") -> int:
    conn = _connect(db_path, create=False)
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE incidents SET status = 'acknowledged', closed_at = ?"
                " WHERE status = 'open'",
                (now,),
            )
            return int(cur.rowcount or 0)
    finally:
        conn.close()
