"""SQLite-backed URL cache for O(1) dedup against Zotero `web-sources`."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_index (
    url_canonical TEXT PRIMARY KEY,
    zotero_key    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_identity (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    api_origin     TEXT NOT NULL,
    library_type   TEXT NOT NULL,
    library_id     TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    bound_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_tags (
    url_canonical TEXT NOT NULL,
    tag           TEXT NOT NULL,
    PRIMARY KEY (url_canonical, tag)
);
CREATE TABLE IF NOT EXISTS claim_link (
    url_canonical TEXT NOT NULL,
    claim         TEXT NOT NULL,
    project       TEXT NOT NULL,
    context       TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (url_canonical, claim)
);
CREATE TABLE IF NOT EXISTS retry_queue (
    url_canonical TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    context       TEXT,
    seen_date     TEXT NOT NULL,
    first_failed  TEXT NOT NULL,
    last_failed   TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 1,
    last_error    TEXT NOT NULL
);
"""

# What the queue stores is deliberately NOT the write. It stores the inputs a
# capture needs -- url, project, context, and the date the URL was actually
# seen -- so a drain can replay the URL through `capture_message` itself rather
# than re-issuing a POST of its own. A failed POST may or may not have
# committed, and re-posting blind is how duplicates are made; replaying through
# the real path inherits the reservation, the claim resolution and the dedup
# that already exist to answer exactly that question.
#
# `seen_date` is stored because it is the provenance. Replaying with today's
# date would file the source under the day the retry ran rather than the day it
# was cited, which is the one fact the library exists to record.

# A queue nothing drains grows forever, which is why there was no queue at all
# before this. Both bounds exist so that it cannot: a full queue refuses new
# entries loudly rather than evicting silently, and an entry that keeps failing
# is given up on instead of being retried until the end of time.
RETRY_QUEUE_MAX = 500
RETRY_MAX_ATTEMPTS = 5

# Added after the original table shipped, so they arrive by migration rather than
# in SCHEMA: the deployed index already holds thousands of rows.
MIGRATIONS = (
    "ALTER TABLE url_index ADD COLUMN pending_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE url_index ADD COLUMN claimed_at TEXT NOT NULL DEFAULT ''",
    # What the page said when it was read, and when that was. A captured item is
    # a URL and a title; if the page changes or dies, nothing in the library can
    # show what was actually consulted. The hash does not preserve the content --
    # it makes a change DETECTABLE, which is the difference between a citation
    # you can defend and one you can only hope about.
    "ALTER TABLE url_index ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE url_index ADD COLUMN hashed_at TEXT NOT NULL DEFAULT ''",
    # WHY a page could not be read, and when we last tried. Absence of a hash
    # used to mean both "never attempted" and "attempted and failed", so every
    # pass re-fetched the same dead rows forever and `--limit` never got past
    # them. It also threw away the distinction that matters most: a 404 is a
    # finding about the source, a 403 is a fact about us.
    "ALTER TABLE url_index ADD COLUMN last_outcome TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE url_index ADD COLUMN last_attempt_at TEXT NOT NULL DEFAULT ''",
    # WHICH URL produced that outcome. Not the same question as which one we
    # asked for: 177 rows in the live index recorded `blocked` against doi.org,
    # and doi.org had answered every one of them correctly with a 302. The 403
    # came from academic.oup.com, one hop later, and its name appeared nowhere
    # in the row -- so the index blamed a resolver for a refusal it did not make
    # and hid the party that did.
    #
    # Empty means NEVER RECORDED, not "same as the requested URL". Those are
    # different facts and the ~4,900 rows written before this column existed can
    # only honestly claim the first. Reading '' as "no redirect happened" would
    # make every legacy row assert something nobody ever checked, which is the
    # same manufacturing-a-finding mistake as guessing `gone` from an
    # unrecognised error.
    "ALTER TABLE url_index ADD COLUMN final_url TEXT NOT NULL DEFAULT ''",
    # WHAT the stored digest covers. Without these two columns `content_hash`
    # could only ever mean "the whole document", so a page too big to read in
    # full had to be recorded as nothing at all -- and the corpus's 71 largest
    # sources, every one fetched successfully, held no evidence whatever.
    #
    # The DEFAULTS are the migration, and they are chosen to describe what
    # actually happened rather than to be convenient. Every row hashed before
    # this existed was hashed under the all-or-nothing rule, so it IS complete:
    # `hash_truncated = 0` is a fact about those ~3,744 rows, not an assumption.
    # Their LENGTH, though, was never recorded, and -1 says exactly that. A
    # default of 0 would have claimed a zero-byte document and any other number
    # would have invented one -- the same manufacturing-a-finding mistake as
    # reading final_url '' as "no redirect happened".
    "ALTER TABLE url_index ADD COLUMN hash_bytes INTEGER NOT NULL DEFAULT -1",
    "ALTER TABLE url_index ADD COLUMN hash_truncated INTEGER NOT NULL DEFAULT 0",
)

