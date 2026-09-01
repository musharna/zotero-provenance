"""A URL lives in two stores, and a write must not be able to touch only one.

On 2026-08-22 a one-off script in a scratch directory corrected 24 truncated
URLs like this:

    with build_client(load_config(), timeout=30.0) as z:
        for o in reps:
            z.update_url(o["key"], o["new"])      # the item. and nothing else.

The Zotero items became right and the index rows stayed wrong. Nine days later
`snapshot` fetched those stale index strings, got 404s, and stamped `gone` -- so
the repair MANUFACTURED the link rot that 0.41.0 was then built to undo.

The lesson is not "remember the index next time". `url_canonical` is the index
PRIMARY KEY and `item.url` is the library's copy of the same identity; moving it
is ONE operation. That it took two writes was visible only in `repair.py`'s
ordering, and a convention cannot bind a script written at 2am in job-tmp.

So the one-store route is removed rather than guarded: `move_url` REQUIRES a
db_path and a client, and no public method on the client writes the url field.
A future scratch script reaching for the obvious API gets the paired one, because
it is the only one there is.

The second half of this file is the trashed-item rule. `_try_add_tags` refused a
trashed item and said why -- "tagging a trashed item would quietly resurrect
provenance onto something the user removed" -- while `record_content_hash` and
the URL write never checked, because Zotero's trash reads 200 OK with
`deleted: 1` rather than 404. One rule, four copies of the read-and-check
preamble, implemented in one of them. That is this project's FIFTH
stale-second-copy defect, so the rule is now defined once and every writer is
made to route through it -- and the guard DERIVES the set of writers from the
code rather than naming them, because a hand-maintained list cannot fail on a
call site that is not on it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from zotero_capture import zotero_client as zc
from zotero_capture.sqlite_cache import init_db, insert_url, row_for_url
from zotero_capture.url_move import move_url
from zotero_capture.zotero_client import ItemGone, ZoteroClient

SEEN = __import__("datetime").date(2026, 5, 5)
OLD = "https://forge.invalid/wiki/Aestivation_(botany"
NEW = "https://forge.invalid/wiki/Aestivation_(botany)"


def _client(handler) -> ZoteroClient:
    return ZoteroClient(
        api_key="fake",
        library_id="0000",
        library_type="user",
        web_sources_collection_key="COLL1",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def connect():
    """The same connection factory the maintenance CLIs hand to repair."""
    import sqlite3
    from contextlib import closing

    def _connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return closing(conn)

    return _connect


# --- the trashed-item rule, derived rather than listed -----------------------


def _methods_that_patch_an_item() -> list[str]:
    """Every ZoteroClient method that PATCHes an item, read out of the source.

    Derived from the definition -- "a method that writes to an item" -- and not
    from the writers that happened to exist when this was written. The UA guard
    in test_version.py had one test per KNOWN call site and so could not fail on
    snapshot.py, which was written six days later. Same defect in a test that the
    test exists to prevent in the code.
    """
    tree = ast.parse(Path(zc.__file__).read_text())
    cls = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ZoteroClient"
    )
    out = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("patch", "delete")
            ):
                out.append(fn.name)
                break
    return out


def test_every_item_writer_routes_through_the_shared_gate() -> None:
    """The rule is defined once and inherited, not copied into each writer.

    Four copies of the same read-and-check preamble is how the trash rule ended
    up in exactly one of them.
    """
    tree = ast.parse(Path(zc.__file__).read_text())
    cls = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ZoteroClient"
    )
    writers = set(_methods_that_patch_an_item())
    assert writers, "derivation found no item writers; the guard would be vacuous"

    missing = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in writers:
            continue
        calls = {
            node.func.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        if "_open_for_write" not in calls:
            missing.append(fn.name)
    assert not missing, (
        f"these item writers do their own GET instead of going through "
        f"_open_for_write, so each carries its own copy of the trash and "
        f"expect_url rules: {sorted(missing)}"
    )


def test_stamping_a_hash_refuses_a_trashed_item() -> None:
    """THE defect: snapshot writing provenance onto something the user threw away.

    Reachable when found: 7 live index rows pointed at trashed items.
    """
    patched: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={
                    "key": "K1",
                    "version": 7,
                    "data": {"key": "K1", "url": NEW, "deleted": 1},
                },
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    with _client(handler) as c:
        assert c.record_content_hash("K1", "abc123") is False
    assert patched == [], "a trashed item was written to"


def test_stamping_a_hash_still_works_on_a_live_item() -> None:
    """The positive control. A change that simply stopped stamping would pass
    the test above while destroying the feature."""
    patched: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={"key": "K1", "version": 7, "data": {"key": "K1", "url": NEW}},
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    with _client(handler) as c:
        assert c.record_content_hash("K1", "abc123") is True
    assert patched and "Content-SHA256: abc123" in patched[0]["extra"]


def test_a_url_move_refuses_a_trashed_item() -> None:
    """Yesterday I repaired the URL of a trashed item without noticing."""
    patched: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={
                    "key": "K1",
                    "version": 7,
                    "data": {"key": "K1", "url": OLD, "deleted": 1},
                },
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    with _client(handler) as c:
        assert c._patch_item_url("K1", NEW, expect_url=OLD) is False
    assert patched == []


def test_trashing_an_already_trashed_item_is_not_refused() -> None:
    """The rule is "do not write provenance onto a removed item", not "never
    touch one". Refusing to remove something because it is already removed would
    make `retire` report a failure for work that is already done."""
    patched: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={
                    "key": "K1",
                    "version": 7,
                    "data": {"key": "K1", "url": NEW, "deleted": 1},
                },
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    with _client(handler) as c:
        assert c.trash_item("K1", expect_url=NEW) is True
    assert patched == [{"deleted": 1}]


def test_add_tags_still_raises_item_gone_for_a_trashed_item() -> None:
    """Its contract differs from the others on purpose: capture DROPS the row on
    ItemGone, and that behaviour must survive the rule moving."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={
                    "key": "K1",
                    "version": 7,
                    "data": {"key": "K1", "url": NEW, "deleted": 1},
                },
            )
        raise AssertionError("must not write")

    with _client(handler) as c:
        with pytest.raises(ItemGone):
            c.add_tags("K1", ["seen:2026-05-05"])


