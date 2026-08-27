#!/usr/bin/env python3
"""Measure what fraction of cited URLs actually reached the library.

"Capture works" was a happy-path claim: the tests prove a URL in a message ends
up in Zotero, and the health check proves the hook ran. Neither answers the
question a provenance tool lives or dies on -- of everything the assistant
actually cited, how much is in the library?

Two numbers, because they mean different things:

  * COVERAGE of eligible citations. The Stop hook is handed
    `last_assistant_message` and captures that. So a URL in the final message of
    a turn is eligible, and if an eligible URL is missing, capture is BROKEN.

  * The STRUCTURAL GAP. A turn is not one message. An agentic turn emits many
    assistant messages around tool calls, and only the last one is ever offered
    to the hook, so a URL cited mid-turn is never seen. Nothing is broken when
    one of those is missing -- the design cannot see it. That is a different
    finding from a bug, and conflating the two would either hide a real failure
    or invent one.

Three rules make the number mean something, the same three that
dev/measure_extraction.py runs on:

  * a CONTROL that must fail. `--control` looks every URL up in an empty decoy
    index, where coverage MUST collapse. If it does not, the lookup is not
    connected to anything and the headline number is worthless.
  * the SAME pipeline capture uses -- `extract_urls`, `canonicalize`,
    `is_excluded`, and the generated-report guard -- because a reimplementation
    measures the reimplementation. Excluded URLs are not misses: capture
    declined them on purpose.
  * a WINDOW. The index only holds what was captured after the plugin was
    installed, so a message older than the earliest row is not evidence of
    anything. Defaults to the index's own earliest row.

    python3 dev/measure_coverage.py                    # measure
    python3 dev/measure_coverage.py --control          # prove it can fail
    python3 dev/measure_coverage.py --since 2026-08-01 --max-bytes 2000

The byte budget is REPORTED, never silent: a few transcripts run to hundreds of
megabytes, and a harness that quietly skipped them would read as "I looked at
everything" when it had not.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from zotero_capture import url_processing as up  # noqa: E402
from zotero_capture.capture import _is_generated_report  # noqa: E402
from zotero_capture.config import _state_dir  # noqa: E402
from zotero_capture.sqlite_cache import lookup_url  # noqa: E402


def _is_real_user_message(line: str) -> bool:
    """Whether a `type: "user"` record is a person speaking, or a tool result.

    Claude Code records tool results as user records. Treating those as turn
    boundaries put every assistant message at the end of its own turn, which
    made every message look eligible and reported the structural gap as exactly
    zero -- in a corpus of agentic sessions, where mid-turn citation is the norm.
    An implausible zero is the tell; the first run produced one.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return False
    if record.get("type") != "user" or record.get("toolUseResult") is not None:
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return any(
        isinstance(block, dict) and block.get("type") == "text"
        for block in content or []
    )