# The alphabet the Zotero API accepts for an object key: base32 without the
# characters that read ambiguously (0/O, 1/I). Keys are 8 of these.
ZOTERO_KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
ZOTERO_KEY_RE = re.compile(rf"[{ZOTERO_KEY_ALPHABET}]{{8}}")


def new_zotero_key() -> str:
    """A key this client picks itself, so a lost response is still answerable.

    Choosing the key before the POST is what turns "did my item get created?"
    from unanswerable into a single GET. secrets rather than random because the
    key must not collide with another session's concurrent choice.
    """
    return "".join(secrets.choice(ZOTERO_KEY_ALPHABET) for _ in range(8))


class URLCacheRow(TypedDict):
    url_canonical: str
    zotero_key: str
    first_seen: str
    last_seen: str
    pending_key: str
    claimed_at: str


# Which index files this PROCESS has already brought up to date. Per process,
# not per connection: the schema cannot go stale underneath a running tool, and
# a hook that opens the index a dozen times pays for it once.
_SCHEMA_READY: set[str] = set()


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for statement in MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as e:
            # Already applied. Anything else is a real problem and re-raises.
            if "duplicate column name" not in str(e):
                raise


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the index, guaranteeing it matches the schema THIS code expects.

    The schema used to be brought current only by whoever remembered to call
    `init_db`, and 2 of the 8 maintenance CLIs did. So a column added FOR a
    maintenance tool was missing in exactly that tool: `snapshot_pages` died on
    the live index with `no such column: last_outcome`, and would have died the
    same way on `content_hash` since the release that added it. No test could
    see it, because every fixture calls `init_db` first.

    Writing that call into the other six scripts would be one rule kept in eight
    places, and the ninth tool would omit it. Ensuring it HERE removes the step
    that can be skipped: there is no way to open this index and see a stale one.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Multiple Claude sessions capture concurrently into one index; wait on the
    # write lock instead of raising "database is locked" immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
    key = str(db_path)
    if key not in _SCHEMA_READY:
        _apply_schema(conn)
        _SCHEMA_READY.add(key)
    return conn


def init_db(db_path: Path) -> None:
    """Create the index and apply every migration. Idempotent.

    Kept as the explicit, named way to do this even though `_connect` now
    guarantees it: callers that say what they mean are worth more than the one
    redundant pass they cost.
    """
    with closing(_connect(db_path)) as conn:
        _apply_schema(conn)


IDENTITY_FIELDS = ("api_origin", "library_type", "library_id", "collection_key")


class IndexIdentityMismatch(RuntimeError):
    """This index is an index of a DIFFERENT library or collection."""


