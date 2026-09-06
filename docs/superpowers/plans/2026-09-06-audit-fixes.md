# Audit Fixes 2026-09-06 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all 19 findings of the 2026-09-06 whole-repo audit in four releases (0.58.0–0.61.0), each merged, deployed, and verified executing.

**Architecture:** Fix each defect at the layer that produces the bad signal, not where it is consumed: negative limits are refused in `sqlite_cache`, the host boundary is fixed in `_host_boundary`, the timeout is caught where it is raised. Every change is TDD; every guard is seen to fail first; every release ends with the full suite, a deploy, and the superseded-root execution check with its negative control.

**Tech Stack:** Python 3.12+, pytest (`python -m pytest -q`, `addopts = -m "not live"`), httpx MockTransport, sqlite3, bash hooks.

**Spec:** `~/.claude/projects/-home-mjarnold-zotero-provenance/memory/audit_whole_repo_2026-09-06.md` (finding ids H1–H2, M1–M7, L1–L8 below refer to it).

## Global Constraints

- No commit attribution lines. Commit subject is a sentence about the finding (repo style: "A refusal is not a document").
- Version triple must move together: `pyproject.toml:3`, `.claude-plugin/plugin.json:4`, `scripts/zotero_capture/__init__.py:6` (guarded by `tests/test_version.py`).
- CHANGELOG.md head entry per release, written from measurements, not intent.
- Every new test: run it RED first and confirm it fails for the stated reason; a negative assertion ships with a positive control in the same test.
- Never set `ZOTERO_*` in a test env; subprocess tests set `ZOTERO_CAPTURE_STATE_DIR` to `tmp_path` AND pop `ZOTERO_*` from the env copy.
- Live index/library mutations (Task 6) only after the release is deployed, journalled through `OperationJournal`, with a `.bak` copy first.
- Release ritual (Tasks 7, 12, 18, 24): full suite rc captured directly (not through `tail`), merge to master, push, deploy to the plugin cache, pin registry, prove EXECUTING via the superseded 0.20.2 hook logging the new version, with the `$HOME`-moved negative control logging 0.20.2.
- Temp files in `/home/mjarnold/.claude/jobs/f2500ce3/tmp/`.

---

# Release 0.58.0 — "A timeout is not a traceback" (H1)

### Task 1: Catch `HookTerminated` in `main()` and record it as a structured event

**Files:**
- Modify: `scripts/zotero_capture/cli.py:444-447` (the `except Exception` in `main`)
- Modify: `scripts/zotero_capture/health.py:40-50` (`REFUSAL_EVENTS`)
- Test: `tests/test_hook_terminated.py` (create)

**Interfaces:**
- Produces: bootstrap event name `"hook-terminated"` in `capture.log`, and `main()` returns 0 after it.

- [ ] **Step 1: Write the failing test**

```python
"""A hook timeout is a fact about US, and it must leave a structured record.

`HookTerminated` derives from BaseException so the per-URL loop cannot swallow
it, and `main()` caught only `Exception`, so the timeout escaped as a raw
traceback into capture.log. Health then read those lines as log corruption and
the next successful capture erased the finding. Live capture.log held 2 of them.
"""
import json
import os
import signal
from pathlib import Path

import pytest

from zotero_capture import cli
from zotero_capture.health import REFUSAL_EVENTS


def _events(state: Path) -> list[dict]:
    log = state / "capture.log"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines() if l.startswith("{")]


def test_a_timeout_writes_a_hook_terminated_event(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(state))
    for k in list(os.environ):
        if k.startswith("ZOTERO_") and k != "ZOTERO_CAPTURE_STATE_DIR":
            monkeypatch.delenv(k)
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_COLLECTION_KEY", "C")

    def blow_up(**_kw):
        raise cli.HookTerminated("terminated by signal 15")

    monkeypatch.setattr(cli, "run_capture", blow_up)
    rc = cli.main(["--cwd", "/tmp", "--session", "s", "--message", "see https://x.test/a"])

    assert rc == 0
    kinds = [e.get("event") for e in _events(state)]
    assert "hook-terminated" in kinds, kinds
    # Positive control: the event is one health counts as a refusal, or it is
    # written and never reported.
    assert "hook-terminated" in REFUSAL_EVENTS


def test_the_traceback_no_longer_reaches_the_log(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("ZOTERO_CAPTURE_STATE_DIR", str(state))
    monkeypatch.setenv("ZOTERO_API_KEY", "k")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("ZOTERO_COLLECTION_KEY", "C")
    monkeypatch.setattr(cli, "run_capture", lambda **_k: (_ for _ in ()).throw(cli.HookTerminated("t")))
    cli.main(["--cwd", "/tmp", "--message", "see https://x.test/a"])
    assert "Traceback" not in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_terminated.py -v`
