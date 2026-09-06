"""The repair pass must fix the damage without inventing URLs nobody cited.

116 rows in the deployed index end in markdown emphasis the old extractor kept.
They can never resolve a title, so they sit in the library as URL-as-title junk.
The extractor rewrite stops new ones; these tests cover cleaning up the old.

The interesting part is what it refuses to touch. Some mangled-looking rows are
not URLs at all — a regex like "https://data\\.gramene\\.org/v69/genes.*" was
captured as one — and "fixing" the trailing ".*" would fabricate a URL that was
never cited. Others gained an "=" from the old canonicaliser, and "?a=1&b=" and
"?a=1&b" cannot be told apart after the fact.
"""

from __future__ import annotations

import pytest

from zotero_capture.repair import correct_url, plan_repair, repaired_url


def _row(url: str, key: str = "KEY1") -> dict:
    return {"url_canonical": url, "zotero_key": key}


@pytest.mark.parametrize(
    "mangled, expected",
    [
        ("https://h.example/abs/2602.06718)**", "https://h.example/abs/2602.06718"),
        (
            "https://h.example/artifact/022b9b86**",
            "https://h.example/artifact/022b9b86",
        ),
        ("https://h.example/p)", "https://h.example/p"),
        # Stripping "**" can uncover a trailing slash, which is not canonical.
        # Left as-is the row would never match a lookup, so the repair would add
        # a permanent near-duplicate instead of removing one.
        ("https://h.example/bios/**", "https://h.example/bios"),
    ],
)
def test_correct_url_strips_the_markdown_that_was_kept(mangled, expected):
    assert correct_url(mangled) == expected


@pytest.mark.parametrize(
    "clean",
    [
        "https://h.example/wiki/Aestivation_(botany)",
        "https://h.example/pii/S0092867400808763",
        "https://h.example/s?q=a+b",
        # RFC 3986 makes "_" unreserved and "*" a sub-delimiter. An earlier cut
        # of this repair stripped both and proposed to "fix" the first of these,
        # which is a real URL in the live index.
        "https://foo.example/release_",
        "https://h.example/path*",
    ],
)
def test_correct_url_leaves_a_healthy_url_alone(clean):
    """Negative control: these characters are URL data, not markdown."""
    assert correct_url(clean) == clean


def test_a_url_legitimately_ending_in_a_bold_run_would_be_altered():
    """The limit of this repair, asserted rather than left as a surprise.

    "<https://h.example/s?q=**>" is a legal autolink whose query really does end
    in two asterisks, and nothing in the stored string distinguishes it from a
    URL that picked them up from bold markdown. The repair cannot tell, which is
    why it is a one-off over an inspected set rather than a general cleaner: the
    dry run lists every change so the set can be checked before it is applied.
    No row in the live index is of this kind.
    """
    assert correct_url("https://h.example/s?q=**") == "https://h.example/s?q="


def test_a_single_trailing_asterisk_is_reported_not_repaired():
    """One "*" is ambiguous; in the live index it marks wildcards, not damage."""
    steps = plan_repair([_row("https://h.example/path*")])
    assert [s.action for s in steps] == ["skip"]
    assert steps[0].corrected == "", "an ambiguous row must not carry a correction"


def test_a_wildcard_pattern_is_reported_not_repaired():
    steps = plan_repair([_row("https://*.example.com/*")])
    assert [s.action for s in steps] == ["skip"]
    assert "not a URL" in steps[0].reason


def test_a_mangled_row_whose_correction_is_new_is_rewritten():
    steps = plan_repair([_row("https://h.example/p**")])
    assert [s.action for s in steps] == ["rewrite"]
    assert steps[0].corrected == "https://h.example/p"


def test_a_mangled_row_whose_correction_exists_is_merged():
    steps = plan_repair(
        [_row("https://h.example/p**", "BAD1"), _row("https://h.example/p", "GOOD1")]
    )
    merges = [s for s in steps if s.action == "merge"]
    assert len(merges) == 1
    assert merges[0].zotero_key == "BAD1", "the mangled copy is the one to retire"


def test_a_healthy_row_produces_no_step():
    """Positive control: the pass must not churn rows that are already right."""
    assert plan_repair([_row("https://h.example/wiki/X_(y)")]) == []


def test_a_regex_captured_as_a_url_is_reported_not_repaired():
    steps = plan_repair([_row("https://data\\.gramene\\.org/v69/genes.*")])
    assert [s.action for s in steps] == ["skip"]
    assert "not a URL" in steps[0].reason


def test_an_escaped_string_is_never_rewritten():
    """The dangerous case: stripping ".*" would fabricate a plausible URL."""
    steps = plan_repair([_row("https://rest\\.uniprot\\.org/uniprotkb/search.*")])
    assert steps[0].action == "skip"
    assert steps[0].corrected == ""