def read_identity(db_path: Path) -> dict[str, str] | None:
    """What library this index describes, or None if it has never been told."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT api_origin, library_type, library_id, collection_key"
            " FROM index_identity WHERE id = 1"
        ).fetchone()
    return {k: row[k] for k in IDENTITY_FIELDS} if row else None


def require_identity(
    db_path: Path,
    *,
    api_origin: str,
    library_type: str,
    library_id: str,
    collection_key: str,
) -> None:
    """Verify, never adopt. For tools that DESTROY rather than accumulate.

    `bind_identity` adopts an index that has never been told what it is, because
    the deployed index holds thousands of rows written before identity existed
    and refusing them would break the working case to guard a hypothetical one.
    That trade is right for capture, whose worst case is a row in the wrong
    collection.

    It is wrong for a tool that trashes items. Adopting there means the first
    thing an index nobody can vouch for does is have rows destroyed out of it,
    on the strength of an assumption made one line earlier. Zotero item keys are
    library-wide, so a client aimed at collection B still finds and trashes
    collection A's item -- the mismatch does not announce itself by failing.

    So: a populated index must already carry an identity, and it must match.
    An EMPTY index is allowed through -- there is nothing to protect and nothing
    to destroy -- and a person can bind a legacy index by letting capture run
    once against the library those rows actually came from.
    """
    incoming = {
        "api_origin": api_origin.rstrip("/"),
        "library_type": library_type,
        "library_id": library_id,
        "collection_key": collection_key,
    }
    current = read_identity(db_path)
    if current == incoming:
        return
    with closing(_connect(db_path)) as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM url_index").fetchone()["n"]
    if not rows:
        return
    if current is None:
        raise IndexIdentityMismatch(
            f"{db_path} holds {rows} row(s) but has never recorded which library "
            f"they belong to, and this command destroys rows. Run a capture "
            f"against the library those rows came from to bind it first."
        )
    differing = [k for k in IDENTITY_FIELDS if current[k] != incoming[k]]
    raise IndexIdentityMismatch(
        f"{db_path} indexes {current} but this command is configured for "
        f"{incoming} (differing: {', '.join(differing)})"
    )


def bind_identity(
    db_path: Path,
    *,
    api_origin: str,
    library_type: str,
    library_id: str,
    collection_key: str,
) -> None:
    """Record which library this index is OF, or refuse if it is another's.

    The index is a cache of one Zotero collection, keyed by URL alone, and it
    stored nothing about what its rows referred to. Three ordinary changes were
    therefore silent corruption:

      * a different COLLECTION — new URLs land in the new one, but a recurring
        URL is only TAGGED, and tagging does not move an item. The sources split
        in half and both halves report success.
      * a different LIBRARY — every stored key belongs to the old one, so each
        recurrence 404s, and the row blocks that URL from ever being recreated.
      * two state dirs against one library — two indexes, each certain a URL is
        new, producing duplicate items neither can see.

    An index with no identity ADOPTS the one it is opened with rather than
    refusing: the deployed index holds thousands of rows written before this
    existed, and refusing them would break the working case to guard a
    hypothetical one. That is a real assumption — it trusts the upgrader is
    still pointing at the library those rows came from — and it is why the
    binding is written on first open rather than inferred later.

    An EMPTY index rebinds freely. With no rows there is nothing to protect,
    and refusing would make a state dir unusable after a single mistaken run.
    """
    incoming = {
        "api_origin": api_origin.rstrip("/"),
        "library_type": library_type,
        "library_id": library_id,
        "collection_key": collection_key,
    }
    current = read_identity(db_path)
    if current is not None and current != incoming:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM url_index").fetchone()["n"]
        if rows:
            differing = [k for k in IDENTITY_FIELDS if current[k] != incoming[k]]
            raise IndexIdentityMismatch(
                f"{db_path} indexes {current} but capture is configured for "
                f"{incoming} (differs on: {', '.join(differing)}). Its "
                f"{rows} rows describe the other target, so reusing it would "
                f"split sources or strand every key. Point "
                f"ZOTERO_CAPTURE_STATE_DIR at a separate directory for this "
                f"target, or delete this index to rebuild it."
            )
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO index_identity"
            " (id, api_origin, library_type, library_id, collection_key, bound_at)"
            " VALUES (1, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " api_origin=excluded.api_origin, library_type=excluded.library_type,"
            " library_id=excluded.library_id, collection_key=excluded.collection_key,"
            " bound_at=excluded.bound_at",
            (*[incoming[k] for k in IDENTITY_FIELDS], stamp),
        )


def lookup_url(db_path: Path, url_canonical: str) -> URLCacheRow | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT url_canonical, zotero_key, first_seen, last_seen, pending_key,"
            " claimed_at FROM url_index WHERE url_canonical = ?",
            (url_canonical,),
        ).fetchone()
    return cast(URLCacheRow, dict(row)) if row else None


def insert_url(
    db_path: Path, url_canonical: str, zotero_key: str, first_seen: date
) -> None:
    iso = first_seen.isoformat()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO url_index (url_canonical, zotero_key, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (url_canonical, zotero_key, iso, iso),
        )


def reserve_url(
    db_path: Path,
    url_canonical: str,
    first_seen: date,
    *,
    pending_key: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Claim a URL before creating its Zotero item. True if this caller won.

    `lookup -> POST -> insert` is not atomic: two sessions could both miss the
    lookup, both POST, and then `INSERT OR IGNORE` would keep one key and drop
    the other — leaving a real Zotero item with nothing in the index pointing at
    it, invisible to dedup forever. The detached prompt hook makes overlapping
    captures ordinary, so this is a race that actually runs.

    The row is written first with an empty key, which the PRIMARY KEY makes
    atomic across processes. `set_zotero_key` fills it in once the item exists;
    `release_url` undoes the claim if the POST was never issued.

    The claim also records the key the caller intends to create and the moment it
    was taken. Without those, an abandoned claim is ambiguous — it may or may not
    already have an item behind it, so neither completing it nor releasing it is
    safe. With them, the question is one GET.
    """
    iso = first_seen.isoformat()
    key = pending_key if pending_key is not None else new_zotero_key()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO url_index"
            " (url_canonical, zotero_key, first_seen, last_seen, pending_key, claimed_at)"
            " VALUES (?, '', ?, ?, ?, ?)",
            (url_canonical, iso, iso, key, stamp),
        )
    return cursor.rowcount == 1


