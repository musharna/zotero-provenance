"""Command-line entry point for capture and triage modes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from .capture import CaptureResult, capture_message
from .config import Config, ConfigError, load_config
from .project_slug import derive_slug
from .sqlite_cache import lookup_url
from .title_fetcher import fetch_title
from .url_processing import canonicalize
from . import __version__
from .zotero_client import api_base, ZoteroClient

logger = logging.getLogger(__name__)


class HookTerminated(BaseException):
    """The hook's `timeout` fired. Raised so the claim can be released.

    Derives from BaseException, not Exception, deliberately: capture's per-URL
    loop swallows Exception into a CaptureFailure and carries on, which is the
    wrong response to being killed. Only the `except BaseException` that
    releases the reservation should see this, and then it re-raises.
    """


def _on_terminate(signum: int, _frame: object) -> None:
    raise HookTerminated(f"terminated by signal {signum}")


def install_termination_handler() -> None:
    """Turn SIGTERM into an exception so cleanup runs before the process dies.

    hooks/capture-stop.sh runs capture under `timeout 10`, and GNU timeout sends
    SIGTERM. Python leaves SIGTERM at its default disposition, which kills the
    process outright without unwinding — so a URL claimed just before a slow
    title fetch stayed claimed forever, and every later sighting skipped it as
    "held by another session". SIGKILL cannot be handled and is not what
    `timeout` sends.
    """
    signal.signal(signal.SIGTERM, _on_terminate)


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
        "--origin",
        choices=("assistant", "user"),
        default="assistant",
        help="who wrote the message; only assistant output may carry the "
        "generated-report marker",
    )
    p.add_argument(
        "--triage",
        default=None,
        metavar="URL",
        help="add the `triaged` tag to an already-captured URL",
    )
    p.add_argument("--db-path", default=None)
    p.add_argument("--log-path", default=None)
    return p


def build_client(config: Config, *, timeout: float | None = None) -> ZoteroClient:
    """Build a client. `timeout` overrides the interactive default.

    The hook's budget is tight on purpose, but an unattended pass that pages
    thousands of items should wait on a slow response rather than abandon the
    sweep partway through.
    """
    kwargs = {} if timeout is None else {"timeout": timeout}
    return ZoteroClient(
        api_key=config.api_key,
        library_id=config.library_id,
        library_type=config.library_type,
        web_sources_collection_key=config.collection_key,
        **kwargs,
    )


def _emit_log(
    log_path: Path,
    *,
    project: str,
    context: str | None,
    result: CaptureResult,
    latency_ms: int,
    identity: dict[str, str] | None = None,
) -> None:
    """Append one JSON line per capture, including WHO captured.

    The runtime fields are here because of what it cost to be without them. A
    session ran plugin v0.3.0 for weeks, writing rows that every release since
    v0.8 refuses, and the log said `urls_new: 1, errors: []` every time —
    perfectly true, and useless. Nothing recorded which code produced the line
    or which index and library it wrote to, so the only way to find it was to
    replay a URL through nine cached versions and see which one accepted it.

    With `version` and `root` on every line the same question is one grep.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": __version__,
        "root": str(Path(__file__).resolve().parent.parent.parent),
        "project": project,
        "context": context,
        "urls_seen": result.urls_seen,
        "urls_new": result.urls_new,
        "urls_recurring": result.urls_recurring,
        "urls_excluded": result.urls_excluded,
        "latency_ms": latency_ms,
        "library": (identity or {}).get("library_id", ""),
        "collection": (identity or {}).get("collection_key", ""),
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
    origin: str = "assistant",
    identity: dict[str, str] | None = None,
) -> CaptureResult:
    text: str = sys.stdin.read() if message is None else message
    started = time.monotonic()
    result = capture_message(
        message=text,
        project_slug=project,
        context=context,
        today=today,
        db_path=db_path,
        zotero=zotero,
        title_fetcher=title_fetcher,
        origin=origin,
        identity=identity,
    )
    # Failures are NOT enqueued: _retry_handler is a stub that never drains, so
    # enqueuing would grow the file forever. Errors are surfaced in the log below.
    latency_ms = int((time.monotonic() - started) * 1000)
    _emit_log(
        log_path,
        project=project,
        context=context,
        identity=identity,
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
    install_termination_handler()
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as e:
        # Loud on stderr (the hook appends stderr to the capture log) but never
        # non-zero from a hook path: a misconfigured plugin must not block a turn.
        sys.stderr.write(f"zotero-provenance: {e}\n")
        return 0 if args.triage is None else 1

    db_path = Path(args.db_path) if args.db_path else config.db_path
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
                origin=args.origin,
                identity={
                    "api_origin": api_base(),
                    "library_type": config.library_type,
                    "library_id": config.library_id,
                    "collection_key": config.collection_key,
                },
            )
        return 0
    except Exception:
        logging.exception("zotero-provenance capture failed")
        return 0 if args.triage is None else 1


if __name__ == "__main__":
    sys.exit(main())