def test_a_query_that_gained_an_equals_is_left_alone():
    """Ambiguous after the fact, and both forms are valid: guessing has no upside."""
    assert plan_repair([_row("https://h.example/x?sig=a&b=")]) == []


def test_a_row_with_no_item_yet_is_skipped():
    steps = plan_repair([_row("https://h.example/p**", "")])
    assert steps[0].action == "skip"
    assert "claim" in steps[0].reason


def test_the_repair_runs_against_an_index_written_by_an_older_release(tmp_path):
    """Regression: every merge failed on the live index with "no such column".

    The repair reads columns that arrived by migration, but the tool never
    brought the schema up to date, and the index it runs against was written by
    an older release. The rewrites went through (they touch no such column) and
    all 64 merges failed, which is the worst shape for a partial pass to take.
    """
    import sqlite3

    from zotero_capture.sqlite_cache import init_db, lookup_url

    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE url_index (url_canonical TEXT PRIMARY KEY, zotero_key TEXT"
        " NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO url_index VALUES ('https://h.example/p', 'K1', '2026-01-01',"
        " '2026-01-01')"
    )
    legacy.commit()
    legacy.close()

    init_db(db)  # what the tool must do before reading

    row = lookup_url(db, "https://h.example/p")
    assert row is not None and row["zotero_key"] == "K1"
    assert row["pending_key"] == "" and row["claimed_at"] == ""


# --- tails the old blacklist tokenizer swallowed (0.11.3) ---


@pytest.mark.parametrize(
    "damaged, expected",
    [
        # A shell line-continuation, swept up because "\" was in no exclusion.
        ("https://cloud.r-project.org\\", "https://cloud.r-project.org"),
        # An unpadded table cell.
        (
            "https://github.com/someone/some-repo.git|",
            "https://github.com/someone/some-repo.git",
        ),
        # An ANSI reset from a stack trace pasted into a message.
        ("https://sqlalche.me/e/20/e3q8\x1b[0m\x1b[4;94m", "https://sqlalche.me/e/20/e3q8"),
    ],
)
def test_an_illegal_tail_is_cut(damaged, expected):
    assert correct_url(damaged) == expected


def test_a_template_is_not_cut_into_a_real_directory():
    """The refusal that keeps this from manufacturing citations.

    Cutting at "{" yields "https://files.rcsb.org/download/", which resolves and
    would acquire a genuine title — a source nobody cited. URL text resumes after
    the brace, so there is no address to recover and the row goes to retirement.
    """
    url = "https://files.rcsb.org/download/{ID}.pdb"
    assert correct_url(url) == url
    assert repaired_url(url) == ""


def test_a_cut_that_leaves_no_host_is_not_a_repair():
    """Replacing junk with "https://" would report success and store worse junk."""
    assert repaired_url("https://\x1b[0m") == ""


def test_a_merge_does_not_carry_the_duplicate_s_unresolved_title_tag(tmp_path):
    """Provenance moves across a merge; the duplicate's title state does not.

    Observed live: repairing "…/some-repo.git|" merged it into the clean row,
    and the survivor — which had a perfectly good title — came out tagged
    title:unresolved. That sticks, because title_is_unresolved() trusts the tag
    over the title in front of it, so the item reads as junk forever.
    """
    import sqlite3

    from zotero_capture.repair import RepairStep, apply_repair
    from zotero_capture.sqlite_cache import init_db

    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/repo', 'SURVIVOR', '2026-01-01', '2026-01-01')"
        )

    class _Zotero:
        def __init__(self):
            self.added: dict[str, list[str]] = {}
            self.trashed: list[str] = []

        def item_exists(self, key):
            return True

        def get_item_tags(self, key):
            return ["project:x", "seen:2026-08-23", "title:unresolved"]

        def add_tags(self, key, tags, **kw):
            self.added[key] = list(tags)
            return True

        def trash_item(self, key, *, expect_url=None):
            self.trashed.append(key)
            return True
    z = _Zotero()
    step = RepairStep(
        "https://h.example/repo|", "DUPLICATE", "merge", "https://h.example/repo"
    )
    counts = apply_repair([step], db_path=db, zotero=z, connect=connect)

    assert counts["merge"] == 1
    assert z.trashed == ["DUPLICATE"]
    # Positive and negative in one assertion set: provenance arrived, state did not.
    assert "project:x" in z.added["SURVIVOR"]
    assert "seen:2026-08-23" in z.added["SURVIVOR"]
    assert "title:unresolved" not in z.added["SURVIVOR"]