def queue_pending_tags(db_path: Path, url_canonical: str, tags: list[str]) -> None:
    """Remember tags that could not be applied yet, so the sighting is not lost.

    A session that loses the race has real provenance to record — its own
    context and project — but no item to put it on, because the winner's POST is
    still in flight. Dropping it silently lost that occurrence for good if the
    URL was never cited again.
    """
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO pending_tags (url_canonical, tag) VALUES (?, ?)",
            [(url_canonical, tag) for tag in tags],
        )


def peek_pending_tags(db_path: Path, url_canonical: str) -> list[str]:
    """The tags queued for a URL, WITHOUT consuming them."""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT tag FROM pending_tags WHERE url_canonical = ? ORDER BY tag",
            (url_canonical,),
        ).fetchall()
    return [r["tag"] for r in rows]


def clear_pending_tags(db_path: Path, url_canonical: str, tags: list[str]) -> None:
    """Drop exactly the tags that were successfully applied.

    Named tags rather than "everything for this URL": between the peek and the
    write, another session may have queued a sighting of its own, and a blanket
    DELETE would discard a tag that was never applied to anything.
    """
    if not tags:
        return
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            "DELETE FROM pending_tags WHERE url_canonical = ? AND tag = ?",
            [(url_canonical, tag) for tag in tags],
        )


def take_pending_tags(db_path: Path, url_canonical: str) -> list[str]:
    """Remove and return the tags queued for a URL. Empty if there were none.

    Destructive, so it must NOT be used to feed a write that can fail: the
    DELETE commits on this autocommit connection before the caller's request is
    issued, and a Zotero error then loses the sighting for good. Capture uses
    peek + clear for that reason. Kept for callers that only need to drain.
    """
    tags = peek_pending_tags(db_path, url_canonical)
    clear_pending_tags(db_path, url_canonical, tags)
    return tags