# --- there is no one-store route ---------------------------------------------


def test_the_client_exposes_no_public_way_to_write_a_url() -> None:
    """The mechanism removal, asserted structurally.

    `update_url` was added with no caller anywhere in the repo and was first
    called 39 minutes later by a script in job-tmp. Making the paired operation
    the only discoverable one is the fix; a docstring warning would not have been
    read by the script that did this.

    THE FIRST VERSION OF THIS TEST PASSED ON THE BROKEN CODE. It only looked at
    a dict LITERAL in the `patch(json=...)` call, and the real method built
    `payload = {"url": url}` a few lines earlier and passed the NAME. The write
    hid one hop behind a variable -- exactly how the User-Agent literal-detector
    missed its drift behind a named constant. So the search is over the whole
    function body: any `{"url": ...}` built anywhere in a public method that
    PATCHes, plus `payload["url"] = ...`. Running a guard against the state it
    is meant to catch is the only reason this is not still green and wrong.
    """
    tree = ast.parse(Path(zc.__file__).read_text())
    cls = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ZoteroClient"
    )
    offenders = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_"):
            continue
        patches = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "patch"
            for n in ast.walk(fn)
        )
        if not patches:
            continue
        for node in ast.walk(fn):
            keys: list = []
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            elif isinstance(node, ast.Subscript) and isinstance(
                node.slice, ast.Constant
            ):
                keys = [node.slice.value]
            if "url" in keys:
                offenders.append(fn.name)
                break
    assert not offenders, (
        f"these PUBLIC client methods write an item's url without the index: "
        f"{sorted(offenders)}. A URL move must go through move_url, which "
        f"cannot be called without a db_path."
    )


def test_move_url_cannot_be_called_without_both_stores() -> None:
    """Not a style point. The 2am script had a client and no db_path; with this
    signature it could not have written half the identity."""
    import inspect

    params = inspect.signature(move_url).parameters
    assert "db_path" in params
    assert "zotero" in params


# --- the paired move ----------------------------------------------------------


@pytest.fixture
def db(tmp_db: Path) -> Path:
    init_db(tmp_db)
    insert_url(tmp_db, OLD, "K1", SEEN)
    return tmp_db


def _moving_client(patched: list):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={"key": "K1", "version": 7, "data": {"key": "K1", "url": OLD}},
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    return _client(handler)


def test_move_url_writes_both_stores(db: Path, connect) -> None:
    patched: list = []
    with _moving_client(patched) as c:
        reason = move_url(db, c, zotero_key="K1", old=OLD, new=NEW, connect=connect)
    assert reason == ""
    assert patched[0]["url"] == NEW, "the item was not moved"
    assert row_for_url(db, NEW) is not None, "the index row was not moved"
    assert row_for_url(db, OLD) is None, "the old index row survived"


