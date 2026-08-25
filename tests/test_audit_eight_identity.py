"""The index must know which library it is an index OF.

It is a cache of what one Zotero collection holds, keyed only by URL. Point it
at a different library or collection and every row silently means something
else — but nothing notices, because nothing was ever recorded about what the
rows referred to.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from zotero_capture.sqlite_cache import (
    IndexIdentityMismatch,
    bind_identity,
    init_db,
    insert_url,
    read_identity,
)

A = {
    "api_origin": "https://api.zotero.org",
    "library_type": "user",
    "library_id": "6532713",
    "collection_key": "AAAACOLL",
}
B = {**A, "collection_key": "BBBBCOLL"}


def test_a_fresh_index_adopts_the_identity_it_is_opened_with(tmp_path: Path):
    db = tmp_path / "url_index.db"
    init_db(db)
    bind_identity(db, **A)
    assert read_identity(db) == A


def test_reopening_with_the_same_identity_is_fine(tmp_path: Path):
    """Positive control — the ordinary path must not be disturbed."""
    db = tmp_path / "url_index.db"
    init_db(db)
    bind_identity(db, **A)
    bind_identity(db, **A)
    assert read_identity(db) == A


def test_a_different_collection_is_refused(tmp_path: Path):
    """Switching collection silently split sources in two.

    New URLs went into the new collection; recurring ones were only TAGGED, and
    tagging does not move an item — so the recurring half stayed behind while
    the index reported success for both.
    """
    db = tmp_path / "url_index.db"
    init_db(db)
    bind_identity(db, **A)
    insert_url(db, "https://fixturehost.org/x", "KEY12345", date(2026, 5, 5))
    with pytest.raises(IndexIdentityMismatch) as e:
        bind_identity(db, **B)
    assert "collection_key" in str(e.value)


def test_a_different_library_is_refused(tmp_path: Path):
    """Switching library made every row a permanent 404.

    The stored keys belong to the old library, so each recurrence GETs a key
    that is not there — and the row blocks the URL from ever being created
    again.
    """
    db = tmp_path / "url_index.db"
    init_db(db)
    bind_identity(db, **A)
    insert_url(db, "https://fixturehost.org/y", "KEY12345", date(2026, 5, 5))
    with pytest.raises(IndexIdentityMismatch):
        bind_identity(db, **{**A, "library_id": "9999999"})


def test_an_index_from_before_this_change_adopts_rather_than_refuses(tmp_path: Path):
    """The live index has thousands of rows and no identity. It must not break.

    An unbound index cannot be MISmatched — there is nothing to disagree with —
    so the first open records the identity it is opened with. That is a real
    assumption, stated rather than hidden: it trusts that whoever upgrades is
    still pointing at the library those rows came from.
    """
    db = tmp_path / "url_index.db"
    init_db(db)
    insert_url(db, "https://fixturehost.org/legacy", "KEY12345", date(2026, 5, 5))
    bind_identity(db, **A)
    assert read_identity(db) == A


def test_an_empty_unbound_index_rebinds_freely(tmp_path: Path):
    """With no rows there is nothing to protect, so changing target is allowed."""
    db = tmp_path / "url_index.db"
    init_db(db)
    bind_identity(db, **A)
    from zotero_capture.sqlite_cache import _connect
    from contextlib import closing

    with closing(_connect(db)) as conn:
        conn.execute("DELETE FROM url_index")
    bind_identity(db, **B)
    assert read_identity(db) == B