def set_zotero_key(
    db_path: Path,
    url_canonical: str,
    zotero_key: str,
    *,
    pending_key: str | None = None,
) -> bool:
    """Complete a reservation once the Zotero item exists. True if it took.

    Pass `pending_key` to make this a compare-and-swap. Matching on the URL
    alone is not enough once a claim can be reaped: A claims and stalls, B
    judges A abandoned and claims the URL with its own key, then A wakes and
    stamps ITS key over B's completed row. The index then points at an item
    that may not exist while B's real item is invisible to dedup forever.
    """
    sql = "UPDATE url_index SET zotero_key = ? WHERE url_canonical = ?"
    params: tuple[str, ...] = (zotero_key, url_canonical)
    if pending_key is not None:
        sql += " AND pending_key = ?"
        params += (pending_key,)
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(sql, params)
    return cursor.rowcount == 1


def release_url(
    db_path: Path, url_canonical: str, *, pending_key: str | None = None
) -> bool:
    """Drop an unfulfilled reservation so a later run can retry. True if it did.

    Only removes a row that never got a key. A completed row belongs to a real
    Zotero item, and deleting its index entry would strand that item exactly the
    way the un-reserved race did.

    Pass `pending_key` to release only YOUR OWN claim. Without it a stalled
    owner, waking after its claim was reaped and re-taken, deletes the
    successor's live reservation — and the successor's POST becomes an orphan
    item that dedup can never see again. That is the very outcome the
    reservation protocol exists to prevent, reintroduced by the reaper.
    """
    sql = "DELETE FROM url_index WHERE url_canonical = ? AND zotero_key = ''"
    params: tuple[str, ...] = (url_canonical,)
    if pending_key is not None:
        sql += " AND pending_key = ?"
        params += (pending_key,)
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(sql, params)
    return cursor.rowcount == 1


def drop_row(db_path: Path, url_canonical: str) -> None:
    """Forget a URL entirely, index row and queued tags together.

    For the case where the Zotero item behind a row no longer exists. Unlike
    release_url this does not care whether the row completed: a completed row
    whose item a person deleted is exactly the row that has to go.
    """
    with closing(_connect(db_path)) as conn:
        conn.execute("DELETE FROM url_index WHERE url_canonical = ?", (url_canonical,))
        conn.execute(
            "DELETE FROM pending_tags WHERE url_canonical = ?", (url_canonical,)
        )


def update_last_seen(db_path: Path, url_canonical: str, seen: date) -> None:
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE url_index SET last_seen = ? WHERE url_canonical = ?",
            (seen.isoformat(), url_canonical),
        )
    if cursor.rowcount == 0:
        raise KeyError(f"url_canonical not found in cache: {url_canonical!r}")


class RetryEntry(TypedDict):
    url_canonical: str
    project: str
    context: str | None
    seen_date: str
    first_failed: str
    last_failed: str
    attempts: int
    last_error: str


