"""Command-line entry point for capture and triage modes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from .capture import CaptureResult, capture_message
from .config import Config, ConfigError, load_config
from .project_slug import derive_slug
from .retry_queue import drain_queue
from .sqlite_cache import lookup_url
from .title_fetcher import fetch_title
from .url_processing import canonicalize
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zotero-capture")
    p.add_argument(
        "--cwd",
        default=None,
        help="session working directory; the project slug is derived from it",
    )
    p.add_argument(
        "--project",
        default=None,
        help="explicit project slug, overriding derivation from --cwd",
    )
    p.add_argument("--session", default="", help="session id (passed by the hook)")
    p.add_argument(
        "--context",
        default=None,
        help="context label for the `context:` tag (default: general)",
    )
    p.add_argument(
        "--message-from-stdin",
        action="store_true",
        help="read the message text from stdin",
    )
    p.add_argument("--message", default=None, help="message text inline (testing)")
    p.add_argument(
        "--triage",
        default=None,
        metavar="URL",
        help="add the `triaged` tag to an already-captured URL",
    )
    p.add_argument("--db-path", default=None)
    p.add_argument("--queue-path", default=None)
    p.add_argument("--log-path", default=None)
    return p


def build_client(config: Config) -> ZoteroClient:
    return ZoteroClient(
        api_key=config.api_key,
        library_id=config.library_id,
        library_type=config.library_type,
        web_sources_collection_key=config.collection_key,
    )


def _retry_handler(entry: dict, zotero: ZoteroClient, db_path: Path) -> bool:
    """Placeholder: retry is not implemented, so queued entries are always kept.

    Nothing enqueues today (see run_capture), so this never drops data. It exists
    so a future retry implementation has one obvious place to land.
    """
    return False


def _emit_log(
    log_path: Path,
    *,
    project: str,
    context: str | None,
    result: CaptureResult,
    latency_ms: int,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project": project,
        "context": context,
        "urls_seen": result.urls_seen,
        "urls_new": result.urls_new,
        "urls_recurring": result.urls_recurring,
        "urls_excluded": result.urls_excluded,
        "latency_ms": latency_ms,
        "errors": [
            {"url": e.url, "code": e.code, "message": e.message} for e in result.errors
        ],
    }
    with log_path.open("a") as fh:
        fh.write(json.dumps(obj) + "\n")


def run_capture(
    *,
    message: str | None,
    project: str,
    context: str | None,
    today: date,
    db_path: Path,
    zotero: ZoteroClient,
    title_fetcher: Callable[..., str],
    log_path: Path,
    retry_queue_path: Path,
) -> CaptureResult:
    text: str = sys.stdin.read() if message is None else message
    started = time.monotonic()
    drain_queue(retry_queue_path, lambda e: _retry_handler(e, zotero, db_path))
    result = capture_message(
        message=text,
        project_slug=project,
        context=context,
        today=today,
        db_path=db_path,
        zotero=zotero,
        title_fetcher=title_fetcher,
    )
    # Failures are NOT enqueued: _retry_handler is a stub that never drains, so
    # enqueuing would grow the file forever. Errors are surfaced in the log below.
    latency_ms = int((time.monotonic() - started) * 1000)
    _emit_log(
        log_path,
        project=project,
        context=context,
        result=result,
        latency_ms=latency_ms,
    )
    return result


def run_triage(*, url: str, db_path: Path, zotero: ZoteroClient) -> int:
    canon = canonicalize(url)
    row = lookup_url(db_path, canon)
    if row is None:
        sys.stderr.write(f"URL not found in capture cache: {canon}\n")
        return 2
    zotero.add_tags(row["zotero_key"], ["triaged"])
    return 0


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("ZOTERO_CAPTURE_DISABLE") == "1":
        return 0
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as e:
        # Loud on stderr (the hook appends stderr to the capture log) but never
        # non-zero from a hook path: a misconfigured plugin must not block a turn.
        sys.stderr.write(f"zotero-provenance: {e}\n")
        return 0 if args.triage is None else 1

    db_path = Path(args.db_path) if args.db_path else config.db_path
    queue_path = Path(args.queue_path) if args.queue_path else config.queue_path
    log_path = Path(args.log_path) if args.log_path else config.log_path
    project = args.project or derive_slug(args.cwd)

    try:
        with build_client(config) as zotero:
            if args.triage:
                return run_triage(url=args.triage, db_path=db_path, zotero=zotero)
            run_capture(
                message=args.message,
                project=project,
                context=args.context,
                today=date.today(),
                db_path=db_path,
                zotero=zotero,
                title_fetcher=fetch_title,
                log_path=log_path,
                retry_queue_path=queue_path,
            )
        return 0
    except Exception:
        logging.exception("zotero-provenance capture failed")
        return 0 if args.triage is None else 1


if __name__ == "__main__":
    sys.exit(main())