def test_a_merge_is_refused_when_the_survivor_has_no_item(tmp_path):
    """The bad row's item must not be trashed to make way for a claim that never completed.

    Found by an external audit of 0.11.3 and reproduced before being accepted.
    plan_repair() chose "merge" purely because the corrected URL was present in
    the index — but that row's claim had never completed, so its zotero_key was
    empty. apply_repair() skipped the tag carry (correctly) and then trashed the
    duplicate anyway, which was the ONLY real item. What survived was an orphan
    row pointing at nothing.

    Skip is the answer rather than a downgrade to rewrite: the clean URL already
    occupies the primary key, so the rewrite's UPDATE would fail after the Zotero
    item had already been changed, leaving remote and local disagreeing.
    """
    import sqlite3

    from zotero_capture.repair import apply_repair, plan_repair
    from zotero_capture.sqlite_cache import init_db

    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path', '', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path|', 'BADITEM', '2026-01-01', '2026-01-01')"
        )
        rows = [dict(r) for r in conn.execute("SELECT url_canonical, zotero_key FROM url_index")]

    class _Zotero:
        def __init__(self):
            self.trashed: list[str] = []

        def item_exists(self, key):
            return False

        def get_item_tags(self, key):
            return ["project:x"]

        def add_tags(self, key, tags, **kw):
            return True

        def trash_item(self, key, *, expect_url=None):
            self.trashed.append(key)
            return True
    z = _Zotero()
    counts = apply_repair(plan_repair(rows), db_path=db, zotero=z, connect=connect)

    assert z.trashed == [], "the only real item must survive"
    assert counts["merge"] == 0
    with connect(db) as conn:
        surviving = sorted(r[0] for r in conn.execute("SELECT url_canonical FROM url_index"))
    assert surviving == ["https://h.example/path", "https://h.example/path|"]
    # 0.61.0: a refusal closes its journal step. It `continue`d after
    # journal.step() with no outcome, so it read as an interrupted operation.
    from zotero_capture.opjournal import unfinished_operations

    assert unfinished_operations(db) == []


def test_a_merge_refused_at_the_trash_leaves_the_survivor_s_tags_alone(tmp_path):
    """add_tags(survivor) ran BEFORE the trash compare-and-swap, so a refused
    trash left the duplicate's tags on the survivor with the duplicate still
    live -- half a merge, journalled as nothing."""
    import sqlite3

    from zotero_capture.opjournal import unfinished_operations
    from zotero_capture.repair import apply_repair, plan_repair
    from zotero_capture.sqlite_cache import init_db

    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path', 'GOODITEM', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path|', 'BADITEM', '2026-01-01', '2026-01-01')"
        )
        rows = [dict(r) for r in conn.execute("SELECT url_canonical, zotero_key FROM url_index")]

    class _Zotero:
        def __init__(self):
            self.tagged: list = []

        def item_exists(self, key):
            return True

        def get_item_tags(self, key):
            return ["project:x"]

        def add_tags(self, key, tags, **kw):
            self.tagged.append((key, sorted(tags)))
            return True

        def trash_item(self, key, *, expect_url=None):
            return False  # no longer the item that was planned

    z = _Zotero()
    counts = apply_repair(plan_repair(rows), db_path=db, zotero=z, connect=connect)

    assert counts["merge"] == 0 and counts["skip"] == 1
    assert z.tagged == [], "tags moved onto the survivor for a merge that did not happen"
    assert unfinished_operations(db) == []


def test_a_merge_still_happens_when_the_survivor_is_a_real_item(tmp_path):
    """Positive control: the guard must not turn every merge into a skip."""
    import sqlite3

    from zotero_capture.repair import apply_repair, plan_repair
    from zotero_capture.sqlite_cache import init_db

    db = tmp_path / "index.db"
    init_db(db)

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    with connect(db) as conn:
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path', 'GOODITEM', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO url_index (url_canonical, zotero_key, first_seen, last_seen)"
            " VALUES ('https://h.example/path|', 'BADITEM', '2026-01-01', '2026-01-01')"
        )
        rows = [dict(r) for r in conn.execute("SELECT url_canonical, zotero_key FROM url_index")]

    class _Zotero:
        def __init__(self):
            self.trashed: list[str] = []
            self.added: dict[str, list[str]] = {}

        def item_exists(self, key):
            return True

        def get_item_tags(self, key):
            return ["project:x"]

        def add_tags(self, key, tags, **kw):
            self.added[key] = list(tags)
            return True

        def trash_item(self, key, *, expect_url=None):
            self.trashed.append(key)
            return True
    z = _Zotero()
    counts = apply_repair(plan_repair(rows), db_path=db, zotero=z, connect=connect)
    assert counts["merge"] == 1
    assert z.trashed == ["BADITEM"]
    assert z.added["GOODITEM"] == ["project:x"]