def enqueue_retry(
    db_path: Path,
    *,
    url_canonical: str,
    project: str,
    context: str | None,
    seen_date: str,
    error: str,
    now: str,
) -> bool:
    """Remember a write that failed, so recovery does not need a re-citation.

    Returns False when the queue is FULL and the entry was refused. The caller
    must surface that: silently dropping the overflow would rebuild, one level
    up, exactly the "logged and dropped" behaviour this replaces.

    Re-queueing a URL already in the queue updates it in place and counts an
    attempt rather than adding a second row, so a URL failing every day cannot
    crowd out everything else.
    """
    with closing(_connect(db_path)) as conn:
        existing = conn.execute(
            "SELECT attempts FROM retry_queue WHERE url_canonical = ?",
            (url_canonical,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE retry_queue SET attempts = attempts + 1, last_failed = ?,"
                " last_error = ? WHERE url_canonical = ?",
                (now, error[:500], url_canonical),
            )
            return True
        depth = conn.execute("SELECT COUNT(*) AS n FROM retry_queue").fetchone()["n"]
        if depth >= RETRY_QUEUE_MAX:
            return False
        conn.execute(
            "INSERT INTO retry_queue (url_canonical, project, context, seen_date,"
            " first_failed, last_failed, attempts, last_error)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (url_canonical, project, context, seen_date, now, now, error[:500]),
        )
        return True


def retry_queue_depth(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with closing(_connect(db_path)) as conn:
        try:
            return int(
                conn.execute("SELECT COUNT(*) AS n FROM retry_queue").fetchone()["n"]
            )
        except sqlite3.OperationalError:
            # An index predating the table. Not an error: nothing is queued.
            return 0


def retry_queue_entries(db_path: Path, *, limit: int | None = None) -> list[RetryEntry]:
    """Oldest failure first, so a drain works through the backlog in order."""
    sql = (
        "SELECT url_canonical, project, context, seen_date, first_failed,"
        " last_failed, attempts, last_error FROM retry_queue"
        " ORDER BY first_failed, url_canonical"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with closing(_connect(db_path)) as conn:
        return [cast(RetryEntry, dict(row)) for row in conn.execute(sql)]


def dequeue_retry(db_path: Path, url_canonical: str) -> bool:
    """Drop an entry. True if it was there, so a caller can tell a no-op apart."""
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "DELETE FROM retry_queue WHERE url_canonical = ?", (url_canonical,)
        )
    return cursor.rowcount == 1


def set_content_hash(
    db_path: Path,
    url_canonical: str,
    *,
    content_hash: str,
    hashed_at: str,
    covers_bytes: int,
    complete: bool,
) -> bool:
    """Record what the page said, when it was read, and HOW MUCH of it was read.

    `covers_bytes` and `complete` are required rather than defaulted, for the
    same reason `final_url` is. Their only sensible default would be "the whole
    document", so a caller that simply forgot would silently promote a prefix
    into a whole-document claim -- reintroducing the exact false negative that
    the old refuse-to-record rule existed to prevent, but now invisibly, in a
    stored row that reads as authoritative. This project has already shipped one
    omittable parameter that was therefore omitted for a whole release.

    True if the row existed.
    """
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE url_index SET content_hash = ?, hashed_at = ?,"
            " hash_bytes = ?, hash_truncated = ?"
            " WHERE url_canonical = ?",
            (
                content_hash,
                hashed_at,
                int(covers_bytes),
                0 if complete else 1,
                url_canonical,
            ),
        )
    return cursor.rowcount == 1


def set_fetch_outcome(
    db_path: Path, url_canonical: str, *, outcome: str, at: str, final_url: str
) -> bool:
    """Record WHY the last read of this page ended as it did, and WHERE.

    Separate from `hashed_at`, which stays empty unless a hash was actually
    stored: a read time is evidence the page WAS read, while an attempt time is
    evidence only that we tried. Conflating them is what made a blocked page and
    a dead page indistinguishable.

    `final_url` is required rather than defaulted, and that is deliberate. Its
    default would be '' -- the value meaning "we never found out" -- so a caller
    that simply forgot it would write a confident "unknown" over a fact it was
    holding at the time. This codebase has already shipped one parameter that
    could be omitted and therefore was (`--sleep`, parsed for a whole release
    and never passed), and the omission was invisible from either end. Making it
    required moves that failure from runtime silence to an immediate TypeError.
    """
    with closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE url_index SET last_outcome = ?, last_attempt_at = ?,"
            " final_url = ? WHERE url_canonical = ?",
            (outcome, at, final_url, url_canonical),
        )
    return cursor.rowcount == 1


def row_for_url(db_path: Path, url_canonical: str) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM url_index WHERE url_canonical = ?", (url_canonical,)
        ).fetchone()
    return dict(row) if row else None