Expected: first test errors with `HookTerminated` propagating out of `main` (it is a BaseException; pytest reports it as an error, which IS the defect); second likewise.

- [ ] **Step 3: Write minimal implementation**

In `cli.py` `main()`, before `except Exception as e:`:

```python
    except HookTerminated as e:
        # The hook's own timeout. A BaseException on purpose so the per-URL
        # loop cannot swallow it, which means it reaches HERE and nowhere else
        # catches it: left alone it printed a traceback that health counted as
        # an unreadable log and the next success erased. It is a refusal of
        # ours, recorded as one.
        _emit_bootstrap_event("hook-terminated", str(e))
        return 0
```

In `health.py` `REFUSAL_EVENTS`, append:

```python
    # The hook's own `timeout` fired mid-capture (0.58.0). A fact about us.
    "hook-terminated",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook_terminated.py tests/test_bootstrap_events.py tests/test_health.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_hook_terminated.py scripts/zotero_capture/cli.py scripts/zotero_capture/health.py
git commit -m "A timeout is a fact about us, not an unreadable log"
```

### Task 2: A POST interrupted by the timeout is queued for retry, not lost

**Files:**
- Modify: `scripts/zotero_capture/capture.py:419-427` (the `except BaseException` branch)
- Test: `tests/test_hook_terminated.py` (append)

**Interfaces:**
- Consumes: `enqueue_retry(db_path, url_canonical=, project=, context=, seen_date=, error=, now=)` from `sqlite_cache.py:602`, already imported in `capture.py`.

- [ ] **Step 1: Write the failing test**

Look at how `tests/test_capture.py` builds a fake `zotero` and calls `capture` (search for `post_webpage_item` there and copy the fixture shape). Then:

```python
from zotero_capture.sqlite_cache import init_db, retry_queue_depth


def test_a_post_cut_off_by_the_timeout_is_queued(tmp_path) -> None:
    """The claim must stand (the POST may have committed) AND the URL must be
    queued, so a drain settles it through _resolve_claim. Before: the claim
    stood and nothing ever revisited it -- two live rows sat for 5-6 days."""
    db = tmp_path / "i.db"
    init_db(db)

    class Z:
        def post_webpage_item(self, **_kw):
            raise cli.HookTerminated("terminated by signal 15")
        # copy the other no-op methods the capture fixture in test_capture.py uses

    with pytest.raises(cli.HookTerminated):
        # call `capture(...)` exactly as test_capture.py does, with zotero=Z()
        ...
    assert retry_queue_depth(db) == 1
    # Positive control: the claim was NOT released -- the POST went out.
    import sqlite3
    row = sqlite3.connect(db).execute(
        "SELECT zotero_key, pending_key FROM url_index").fetchone()
    assert row[0] == "" and row[1] != ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_terminated.py::test_a_post_cut_off_by_the_timeout_is_queued -v`
Expected: FAIL on `retry_queue_depth(db) == 1` (0 == 1).

- [ ] **Step 3: Write minimal implementation**

In `capture.py`, inside `except BaseException:` after the `if not issued:` release:

```python
                        else:
                            # The POST went out and was cut off. The claim
                            # stands (Zotero may have committed), but nothing
                            # would ever revisit it: _resolve_claim runs only
                            # when the URL is cited AGAIN. Queue it, so a drain
                            # replays it through the reservation and settles
                            # it by asking Zotero.
                            enqueue_retry(
                                db_path,
                                url_canonical=url,
                                project=project_slug,
                                context=context,
                                seen_date=today_iso,
                                error="interrupted after the POST was issued",
                                now=now.isoformat(),
                            )
                        raise
```

