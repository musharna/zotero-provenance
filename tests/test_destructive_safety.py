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
