"""What the destructive tools must REFUSE.

These two tools trash items in a person's library. The index is the only record
that a citation was ever made, so a predicate that over-fires here destroys the
evidence of its own mistake. Both defects below were found by the first external
review these modules ever had.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import repair_urls
import retire_rows
from zotero_capture.retire import apply_retire, plan_retire
from zotero_capture.retire import HARD, POLICY, classify
from zotero_capture.sqlite_cache import init_db

JUNK = [
    "https://files.rcsb.org/download/{ID}.pdb",
    "https://atted.jp/api/coex/Ath-u/{locus}/{top_n",
    "https://ftp.ebi.ac.uk/pub/${VERSION}/interproscan.tar.gz",
]


@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setenv("ZOTERO_API_KEY", "fake")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "0")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "user")
    monkeypatch.setenv("ZOTERO_WEBSOURCES_COLLECTION_KEY", "FAKE0000")


def _index(tmp_path: Path) -> Path:
    db = tmp_path / "idx.db"
    init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        for i, url in enumerate(JUNK):
            conn.execute(
                "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
                " VALUES (?, ?, '2026-01-01', '2026-01-01')",
                (url, f"KEY{i}"),
            )
        conn.commit()
    return db


def _planned(capsys) -> int:
    """How many steps the tool says it will carry out."""
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("to retire:"):
            return int(line.split()[2])
    raise AssertionError("the tool printed no plan")


def test_limit_zero_retires_nothing(tmp_path: Path, capsys, _creds) -> None:
    """`--limit 0` used to skip the slice entirely and retire the WHOLE plan.

    `if args.limit:` is false for 0, so the guard meant to cap a destructive
    bulk run inverted into "no cap" at exactly the value a person reaches for
    when they want to be careful.
    """
    db = _index(tmp_path)

    assert retire_rows.main(["--db-path", str(db)]) == 0
    assert _planned(capsys) == len(JUNK), "positive control: the plan should be full"

    assert retire_rows.main(["--db-path", str(db), "--limit", "2"]) == 0
    assert _planned(capsys) == 2, "positive control: a real limit should cap"

    assert retire_rows.main(["--db-path", str(db), "--limit", "0"]) == 0
    assert _planned(capsys) == 0


def test_a_negative_limit_is_refused_rather_than_reversed(
    tmp_path: Path, _creds
) -> None:
    """`steps[:-1]` is every step but the last -- a cap that removes one item."""
    db = _index(tmp_path)
    with pytest.raises(SystemExit):
        retire_rows.main(["--db-path", str(db), "--limit", "-1"])
    with pytest.raises(SystemExit):
        repair_urls.main(["--db-path", str(db), "--limit", "-1"])


def test_repair_limit_zero_repairs_nothing(tmp_path: Path, capsys, _creds) -> None:
    db = _index(tmp_path)
    repair_urls.main(["--db-path", str(db), "--limit", "0"])
    out = capsys.readouterr().out
    assert " rewrite  0" in out.replace("  rewrite", " rewrite"), out


@pytest.mark.parametrize(
    "url",
    [
        "https://printer.local/manual",
        "http://expyuzz4wqqyqhjn.onion/about",
    ],
)
def test_a_resolvable_special_use_name_is_policy_not_proof(url: str) -> None:
    """HARD means "proof this cannot be an address at all".

    `.local` resolves over mDNS and `.onion` resolves through Tor, so both ARE
    addresses -- for whoever is on that network. Filing them as proof meant a
    bare `--apply`, with no `--policy` flag, reached back and trashed them.
    """
    tier, _reason = classify(url)
    assert tier == POLICY, f"{url} was classified {tier!r}, so --apply would trash it"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/paper.pdf",
        "https://files.rcsb.org/download/{ID}.pdb",
    ],
)
def test_genuine_proof_is_still_hard(url: str) -> None:
    """Positive control: narrowing HARD must not empty it."""
    assert classify(url)[0] == HARD


# --- the reservation race, and finalizing by URL alone -----------------------


def _connect(db: Path):
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


class _FakeZotero:
    def __init__(self) -> None:
        self.trashed: list[str] = []

    def trash_item(self, key: str) -> None:
        self.trashed.append(key)


JUNK_URL = "https://files.rcsb.org/download/{ID}.pdb"


def _insert(db: Path, url: str, key: str, *, pending_key: str = "",
            claimed_at: str = "") -> None:
    with closing(_connect(db)) as conn:
        conn.execute(
            "INSERT INTO url_index"
            " (url_canonical, zotero_key, first_seen, last_seen, pending_key, claimed_at)"
            " VALUES (?, ?, '2026-01-01', '2026-01-01', ?, ?)",
            (url, key, pending_key, claimed_at),
        )


def _rows(db: Path) -> list[dict]:
    return retire_rows._read_rows(db)


def test_an_in_flight_reservation_is_not_retired(tmp_path: Path) -> None:
    """`zotero_key == ""` is not proof the claim never completed.

    It is also the NORMAL state between reserve_url() and the POST returning.
    Retire read it as row-only garbage and deleted the row; capture's POST then
    landed, set_zotero_key matched nothing, and the item existed in Zotero with
    nothing in the index pointing at it -- invisible to dedup forever, which is
    precisely what the reservation protocol exists to prevent.
    """
    db = tmp_path / "idx.db"
    init_db(db)
    _insert(db, JUNK_URL, "", pending_key="PENDING1", claimed_at="2026-08-26T09:00:00")

    steps = plan_retire(_rows(db))

    assert steps == [], f"an unresolved claim was planned for deletion: {steps}"


def test_a_row_with_no_claim_at_all_is_still_retired(tmp_path: Path) -> None:
    """Positive control: narrowing this must not stop retirement working."""
    db = tmp_path / "idx.db"
    init_db(db)
    _insert(db, JUNK_URL, "")

    steps = plan_retire(_rows(db))

    assert [s.url for s in steps] == [JUNK_URL]
    assert steps[0].zotero_key == ""


def test_the_planner_is_actually_given_the_claim_columns(tmp_path: Path) -> None:
    """A guard the production reader cannot feed is not a guard.

    plan_retire's rows come from _read_rows, which selected only url and key --
    so the claim was invisible to the planner by construction.
    """
    db = tmp_path / "idx.db"
    init_db(db)
    _insert(db, JUNK_URL, "", pending_key="PENDING1")

    row = _rows(db)[0]

    assert "pending_key" in row, f"the planner cannot see the claim: {sorted(row)}"


def test_retire_does_not_delete_a_row_another_session_replaced(tmp_path: Path) -> None:
    """Finalize was `WHERE url_canonical = ?` with no key term.

    Plan names K1. Another session drops that row and capture recreates the URL
    as K2. Retire then trashes K1 and deletes "the row for this URL" -- which is
    now K2's, an item nobody trashed. K2 becomes an unindexed remote orphan.
    """
    db = tmp_path / "idx.db"
    init_db(db)
    _insert(db, JUNK_URL, "K1")
    steps = plan_retire(_rows(db))
    assert [s.zotero_key for s in steps] == ["K1"], "positive control: K1 was planned"

    # the replacement happens between planning and applying
    with closing(_connect(db)) as conn:
        conn.execute(
            "UPDATE url_index SET zotero_key = 'K2' WHERE url_canonical = ?",
            (JUNK_URL,),
        )

    zotero = _FakeZotero()
    apply_retire(steps, db_path=db, zotero=zotero, connect=_connect)

    with closing(_connect(db)) as conn:
        left = [dict(r) for r in conn.execute("SELECT * FROM url_index")]
    assert [r["zotero_key"] for r in left] == ["K2"], (
        "the replacement row was deleted for an item that was never trashed"
    )


def test_retire_still_deletes_the_row_it_planned(tmp_path: Path) -> None:
    """Positive control for the compare-and-swap: the ordinary case must work."""
    db = tmp_path / "idx.db"
    init_db(db)
    _insert(db, JUNK_URL, "K1")
    _insert(db, "https://example.org.uk/real", "GOOD1")

    steps = plan_retire(_rows(db))
    zotero = _FakeZotero()
    counts = apply_retire(steps, db_path=db, zotero=zotero, connect=_connect)

    assert zotero.trashed == ["K1"]
    assert counts["trashed"] == 1
    with closing(_connect(db)) as conn:
        left = [r[0] for r in conn.execute("SELECT url_canonical FROM url_index")]
    assert left == ["https://example.org.uk/real"]


# --- the other half of the race: capture must notice its claim vanished ------


def test_capture_reports_an_item_whose_index_row_vanished(tmp_path: Path) -> None:
    """`set_zotero_key` returns True only if it took, and capture threw it away.

    It is a compare-and-swap whose whole purpose is to fail when the claim has
    been taken over or deleted -- and its result went unread, so the one moment
    the system could notice an item had been stranded in Zotero passed in
    silence.
    """
    from datetime import date

    from zotero_capture.capture import capture_message

    db = tmp_path / "idx.db"
    init_db(db)
    url = "https://fixturehost.org/stranded"

    class _DeletesTheRowMidFlight:
        """A retire pass landing between the reservation and the POST."""

        def __init__(self) -> None:
            self.posted: list[str] = []

        def post_webpage_item(self, *, url_canonical, title, access_date, tags,
                              item_key=None, **kw):
            with closing(_connect(db)) as conn:
                conn.execute(
                    "DELETE FROM url_index WHERE url_canonical = ?", (url_canonical,)
                )
            self.posted.append(url_canonical)
            return item_key or "KEY"

        def add_tags(self, *a, **k):
            pass

        def item_exists(self, key):
            return True

    zotero = _DeletesTheRowMidFlight()
    result = capture_message(
        message=f"see {url}",
        project_slug="p",
        context=None,
        today=date(2026, 8, 26),
        db_path=db,
        zotero=zotero,
        title_fetcher=lambda u: "t",
    )

    assert zotero.posted == [url], "positive control: the item was created"
    assert any(e.code == "claim_lost" for e in result.errors), (
        f"an item was stranded in Zotero and nothing recorded it: {result.errors}"
    )


def test_an_ordinary_capture_reports_no_claim_loss(tmp_path: Path) -> None:
    """Positive control: the new check must not fire on the normal path."""
    from datetime import date

    from zotero_capture.capture import capture_message

    db = tmp_path / "idx.db"
    init_db(db)

    class _Ok:
        def post_webpage_item(self, *, item_key=None, **kw):
            return item_key or "KEY"

        def add_tags(self, *a, **k):
            pass

        def item_exists(self, key):
            return True

    result = capture_message(
        message="see https://fixturehost.org/fine",
        project_slug="p",
        context=None,
        today=date(2026, 8, 26),
        db_path=db,
        zotero=_Ok(),
        title_fetcher=lambda u: "t",
    )
    assert result.urls_new == 1
    assert result.errors == []
