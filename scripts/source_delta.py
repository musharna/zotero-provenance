#!/usr/bin/env python3
"""Print a markdown source-delta report for one `context:` label.

Usage: source_delta.py <context-name> [--since 90d] [--run-started YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zotero_capture.cli import build_client  # noqa: E402
from zotero_capture.config import ConfigError, load_config  # noqa: E402
from zotero_capture.delta import emit_markdown, seen_dates  # noqa: E402


def parse_window(s: str) -> int:
    m = re.fullmatch(r"(\d+)d", s)
    if not m:
        raise SystemExit(f"--since must look like '90d', got {s!r}")
    return int(m.group(1))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("context_name", help="context label, e.g. 'general' or 'lit-review'")
    p.add_argument("--since", default="90d")
    p.add_argument("--run-started", default=date.today().isoformat())
    args = p.parse_args()

    since_days = parse_window(args.since)
    run_started = date.fromisoformat(args.run_started)
    cutoff = run_started - timedelta(days=since_days)

    try:
        config = load_config()
    except ConfigError as e:
        sys.stderr.write(f"zotero-provenance: {e}\n")
        return 1

    with build_client(config) as zotero:
        try:
            items = zotero.query_by_tag(f"context:{args.context_name}")
        except Exception as e:
            sys.stderr.write(f"zotero-provenance: delta query failed: {e}\n")
            return 1
        kept = [i for i in items if any(d >= cutoff for d in seen_dates(i))]
        print(
            emit_markdown(
                kept,
                run_started=run_started,
                context_name=args.context_name,
                since_days=since_days,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