def rows_needing_hash(
    db_path: Path,
    *,
    limit: int | None = None,
    include_failed: bool = False,
    only_outcome: str | None = None,
    only_host: str | None = None,
) -> list[dict]:
    """Completed rows that have never been hashed, oldest sighting first.

    An in-flight row (`zotero_key = ''`) is excluded: it has no item to write the
    hash onto, and the capture that owns it may still be mid-POST.

    A row whose last attempt FAILED is excluded too, unless `include_failed`.
    Without that, absence of a hash means both "never tried" and "tried and
    failed", so a corpus with 1,285 unreadable rows re-fetches every one of them
    on every pass and `--limit N` never advances past the first N dead links.

    `ok` is deliberately not a failure: a fetch that succeeded but whose Zotero
    stamp was refused has no hash yet and SHOULD be retried.

    `only_outcome` narrows to rows whose last read ended a particular way, so a
    change to how ONE outcome is decided can be re-run over just those rows.
    Without it, correcting 306 misjudged rows meant re-fetching all 1,400 --
    which would have asked academic.oup.com for 159 pages it had refused an hour
    earlier, purely as collateral. Politeness is a reason for a feature, not only
    a delay.

    `only_host` narrows the same way on the other axis: to one host and its
    subdomains. It matches on a BOUNDARY, never a substring -- "wikipedia.org"
    takes en.wikipedia.org and wikipedia.org, and refuses notwikipedia.org and
    wikipedia.org.evil.test. A substring test where a token was meant is a
    mistake this project has now made three times (URL_RE as a character
    blacklist, and an alert filter that read "OOM" out of Bloomberg), so the
    boundary case is the one the tests are actually about.
    """
    sql = (
        "SELECT url_canonical, zotero_key, first_seen FROM url_index"
        " WHERE content_hash = '' AND zotero_key != ''"
    )
    params: list[str] = []
    if only_outcome is not None:
        # Parameterised, not interpolated: this reaches the CLI surface.
        sql += " AND last_outcome = ?"
        params.append(only_outcome)
    elif not include_failed:
        sql += " AND last_outcome IN ('', 'ok')"
    if only_host is not None:
        host = only_host.strip().lower().lstrip(".")
        if not host:
            raise ValueError("only_host must name a host")
        # LIKE metacharacters in the host are escaped, not trusted. This reaches
        # the CLI, and an unescaped "%" would silently widen the filter to every
        # row -- a scoped re-run quietly becoming a full one is the worst way for
        # this to fail, because the report would still say it did what was asked.
        escaped = host.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Four patterns, anchored at BOTH ends. The scheme anchors the left; the
        # trailing "/" anchors the right, so "wikipedia.org" cannot take
        # "wikipedia.org.evil.test". The "%." arm is what admits subdomains, and
        # its dot is what keeps "notwikipedia.org" out.
        sql += (
            " AND (url_canonical LIKE ? ESCAPE '\\'"
            " OR url_canonical LIKE ? ESCAPE '\\'"
            " OR url_canonical LIKE ? ESCAPE '\\'"
            " OR url_canonical LIKE ? ESCAPE '\\')"
        )
        params += [
            f"http://{escaped}/%",
            f"https://{escaped}/%",
            f"http://%.{escaped}/%",
            f"https://%.{escaped}/%",
        ]
    sql += " ORDER BY first_seen, url_canonical"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with closing(_connect(db_path)) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def rows_with_hash(db_path: Path, *, limit: int | None = None) -> list[dict]:
    """Rows that carry a hash, so a verify pass can ask whether it still holds."""
    sql = (
        "SELECT url_canonical, zotero_key, content_hash, hashed_at,"
        " hash_bytes, hash_truncated FROM url_index"
        " WHERE content_hash != '' ORDER BY hashed_at, url_canonical"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with closing(_connect(db_path)) as conn:
        return [dict(row) for row in conn.execute(sql)]