def _turns(path: pathlib.Path):
    """Yield each turn's assistant text blocks, in order, oldest turn first.

    A turn is the run of assistant messages between two user messages. Sidechain
    records are subagent traffic on a separate spine; the Stop hook never sees
    them, so counting them would manufacture a gap the design never had.
    """
    turn: list[tuple[str, str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if (
                    '"type":"assistant"' not in line
                    and '"type": "assistant"' not in line
                ):
                    if '"type":"user"' in line or '"type": "user"' in line:
                        if turn and _is_real_user_message(line):
                            yield turn
                            turn = []
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("isSidechain"):
                    continue
                if record.get("type") != "assistant":
                    continue
                text = "\n".join(
                    block.get("text") or ""
                    for block in (record.get("message") or {}).get("content") or []
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
                if text:
                    turn.append((text, record.get("timestamp") or ""))
    except OSError as exc:
        print(f"skipped {path}: {exc}", file=sys.stderr)
        return
    if turn:
        yield turn


def _eligible_urls(text: str) -> list[str]:
    """Exactly what capture would store for this message, and nothing else."""
    if _is_generated_report(text, "assistant"):
        return []
    out, seen = [], set()
    for raw in up.extract_urls(text):
        canonical = up.canonicalize(raw)
        if up.is_excluded(canonical) or canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def _empty_index() -> pathlib.Path:
    """A decoy index with the real schema and no rows, for --control."""
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    path = pathlib.Path(handle.name)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE url_index (url_canonical TEXT PRIMARY KEY,"
        " zotero_key TEXT NOT NULL, first_seen TEXT NOT NULL,"
        " last_seen TEXT NOT NULL, pending_key TEXT NOT NULL DEFAULT '',"
        " claimed_at TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--projects",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".claude" / "projects",
    )
    parser.add_argument("--db", type=pathlib.Path, default=None)
    parser.add_argument(
        "--since", default=None, help="ISO date; defaults to the index's earliest row"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=500,
        help="budget in MB across transcripts, newest first",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="look up in an empty index; coverage must collapse",
    )
    parser.add_argument("--show", type=int, default=8, help="example misses to print")
    args = parser.parse_args()

    real_db = args.db or (_state_dir({}) / "url_index.db")
    if not real_db.exists():
        print(f"no index at {real_db}", file=sys.stderr)
        return 2

    with sqlite3.connect(real_db) as conn:
        earliest = conn.execute("SELECT MIN(first_seen) FROM url_index").fetchone()[0]
    since = args.since or earliest
    if not since:
        print("the index is empty; nothing to measure against", file=sys.stderr)
        return 2

    db = _empty_index() if args.control else real_db

    transcripts = sorted(
        args.projects.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    budget = args.max_bytes * 1024 * 1024
    used, read, skipped = 0, [], 0
    for path in transcripts:
        size = path.stat().st_size
        if used + size > budget:
            skipped += 1
            continue
        used += size
        read.append(path)

    final_urls: dict[str, str] = {}
    mid_urls: dict[str, str] = {}
    turns = 0
    for path in read:
        for turn in _turns(path):
            turns += 1
            for index, (text, stamp) in enumerate(turn):
                if stamp[:10] < since:
                    continue
                bucket = final_urls if index == len(turn) - 1 else mid_urls
                for url in _eligible_urls(text):
                    bucket.setdefault(url, stamp)

    # A URL cited mid-turn AND in a final message is eligible: it had its chance.
    for url in final_urls:
        mid_urls.pop(url, None)

    present = [u for u in final_urls if lookup_url(db, u)]
    missing = [u for u in final_urls if u not in present]
    mid_present = [u for u in mid_urls if lookup_url(db, u)]

    total = len(final_urls)
    coverage = (len(present) / total * 100) if total else 0.0

    print(f"window        since {since}")
    print(
        f"transcripts   {len(read)} read, {skipped} skipped for the "
        f"{args.max_bytes} MB budget ({used / 1048576:.0f} MB read)"
    )
    print(f"turns         {turns}")
    print(f"index         {db}{'  [CONTROL: empty decoy]' if args.control else ''}")
    print()
    print(f"eligible URLs (final message of a turn)   {total}")
    print(f"  present in the library                  {len(present)}")
    print(f"  MISSING                                 {len(missing)}")
    print(f"  coverage                                {coverage:.1f}%")
    print()
    print(f"cited only mid-turn (design cannot see)   {len(mid_urls)}")
    print(f"  of those, present anyway                {len(mid_present)}")

    if missing and args.show:
        print(f"\nexamples of eligible URLs that are missing (up to {args.show}):")
        for url in sorted(missing)[: args.show]:
            print(f"  {final_urls[url][:19]}  {url[:96]}")

    if args.control:
        if present:
            print(
                "\nCONTROL FAILED: an empty index reported URLs as present.",
                file=sys.stderr,
            )
            print(
                "The lookup is not reading the index it claims to read, so a "
                "high coverage number from this harness means nothing.",
                file=sys.stderr,
            )
            return 2
        print(
            "\ncontrol ok: against an empty index nothing is found, so the "
            "lookup is genuinely reading the index."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