def test_move_url_leaves_the_index_alone_when_the_item_is_gone(
    db: Path, connect
) -> None:
    """The half-write, in the direction the original defect did NOT go -- and
    the one that would leave the index claiming a URL with nothing behind it."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with _client(handler) as c:
        reason = move_url(db, c, zotero_key="K1", old=OLD, new=NEW, connect=connect)
    assert reason
    assert row_for_url(db, OLD) is not None, "the index moved without the item"
    assert row_for_url(db, NEW) is None


def test_move_url_refuses_when_the_item_is_no_longer_the_one_selected(
    db: Path, connect
) -> None:
    """expect_url is carried through the paired operation, not left to callers."""
    patched: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                headers={"Last-Modified-Version": "7"},
                json={
                    "key": "K1",
                    "version": 7,
                    "data": {"key": "K1", "url": "https://forge.invalid/other"},
                },
            )
        patched.append(json.loads(req.content))
        return httpx.Response(204)

    with _client(handler) as c:
        reason = move_url(db, c, zotero_key="K1", old=OLD, new=NEW, connect=connect)
    assert reason
    assert patched == []
    assert row_for_url(db, OLD) is not None


# --- the detector, which must look in BOTH directions -------------------------


def test_verify_finds_an_item_the_index_does_not_name() -> None:
    """The whole reason 49 stranded items went unreported for months.

    Every other maintenance tool starts from `SELECT ... FROM url_index`, so it
    can only ever check the rows it enumerated -- an item the index does not
    name is not merely unchecked, it is unreachable. A one-directional sweep
    that reports "nothing wrong" is answering a question about its own input.
    """
    import verify_index

    rows = [
        {
            "url_canonical": "https://a.test/kept",
            "zotero_key": "K1",
            "pending_key": "",
            "first_seen": "2026-05-05",
        }
    ]
    items = [
        {"key": "K1", "url": "https://a.test/kept", "title": "", "dateAdded": ""},
        {"key": "K2", "url": "https://a.test/lost", "title": "", "dateAdded": ""},
    ]

    found = verify_index.compare(rows, items)
    assert [i["key"] for i in found["orphan_stranded"]] == ["K2"]
    assert found["url_disagreement"] == []


def test_verify_separates_a_duplicate_from_a_merely_stranded_item() -> None:
    """A stranded item whose URL is ALREADY indexed under another key is not the
    same finding: the duplicate has already happened. Keeping them in one bucket
    would have hidden that 30 of the 49 were real duplicates."""
    import verify_index

    rows = [
        {
            "url_canonical": "https://a.test/x",
            "zotero_key": "K1",
            "pending_key": "",
            "first_seen": "2026-05-05",
        }
    ]
    items = [
        {"key": "K1", "url": "https://a.test/x", "title": "", "dateAdded": ""},
        {"key": "K2", "url": "https://a.test/x", "title": "", "dateAdded": ""},
    ]

    found = verify_index.compare(rows, items)
    assert [i["key"] for i in found["orphan_duplicate"]] == ["K2"]
    assert found["orphan_stranded"] == []


def test_verify_does_not_call_an_unresolved_claim_a_fault() -> None:
    """The positive control on the other side: a detector that flags everything
    is as useless as one that flags nothing. These two rows are the recoverable-
    claim protocol working exactly as designed."""
    import verify_index

    rows = [
        {
            "url_canonical": "https://a.test/x",
            "zotero_key": "",
            "pending_key": "K9",
            "first_seen": "2026-05-05",
        }
    ]
    items = [{"key": "K9", "url": "https://a.test/x", "title": "", "dateAdded": ""}]

    found = verify_index.compare(rows, items)
    assert [i["key"] for i in found["orphan_pending_claim"]] == ["K9"]
    assert found["orphan_duplicate"] == []
    assert found["orphan_stranded"] == []


def test_verify_sees_a_url_disagreement_in_either_direction() -> None:
    """The original defect: item and row holding two different strings."""
    import verify_index

    rows = [
        {
            "url_canonical": OLD,
            "zotero_key": "K1",
            "pending_key": "",
            "first_seen": "2026-05-05",
        }
    ]
    items = [{"key": "K1", "url": NEW, "title": "", "dateAdded": ""}]

    found = verify_index.compare(rows, items)
    assert len(found["url_disagreement"]) == 1
    row, item = found["url_disagreement"][0]
    assert row["url_canonical"] == OLD and item["url"] == NEW