(Keep the existing `raise`; the `else` attaches to `if not issued`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook_terminated.py tests/test_capture.py tests/test_retry_queue.py -q` (use whatever the retry-queue test file is actually named: `ls tests | grep -i retry`).
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_hook_terminated.py scripts/zotero_capture/capture.py
git commit -m "A POST cut off by the timeout is queued, not forgotten"
```

### Task 3: A drain settles an issued claim instead of stalling on it

**Files:**
- Read: `scripts/drain_queue.py` and `scripts/zotero_capture/capture.py` `_resolve_claim` (grep `def _resolve_claim`).
- Test: `tests/test_hook_terminated.py` (append)

- [ ] **Step 1: Write the failing test** — drive a drain over the queued row from Task 2 with a fake Zotero whose `find_item_by_url`/equivalent (read `_resolve_claim` for the exact method) returns the pending key, and assert the row ends with `zotero_key == pending_key` and the queue is empty. If the test passes immediately, the drain already settles it: record that in the CHANGELOG as measured and delete the test's RED expectation, keep the test as the regression guard.

- [ ] **Step 2: Run** `python -m pytest tests/test_hook_terminated.py -v`. Expected: either FAIL (fix in `drain_queue.py` so a queued URL with an open claim is replayed through `capture`, not skipped) or PASS (document).

- [ ] **Step 3: Commit** `git commit -m "A drain settles a claim the timeout left open"` (only if code changed; otherwise fold the test into Task 2's commit with `--amend`).

### Task 4: Health names the two stranded claims instead of only refusing to touch them

**Files:**
- Modify: `scripts/verify_index.py:60-80` — `orphan_stranded` already exists; confirm an in-flight row (`zotero_key=''`) older than the stale-claim window is listed there, with its age.
- Test: `tests/test_verify_index.py` (append one test: a row with `zotero_key=''` and `first_seen` 5 days old appears in the report with "days" in its line).

- [ ] Steps: RED → implement (age column in the printed line) → GREEN → commit `"A stranded claim is reported with its age"`.

### Task 5: CHANGELOG + version 0.58.0

- [ ] Bump the three version files to `0.58.0`; run `python -m pytest tests/test_version.py -q`.
- [ ] Write the CHANGELOG entry: the two live tracebacks, the two stranded keys (IR5PD8S6, 8LUQ8R4J), what each task changed, mutation results (comment out the `except HookTerminated` → Task 1 test must fail; comment out `enqueue_retry` in the `else` → Task 2 test must fail; record both).
- [ ] Commit `"Release 0.58.0"`.

### Task 6: Recover the two live stranded claims (AFTER Task 7 deploys)

- [ ] `cp ~/.local/state/zotero-provenance/url_index.db ~/.local/state/zotero-provenance/url_index.db.bak-pre-0.58.0-stranded`
- [ ] Read-only: `SELECT url_canonical, pending_key, first_seen FROM url_index WHERE zotero_key=''` — expect exactly 2.
- [ ] Ask Zotero read-only whether items `IR5PD8S6` and `8LUQ8R4J` exist (`zotero.item_exists`) and record the answer BEFORE acting.
- [ ] Enqueue both via `enqueue_retry` in a `timeout 60 python3` snippet (journal the two rows first through `OperationJournal`), then `python3 scripts/drain_queue.py --limit 2`.
- [ ] Verify: 0 rows with `zotero_key=''`; queue depth 0; each URL indexed to exactly one item; `PRAGMA integrity_check` ok. Write the counts into the memory file `audit_whole_repo_2026-09-06.md` under H1.

### Task 7: Release ritual for 0.58.0

- [ ] `python -m pytest -q > /home/mjarnold/.claude/jobs/f2500ce3/tmp/suite_058.log 2>&1; echo rc=$?` — rc must be 0; record the count.
- [ ] Merge to master (`git merge --no-ff`), push, deploy to `~/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.58.0`, pin the registry (`dev/` has the deploy script used for 0.57.0; read `git log -1 --stat 53b4fd7` and the memory file `a_202_is_not_a_document_2026-09-05.md` "SHIPPED" section for the exact commands).
- [ ] Prove executing: fire the superseded 0.20.2 hook; `capture.log` must log `version 0.58.0`; repeat with `HOME` moved → logs `0.20.2` (negative control). Check isolation both ways (`env | grep ^ZOTERO_` shows nothing pointing at production during the probe).

---

# Release 0.59.0 — "A scope that lies" (M1, M2, L2, verify_dois `--limit 0`)

### Task 8: `--only-host` matches the site root

**Files:**
- Modify: `scripts/zotero_capture/sqlite_cache.py:790-801` (`_host_boundary`)
- Test: `tests/test_verify_filters.py` (append; find the existing boundary test that pins `notwikipedia.org` and add beside it)

- [ ] **Step 1: Write the failing test**

```python
def test_only_host_matches_the_site_root_row(tmp_path) -> None:
    """canonicalize strips a bare `/`, so `https://example.com` has an EMPTY
    path and `https://example.com/%` never matched it. 191 of 5,133 live rows
    are site roots. Boundary is still held: the two control rows must stay out."""
    db = _seed(tmp_path / "i.db", [
        "https://example.com",
        "https://example.com?q=1",
        "https://www.example.com",
        "https://example.com/x",
        "https://notexample.com",          # control: substring, not boundary
        "https://example.com.evil.test",   # control: suffix, not boundary
    ])
    seen: list[str] = []
    verify(db, hasher=_recording_hasher(seen), clock=lambda: "NOW", only_host="example.com")
    assert sorted(seen) == [
        "https://example.com",
        "https://example.com/x",
        "https://example.com?q=1",
        "https://www.example.com",
    ]
```

- [ ] **Step 2: Run** `python -m pytest tests/test_verify_filters.py::test_only_host_matches_the_site_root_row -v`. Expected: FAIL, `seen` holds only `/x`.

- [ ] **Step 3: Implement** — replace the four patterns with a boundary that allows end-of-string, `/`, `?`, `#` after the host:

```python
    # After the host the URL may END (canonicalize strips a bare "/"), or
    # continue with a path, query or fragment. Six patterns, two per scheme
    # per level, because LIKE has no alternation and "%" after the host would
    # match example.com.evil.test -- the suffix case the boundary exists for.
    pats: list[str] = []
    for scheme in ("http", "https"):
        for prefix in (f"{scheme}://{escaped}", f"{scheme}://%.{escaped}"):
            pats += [prefix, f"{prefix}/%", f"{prefix}?%"]
    clause = " AND (" + " OR ".join(["url_canonical LIKE ? ESCAPE '\\'"] * len(pats)) + ")"
    return clause, pats
```

Update the docstring to say so. (`#` never survives canonicalize — fragment is dropped at `url_processing.py:265`; do not add a pattern for it.)

- [ ] **Step 4: Run** `python -m pytest tests/test_verify_filters.py tests/test_snapshot.py -q`. Expected: pass, including the existing `notwikipedia.org` control.

- [ ] **Step 5: Commit** `git commit -m "A scoped run must reach the site root"`.

### Task 9: A negative `--limit` is refused where the SQL is built

**Files:**
- Modify: `scripts/zotero_capture/sqlite_cache.py:667,857,995` (three `LIMIT {int(limit)}` sites) — extract one helper.
- Modify: `scripts/verify_dois.py:60` (`if args.limit` → `is not None`).
- Test: `tests/test_verify_filters.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from zotero_capture.sqlite_cache import rows_needing_hash, rows_to_verify, pending_retries


@pytest.mark.parametrize("fn", [rows_needing_hash, rows_to_verify, pending_retries])
def test_a_negative_limit_is_refused_not_unlimited(tmp_path, fn) -> None:
    """SQLite reads `LIMIT -1` as no limit. `--limit -1` is what someone types
    to be careful, and it ran the whole corpus. Refused at the one place the
    SQL is built, so no CLI can forget."""
    db = _seed(tmp_path / "i.db", ["https://a.test/1", "https://b.test/1"])
    with pytest.raises(ValueError, match="limit"):
        fn(db, limit=-1)
    # Positive controls: 0 means none, 1 means one, None means all.
    assert len(fn(db, limit=0)) == 0
    assert len(fn(db, limit=1)) <= 1
    assert len(fn(db, limit=None)) == len(fn(db))
```

(Use the real function names: `grep -n "^def .*limit" scripts/zotero_capture/sqlite_cache.py`; adjust the parametrize list to the three functions at lines 660, 850, 988, and seed `pending_retries` via `enqueue_retry` if it reads `retry_queue`.)

Add a CLI-level test in `tests/test_verify_filters.py` beside `test_snapshot_only_flags_are_refused_rather_than_ignored`:

```python
def test_a_negative_limit_is_refused_at_the_cli(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZOTERO_")}
    env["ZOTERO_CAPTURE_STATE_DIR"] = str(tmp_path)
    for flags in (["--limit", "-1"], ["--verify", "--limit", "-1"]):
        done = subprocess.run([sys.executable, str(CLI), *flags],
                              capture_output=True, text=True, timeout=60, env=env)
        assert done.returncode != 0, flags
        assert "limit" in done.stderr
```

- [ ] **Step 2: Run** both. Expected: sqlite tests FAIL (no ValueError; `-1` returns all rows); CLI test FAIL (rc 0 or a config error — check the stderr says nothing about limit).

- [ ] **Step 3: Implement**

In `sqlite_cache.py`, one helper near the top:

```python
def _limit_clause(limit: int | None) -> str:
    """SQLite treats a negative LIMIT as no limit at all. Refused here, once,
    so `--limit -1` cannot mean 'the whole corpus' from any CLI."""
    if limit is None:
        return ""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    return f" LIMIT {int(limit)}"
```

Replace each `if limit is not None: sql += f" LIMIT {int(limit)}"` with `sql += _limit_clause(limit)`.

In `scripts/snapshot_pages.py` after the `--verify` combination check, and in `scripts/drain_queue.py` after `parse_args()`:

```python
    if args.limit is not None and args.limit < 0:
        p.error("--limit must be >= 0")
```

In `scripts/verify_dois.py:60`: `dois = list(by_url)[: args.limit] if args.limit is not None else list(by_url)`.

- [ ] **Step 4: Run** `python -m pytest tests/test_verify_filters.py tests/test_hash_coverage.py tests/test_destructive_safety.py -q`. Expected: pass.

- [ ] **Step 5: Commit** `git commit -m "A negative limit is a mistake, not the whole corpus"`.

### Task 10: The held count is computed before truncation

**Files:**
- Modify: `scripts/retire_rows.py:90-97`
- Test: `tests/test_destructive_safety.py` (append; find the existing `--limit` retire test for the fixture shape)

- [ ] **Step 1: Test**: 4 hard + 1 policy rows, `--limit 1 --dry-run`; assert stdout contains `(1 more are policy exclusions` and not `(4 more`.
- [ ] **Step 2: Run** — FAIL with `4 more`.
- [ ] **Step 3: Implement** — compute `held = len(plan_retire(rows, include_policy=True)) - len(plan_retire(rows))` BEFORE `steps = steps[: args.limit]` (i.e. move the calculation above line 90, against the untruncated plan).
- [ ] **Step 4: Run** the destructive-safety file. **Step 5: Commit** `"The held count is about the plan, not the slice"`.

### Task 11: CHANGELOG + version 0.59.0

- [ ] Bump triple; CHANGELOG entry with the live number (191 site-root rows) and the mutation results (restore the old `_host_boundary` patterns → Task 8 test fails; drop the `< 0` raise → Task 9 tests fail). Commit `"Release 0.59.0"`.

### Task 12: Release ritual for 0.59.0 (as Task 7), then one measured re-run

- [ ] After deploy: `python3 scripts/snapshot_pages.py --dry-run --only-host arxiv.org` before/after counts — the after count must include `http://export.arxiv.org` (a live site-root row). Record both numbers in CHANGELOG (amend the release entry in a follow-up commit, or in memory).

---

# Release 0.60.0 — "A test that cannot fail" (H2, M3, M4, M6, M7, L7, L8)

### Task 13: Replace the vacuous assertion; id-bearing stale records are re-importable

**Files:**
- Modify: `tests/test_health.py:296-305`
- Modify: `scripts/zotero_capture_health.py:155-170`
- Read: `scripts/zotero_capture/health_ledger.py:39-55` (`mutation_id(capture_id, url)` is a pure function — this is what makes the fix idempotent).

- [ ] **Step 1: Write the failing test** (replace the last two lines of `test_hook_reports_the_outage_shape` and add a new test):

```python
    # A 29h-old capture from a superseded root is an integrity incident. Before
    # 0.60.0 this line asserted a string that existed nowhere in the code.
    assert "integrity incident" in proc.stdout, proc.stdout


def test_a_stale_write_with_an_id_is_still_reported_when_the_ledger_is_gone(tmp_path) -> None:
    """The record carries incident_id, so the CLI assumed the ledger already
    held it and skipped the import. Lose health.db and every post-0.19 stale
    write vanished for good. mutation_id(capture_id, url) is a pure function,
    so re-importing under it is idempotent and cannot duplicate a live row."""
    state = tmp_path / "state"
    stale = "/home/u/.claude/plugins/cache/zotero-provenance/zotero-provenance/0.3.0"
    old = (_dt.now().astimezone() - timedelta(hours=29)).isoformat()
    _write_log(state, [_capture(old, root=stale, incident_id="abc123", url="https://x.test/a")])
    proc = _run_hook(state)
    assert "integrity incident" in proc.stdout, proc.stdout
    # Positive control: running it twice does not double the count.
    proc2 = _run_hook(state)
    assert proc2.stdout.count("1 open") == 1, proc2.stdout
```

(Check `_capture(...)`'s kwargs at the top of `test_health.py` and add `incident_id`/`url` passthrough if missing.)

- [ ] **Step 2: Run** `python -m pytest tests/test_health.py -k "outage_shape or ledger_is_gone" -v`. Expected: both FAIL — stdout is `''`.

- [ ] **Step 3: Implement** in `zotero_capture_health.py`: drop the `startswith("legacy:")` filter; for records with a real `incident_id`, import under `mutation_id(record_id, url)`:

```python
        from zotero_capture.health_ledger import mutation_id
        for item in found:
            rid = str(item["id"])
            key = rid if rid.startswith("legacy:") else mutation_id(rid, item.get("url"))
            open_incident(ledger, incident_id=key, ...)
```

Rewrite the comment: the duplication it feared came from importing under the CAPTURE key; the ledger's own key is per-mutation and derivable, so the import lands on the existing row (ON CONFLICT DO NOTHING) when it exists and recreates it when the ledger was lost.

- [ ] **Step 4: Run** `python -m pytest tests/test_health.py tests/test_ledger_faults.py tests/test_round7_remainder.py -q`. Expected: pass. Confirm `test_hook_is_silent_on_a_healthy_log` still passes.

- [ ] **Step 5: Commit** `"A stale write is reportable even after the ledger is lost"`.

### Task 14: Health tests do not read this machine

**Files:**
- Modify: `tests/test_health.py:236-240` (`_hook_env`) and `:270-272`
- Modify: `tests/test_verify_filters.py:114-121`; `tests/test_ledger_faults.py:96`; `tests/test_hook_integration.py:27-41`

- [ ] **Step 1: Test** — add to `tests/test_health.py`:

```python
def test_the_hook_env_carries_no_credentials_and_no_registry(tmp_path) -> None:
    env = _hook_env(tmp_path)
    assert not [k for k in env if k.startswith("ZOTERO_") and k != "ZOTERO_CAPTURE_STATE_DIR"]
    assert env["ZOTERO_PROVENANCE_INSTALLED_MANIFEST"].startswith(str(tmp_path))
```

- [ ] **Step 2: Run** — FAIL (session has 4 live `ZOTERO_*` vars; no manifest key).
- [ ] **Step 3: Implement** `_hook_env`: pop every `ZOTERO_*`, set `ZOTERO_PROVENANCE_INSTALLED_MANIFEST` to a manifest written under `tmp_path` pinning `PINNED` (copy the manifest shape from `tests/test_end_to_end.py:130`). Replace the `_installed()` call at :270 with `PINNED`. Apply the same env construction to the three other files (extract `clean_env(state: Path) -> dict` into `tests/conftest.py` and use it in all four).
- [ ] **Step 4: Run** `python -m pytest tests/test_health.py tests/test_verify_filters.py tests/test_ledger_faults.py tests/test_hook_integration.py -q` and then the same under `HOME=$(mktemp -d)`. Both pass.
- [ ] **Step 5: Commit** `"A test must not read the developer's machine"`.

### Task 15: `drain_queue` resolves the state dir from the environment

**Files:** `scripts/drain_queue.py:55`; test `tests/test_drain_queue.py` (create if absent; `ls tests | grep drain`).

- [ ] **Step 1: Test**: run `drain_queue.py --dry-run` (or `--limit 0`) as a subprocess with `ZOTERO_CAPTURE_STATE_DIR=tmp_path` and no `ZOTERO_*` else; assert a `health.db` is NOT created under the real `~/.local/state/zotero-provenance` path — better: assert the script's resolved ledger path (print it under `--dry-run`, or import `main` and monkeypatch `open_incident` to capture `ledger_path`) starts with `tmp_path`.
- [ ] **Step 2: Run** — FAIL (path is `~/.local/state/...`).
- [ ] **Step 3: Implement**: `ledger_path = _state_dir(os.environ) / "health.db"` (add `import os`). Grep the repo for any other `_state_dir({})`: `grep -rn "_state_dir({})" scripts hooks tests` — must be 0 after.
- [ ] **Step 4: Run** file. **Step 5: Commit** `"A drain journals into the state dir it was given"`.

### Task 16: The two capture flags do something

**Files:** `scripts/zotero_capture/cli.py:70,76-80,294`; `tests/test_capture_flags.py` (create).

- [ ] **Step 1: Tests**

```python
import ast
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "scripts" / "zotero_capture" / "cli.py"


def test_every_capture_flag_is_read() -> None:
    """The dead-flag AST guard covered snapshot_pages.py only; the capture CLI
    shipped two flags both hooks pass and nothing reads."""
    tree = ast.parse(CLI.read_text())
    flags = {str(a.value) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "add_argument"
             for a in n.args if isinstance(a, ast.Constant) and str(a.value).startswith("--")}
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "args"}
    dead = sorted(f for f in flags if f.lstrip("-").replace("-", "_") not in used)
    assert dead == [], dead


def test_the_session_id_reaches_the_log(tmp_path, monkeypatch) -> None:
    # same env setup as tests/test_bootstrap_events.py; fake run_capture that
    # records kwargs; assert kwargs["session"] == "sess-1"
    ...
```

- [ ] **Step 2: Run** — first FAILS with `['--message-from-stdin', '--session']`.
- [ ] **Step 3: Implement**: thread `session=args.session` into `run_capture` and into `_emit_log`'s record as `"session"`; make `--message-from-stdin` the explicit source: `text = sys.stdin.read() if args.message_from_stdin or message is None else message` (pass `message_from_stdin=args.message_from_stdin` to `run_capture`). Keep both flags — superseded hooks still pass them.
- [ ] **Step 4: Run** `python -m pytest tests/test_capture_flags.py tests/test_capture.py tests/test_bootstrap_events.py tests/test_end_to_end.py -q`.
- [ ] **Step 5: Commit** `"A flag the hooks pass is read"`.

### Task 17: Dead integrity counters, README count, health-hook forwarding test

- [ ] **M6**: delete `stale_n/stale_newest/stale_roots/unverified_n/unverified_newest` and their two warning blocks from `health.py:261-266, 351-364`; drop the unused `pinned_root` parameter from `evaluate`/`incidents`/`_integrity_kind` ONLY if `grep -rn "pinned_root=" tests scripts` shows no caller depends on it — otherwise leave the parameter and delete only the counters. Run `python -m pytest tests/test_health.py -q`. Commit `"Counters nothing increments are deleted"`.
- [ ] **L7**: add to `tests/test_version.py`:

```python
def test_the_readme_test_count_is_within_ten_percent_of_reality() -> None:
    import re, subprocess, sys
    text = (ROOT / "README.md").read_text()
    m = re.search(r"(\d+) tests run by default", text)
    assert m, "README no longer states a test count"
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                         capture_output=True, text=True, cwd=ROOT, timeout=120).stdout
    n = int(re.search(r"(\d+) tests? collected", out).group(1))
    assert abs(int(m.group(1)) - n) <= n * 0.1, f"README says {m.group(1)}, collected {n}"
```

Run RED (780 vs ~1190), fix README to the collected number, GREEN, commit `"The README's test count is guarded"`.

- [ ] **L8**: in `tests/test_trampoline.py`, add `"session-health.sh"` to `HOOKS` if the parity/forwarding tests generalise (read the test bodies; the health hook takes no stdin URL, so add a separate `test_health_hook_forwards_from_a_superseded_root` that asserts the forwarded root's `session-health.sh` is what runs). Also add ONE non-jq test: with `jq` masked off `PATH`, the capture hook must exit 0 AND write a `missing-dependencies` event, not exit silently (read `zp_tramp_refuse` in `hooks/lib.sh`). RED → GREEN → commit `"A missing jq is reported, not silent"`.

### Task 18: CHANGELOG + version 0.60.0 + release ritual (as Task 7)

---

# Release 0.61.0 — "Words and copies" (M5, L1, L3, L4, L5, L6)

### Task 19: `prefix_agreed` is not stamped when nothing agreed

**Files:** `scripts/zotero_capture/snapshot.py:1425-1432, 650-658`; `tests/test_hash_coverage.py:311` area.

- [ ] **Step 1: Test**: stored complete digest `OLDWHOLE`, hasher returns `NEWPREFIX` incomplete → `verify_outcome == "incomparable"` and `partial_match` NOT incremented; positive control: stored complete `X`, hasher returns prefix that MATCHES the stored prefix-digest → still `prefix_agreed`.
- [ ] **Step 2: Run** — FAIL (`prefix_agreed`).
- [ ] **Step 3: Implement**: add `INCOMPARABLE = "incomparable"  # spans differ; nothing was compared` beside `PREFIX_AGREED`; in the `if not was_truncated and not read.complete:` branch, `conclude(url, INCOMPARABLE)` and count in a new `result.incomparable` (add the field to `VerifyResult` and one report line). Reserve `PREFIX_AGREED` for the branch where a prefix digest actually matched (find it: `grep -n PREFIX_AGREED scripts/zotero_capture/snapshot.py`).
- [ ] **Step 4: Run** `tests/test_hash_coverage.py tests/test_snapshot.py tests/test_stable_digest.py`. **Step 5: Commit** `"Incomparable is not agreed"`.

### Task 20: Corroboration stamps per event and keeps the answering host

**Files:** `scripts/corroborate_github.py:106-118`; its test file (`ls tests | grep -i corrob`).

- [ ] **Step 1: Tests**: (a) two rows downgraded in one run carry DIFFERENT `last_attempt_at` under a ticking clock (inject `clock=` as `snapshot_pages` does; assert reads == rows); (b) a row whose `final_url` was `https://github.com/x` before the downgrade still has it after.
- [ ] **Step 2: Run** — both FAIL.
- [ ] **Step 3: Implement**: read `now` inside the loop from an injected `clock`; pass `final_url=r["final_url"]` (the recorded answer) instead of `""`, and fix the comment: "" means never found out and must not overwrite a host that answered.
- [ ] **Step 4/5**: run, commit `"A downgrade keeps the host that answered"`.

### Task 21: Retire is framed by the journal and stamps per row

**Files:** `scripts/zotero_capture/retire.py:199-235`; `tests/test_destructive_safety.py`.

- [ ] **Step 1: Tests**: (a) `apply_retire` interrupted after the first row (fake zotero raises on the second) leaves an entry in `unfinished_operations(db)`; (b) two retired rows' `retired_at` differ under a ticking clock.
- [ ] **Step 2: Run** — FAIL both.
- [ ] **Step 3: Implement**: wrap the loop in `OperationJournal(db_path, ...)` exactly as `repair.py:234` does (`step` before the trash, `outcome` after); take `stamp = clock()` per row with `clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat()` as a keyword parameter.
- [ ] **Step 4/5**: run the destructive-safety file, commit `"Retire is journalled like every other destructive pass"`.

### Task 22: Merge refusals close their journal step; tags move after the trash

**Files:** `scripts/zotero_capture/repair.py:279-286, 313-323`; `tests/test_repair.py`.

- [ ] **Step 1: Tests**: (a) a merge refused because the survivor has no item leaves NO unfinished step (`unfinished_operations(db) == []`); (b) a merge whose `trash_item` returns False leaves the survivor's tags UNCHANGED (fake zotero records `add_tags` calls; assert none).
- [ ] **Step 2: Run** — FAIL both.
- [ ] **Step 3: Implement**: `journal.outcome(seq, "refused", "...")` before each `continue`; move the `zotero.add_tags(survivor_key, tags)` call to after a successful `trash_item` (the index DELETE that follows already CASes on `zotero_key`).
- [ ] **Step 4/5**: run `tests/test_repair.py`, commit `"A refused merge is closed, and tags move after the trash"`.

### Task 23: Third copies and unescaped JSON

- [ ] **L5 DOI hosts**: add `DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})` to `url_processing.py`; use it at `:633`; in `title_fetcher.py:315-317` build the resolver map from it; in `doi_gate.py:30` derive the regex alternation from it. Test: `test_a_www_doi_malformed_path_is_excluded` (RED: `www.doi.org/10.x` not excluded today). Commit `"One list of DOI hosts"`.
- [ ] **L5 state dir**: the shell copies (`hooks/lib.sh:5` and the trampoline `ZP_LOG`) are documented as deliberately inline; add a test in `tests/test_trampoline.py` that runs `zp_log_path` under `ZOTERO_CAPTURE_STATE_DIR`, `XDG_STATE_HOME`, and neither, and compares each to `config._state_dir(env)` — a parity guard, not a merge. RED impossible (they agree today) → this is the one test allowed to pass first; say so in its docstring and mutate `lib.sh` once by hand to see it fail, record in CHANGELOG.
- [ ] **L6**: in `hooks/lib.sh:78` and `zp_tramp_log`, route the interpolated values through `jq -Rn --arg`/`python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))'` (jq may be absent — use the python form, python3 is already required). Test: a state dir containing `"` in its path yields a line `json.loads` accepts. RED → GREEN → commit `"A path with a quote is still a JSON line"`.

### Task 24: CHANGELOG + version 0.61.0 + release ritual (as Task 7)

- [ ] After all four releases: update `audit_whole_repo_2026-09-06.md` status lines to FIXED with release numbers; add one line to `MEMORY.md`. Report the final live counts (stranded claims 0, site-root rows now reachable, `integrity_check` ok).
