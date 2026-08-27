"""Command-line entry point for capture and triage modes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

from .capture import CaptureResult, capture_message
from .config import Config, ConfigError, _state_dir, load_config
from .project_slug import derive_slug
from .sqlite_cache import IndexIdentityMismatch, lookup_url, require_identity
from .staleness import installed_version, stale_reason
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


def _emit_bootstrap_event(event: str, detail: str) -> None:
    """Record a failure that happened BEFORE any capture could be attempted.

    Written through `_state_dir`, which resolves without valid credentials —
    which is the whole point, since these are the paths where the credentials
    are what is missing. Previously these were a plaintext stderr line and a
    traceback; the health parser drops anything that is not JSON, so a fresh
    install with no API key failed on every cited URL and reported nothing,
    forever. Best-effort: this must never itself break a turn.
    """
    try:
        log_path = _state_dir(os.environ) / "capture.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "event": event,
                        "detail": detail[:500],
                        "version": __version__,
                    }
                )
                + "\n"
            )
    except Exception as exc:
        # If the reporter of failures fails, say so on stderr — which the hook
        # appends to the log — rather than swallowing it. A silent failure
        # reporter is the same class of bug as everything else this file exists
        # to surface.
        try:
            sys.stderr.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "event": "capture-bootstrap-error",
                        "detail": f"could not record {event}: {exc}",
                    }
                )
                + "\n"
            )
        except Exception:
            pass


def _observed_pinned_root() -> str | None:
    """What the registry pinned at the moment of THIS write, or None.

    Recorded per line so staleness becomes a property of the record, decided
    once by the process that was actually there. Deriving it later cannot work:
    the registry moves, and any upgrade or reinstall would retroactively forgive
    every write that had already been stale when it happened.
    """
    try:
        from .registry import resolve_pinned

        root, _ = resolve_pinned(
            own_root=Path(__file__).resolve().parent.parent.parent,
            registry_path=Path.home()
            / ".claude"
            / "plugins"
            / "installed_plugins.json",
        )
        return str(root) if root else None
    except Exception:  # never let bookkeeping break a capture
        return None


def _surface(env: Mapping[str, str]) -> str:
    """Which surface drove this session: local CLI, bridged, or a cloud session.

    `CLAUDE_CODE_BRIDGE_SESSION_ID` is set on the LOCAL session for as long as a
    Remote Control connection is attached (Claude Code v2.1.199+), because
    Remote Control bridges a session that goes on running locally rather than
    moving it anywhere. `CLAUDE_CODE_REMOTE` is set in remote web environments.

    Checked in that order: a bridged session may plausibly carry both, and the
    interesting fact about it is that it is bridged, not that it is remote.
    """
    if env.get("CLAUDE_CODE_BRIDGE_SESSION_ID"):
        return "bridged"
    if env.get("CLAUDE_CODE_REMOTE") == "true":
        return "remote"
    return "local"


def _emit_log(
    log_path: Path,
    *,
    project: str,
    context: str | None,
    result: CaptureResult,
    latency_ms: int,
    identity: dict[str, str] | None = None,
    pinned_root: str | None = None,
    incident_id: str | None = None,
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
        # Which surface the session was driven from. The README asserted from its
        # first commit that a session bridged with /remote-control fires no local
        # Stop hook, so "nothing is captured in those sessions" -- an assumption
        # that shipped as a documented limitation and was never measured. It is
        # false: Remote Control bridges a session that keeps running locally, and
        # the docs say hooks "run wherever Claude Code runs". It was disproved by
        # a bridged session capturing its own citations.
        #
        # Recorded so the next such claim is a query rather than a belief. Only
        # the class, never the bridge session id.
        "surface": _surface(os.environ),
        "urls_seen": result.urls_seen,
        "urls_new": result.urls_new,
        "urls_recurring": result.urls_recurring,
        "urls_excluded": result.urls_excluded,
        # Identity comes from the writer. Reconstructing it afterwards from
        # timestamp+root aliased distinct incidents that happened in the same
        # second, so acknowledging one silenced another that was never shown.
        # Minted before the capture ran, not here. See run_capture.
        **({"incident_id": incident_id} if incident_id else {}),
        # Observed BEFORE the capture, not after: reading it afterwards let a
        # registry change mid-capture record a pin the write never ran under,
        # fabricating a stale write that never happened (and, reversed, hiding a
        # real one). When it cannot be resolved, say so explicitly — silently
        # omitting the field downgraded a fresh record to the legacy timestamp
        # comparison it was meant to replace.
        **(
            {"pinned_root": pinned_root}
            if pinned_root
            else {"pin_observation": "unknown"}
        ),
        # Only present when the run refused; absent on ordinary captures so the
        # healthy line keeps its existing shape.
        **({"refused": result.refused} if result.refused else {}),
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
    ledger_path: Path,
    identity: dict[str, str] | None = None,
) -> CaptureResult:
    text: str = sys.stdin.read() if message is None else message
    # Read the authorisation state BEFORE the work it authorises.
    pinned_root = _observed_pinned_root()
    # Created BEFORE the work it names. It used to be minted inside _emit_log,
    # which runs after every Zotero write, so a hook timeout could leave a row
    # in the library from a superseded root with no record that it happened.
    incident_id = uuid.uuid4().hex
    running_root = str(Path(__file__).resolve().parent.parent.parent)
    # Required, not defaulted. Handed in by the caller and never read from the
    # environment here: deriving it from log_path.parent put incidents where the
    # checker never looked, and deriving it from os.environ was worse — a
    # function given explicit paths reaching for a sibling meant the test suite
    # wrote real incidents into the developer's live ledger. main() owns the
    # environment and passes both. The fallback that used to sit here made that
    # agreement a convention every caller had to remember; a caller that forgot
    # got a ledger next to the LOG, which is exactly where the checker no longer
    # looks. An invariant a signature can hold should not be left to memory.
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
        incident_id=incident_id,
        pinned_root=pinned_root,
        running_root=running_root,
        ledger_path=ledger_path,
        # Re-read before every mutation, not once for the message: an upgrade
        # landing mid-capture must not be forgiven for the writes that follow it.
        observe_pin=_observed_pinned_root,
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
        pinned_root=pinned_root,
        incident_id=incident_id,
    )
    return result


def run_triage(
    *,
    url: str,
    db_path: Path,
    zotero: ZoteroClient,
    identity: dict[str, str] | None = None,
) -> int:
    """Tag one captured row as triaged.

    Verifies the index identity first. This mutates the library from a row it
    read out of the index, and capture was the only caller that ever checked the
    index was OF that library -- so a configuration pointed at another
    collection tagged the wrong item and reported success.

    And it refuses to write from a superseded root. 0.22.0's trampoline handles
    that structurally by forwarding, but only for roots that CONTAIN the
    trampoline and only when a target can be resolved at all -- and the
    unresolvable case is precisely what staleness.py was kept for as defence in
    depth. `stale_reason` appeared nowhere in this module until now, so the one
    guard the project built for exactly this had never been asked.

    A refusal here is cheap. Unlike capture, nothing is lost by declining: the
    row stays untriaged and a person can run it again from a current session.
    """
    reason = stale_reason(__version__, installed_version())
    if reason:
        sys.stderr.write(f"zotero-provenance: refusing to triage: {reason}\n")
        return 3
    if identity is not None:
        try:
            require_identity(db_path, **identity)
        except IndexIdentityMismatch as e:
            sys.stderr.write(f"zotero-provenance: {e}\n")
            return 2
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
        _emit_bootstrap_event("configuration-error", str(e))
        return 0 if args.triage is None else 1

    db_path = Path(args.db_path) if args.db_path else config.db_path
    log_path = Path(args.log_path) if args.log_path else config.log_path
    project = args.project or derive_slug(args.cwd)

    try:
        with build_client(config) as zotero:
            if args.triage:
                return run_triage(
                    url=args.triage,
                    db_path=db_path,
                    zotero=zotero,
                    identity={
                        "api_origin": api_base(),
                        "library_type": config.library_type,
                        "library_id": config.library_id,
                        "collection_key": config.collection_key,
                    },
                )
            run_capture(
                ledger_path=_state_dir(os.environ) / "health.db",
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
    except Exception as e:
        logging.exception("zotero-provenance capture failed")
        _emit_bootstrap_event("capture-bootstrap-error", f"{type(e).__name__}: {e}")
        return 0 if args.triage is None else 1


if __name__ == "__main__":
    sys.exit(main())
