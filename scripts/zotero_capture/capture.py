"""Top-level capture orchestrator — wires extract -> exclude -> dedup -> POST/PATCH."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .health_ledger import mutation_id, open_incident
from .staleness import installed_version, stale_reason
from .sqlite_cache import (
    IndexIdentityMismatch,
    bind_identity,
    drop_row,
    init_db,
    new_zotero_key,
    queue_pending_tags,
    lookup_url,
    release_url,
    reserve_url,
    set_zotero_key,
    clear_pending_tags,
    peek_pending_tags,
    update_last_seen,
)
from .url_processing import (
    NO_CAPTURE_MARKER,
    canonicalize,
    extract_urls,
    is_excluded,
)
from .zotero_client import ItemGone, UNRESOLVED_TITLE_TAG, ZoteroClient, ZoteroError

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT = "general"

# Most title re-fetches to attempt in a single capture run. Each costs one live
# HTTP request inside the Stop hook's few-second budget; unspent retries simply
# happen on a later run, since an unresolved title stays marked until it resolves.
MAX_REENRICH_PER_RUN = 3

# How long a reservation must sit untouched before another run may question it.
# Comfortably beyond the hook's own 10s timeout, so a POST that is merely slow is
# never mistaken for one that was abandoned.
STALE_CLAIM_S = 60.0


@dataclass
class CaptureFailure:
    url: str
    code: str  # "zotero_error" or "unexpected"
    message: str


@dataclass
class CaptureResult:
    urls_seen: int = 0
    urls_new: int = 0
    urls_recurring: int = 0
    urls_excluded: int = 0
    errors: list[CaptureFailure] = field(default_factory=list)
    # Why this run wrote nothing, when the reason was a deliberate refusal
    # rather than an absence of URLs. Without it a refusal is byte-identical to
    # a healthy message that cited nothing, which is how the health check came
    # to count the plugin's most deliberate failures as successes.
    refused: str | None = None


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is_generated_report(message: str, origin: str) -> bool:
    """True for one of this plugin's own reports, which must not be re-captured.

    Two restrictions, both because the marker is a public string that anyone can
    type. It is honoured only in assistant output — a user prompt is not a
    generated report, and honouring it there let anyone silence capture for a
    whole message by quoting it. And it must LEAD the message, the way
    emit_markdown writes it, so that merely discussing the marker does not
    suppress the citations in the same turn.

    This is the backup layer. The structural rule — a report shows each URL in a
    code span, which extraction reads as displayed rather than cited — is what
    normally does the work.
    """
    return origin == "assistant" and message.lstrip().startswith(NO_CAPTURE_MARKER)


def _resolve_claim(
    row: dict,
    *,
    url: str,
    db_path: Path,
    zotero: ZoteroClient,
    now: datetime,
) -> dict | None:
    """Settle a reservation that never completed. None means "claim it afresh".

    An abandoned claim is ambiguous on its face — the POST may have committed
    before the response was lost — so a timeout alone cannot decide it. Because
    the key was chosen before the request went out, Zotero can be asked directly:
    if the item is there the claim completes, and if it is not the claim is
    released so this run can retry.

    A claim younger than STALE_CLAIM_S is left strictly alone. That is another
    session's POST still in flight, well inside the hook's own 10s budget, and
    questioning it is how two items get created for one URL.
    """
    claimed_at = row.get("claimed_at") or ""
    pending_key = row.get("pending_key") or ""
    if not claimed_at or not pending_key:
        # Predates the recoverable protocol; nothing to ask Zotero about.
        return row
    try:
        age = (now - datetime.fromisoformat(claimed_at)).total_seconds()
    except ValueError:
        return row
    if age < STALE_CLAIM_S:
        return row
    if zotero.item_exists(pending_key):
        logger.info("recovered %s: item %s exists, completing claim", url, pending_key)
        set_zotero_key(db_path, url, pending_key, pending_key=pending_key)
        return lookup_url(db_path, url)
    logger.info("releasing stale claim on %s: %s was never created", url, pending_key)
    # Scoped to the key we just asked Zotero about. Between the lookup and here
    # another session may have taken the claim over, and releasing by URL alone
    # would delete a live reservation on the strength of a stale reading.
    release_url(db_path, url, pending_key=pending_key)
    return None


def _flush_pending(db_path: Path, url: str, key: str, zotero) -> None:
    """Apply any tags another session queued against this URL, then clear them.

    Peek, write, THEN clear — a destructive read would lose the sighting to a
    transient error, which is the same defect this pass fixed in the recurring
    branch.
    """
    queued = peek_pending_tags(db_path, url)
    if not queued:
        return
    zotero.add_tags(key, queued)
    clear_pending_tags(db_path, url, queued)


def _incident_kind(running_root: str | None, pinned_root: str | None) -> str | None:
    """Is a write from here an integrity incident, decided BEFORE the write?

    None for a healthy capture, so a correct install never accumulates ledger
    rows for ordinary work.
    """
    if not running_root:
        return None
    if pinned_root is None:
        return "unverified"
    return "stale" if running_root != pinned_root else None


def _record_intent(
    *,
    ledger_path,
    incident_id,
    url,
    running_root,
    pinned_root,
    ts,
) -> None:
    """Write the incident BEFORE the mutation it describes.

    An intent for a mutation that never happens is a false positive someone can
    close in one command. A mutation with no intent is corruption nobody can
    find.

    So this does NOT swallow failures. Letting the write proceed when the
    incident could not be recorded — a read-only state directory, a full disk, a
    permission error — reproduces exactly the condition this journal exists to
    prevent, and does so silently. The exception is caught by the per-URL
    handler, which releases the reservation and records the failure, so the
    citation is skipped rather than written blind. A skipped citation is
    recoverable; an unrecorded mutation is not.

    A healthy capture opens no incident and never reaches the write below, so a
    broken ledger cannot stop ordinary work.

    `incident_id` names the CAPTURE. The row names ONE write of ONE url, and
    keying it on the capture collapsed every URL in a message onto a single row
    -- keeping the first and discarding the rest -- because the id is the
    table's primary key and the insert says DO NOTHING on conflict. The row
    carries its own derived id; the capture id stays in the log line, which is
    the thing that really is per capture.
    """
    kind = _incident_kind(running_root, pinned_root)
    if not (kind and ledger_path and incident_id and running_root):
        return
    open_incident(
        ledger_path,
        incident_id=mutation_id(incident_id, url),
        url=url,
        root=running_root,
        pinned_root=pinned_root,
        kind=kind,
        ts=ts,
    )


def capture_message(
    *,
    message: str,
    project_slug: str,
    context: str | None,
    today: date,
    db_path: Path,
    zotero: ZoteroClient,
    title_fetcher: Callable[[str], str],
    origin: str = "assistant",
    now: datetime | None = None,
    identity: dict[str, str] | None = None,
    incident_id: str | None = None,
    pinned_root: str | None = None,
    running_root: str | None = None,
    ledger_path: Path | None = None,
) -> CaptureResult:
    """Process one message: extract URLs, then create or re-tag each in Zotero."""
    result = CaptureResult()
    # Before anything is written: is this root even allowed to write? A session
    # keeps the plugin version it resolved at its own start, and an old one
    # applies rules that have since been corrected — v0.3.0 put eight URLs into
    # the collection on 2026-08-23 that every release since v0.8 would refuse.
    reason = stale_reason(__version__, installed_version())
    if reason:
        logger.error("%s", reason)
        result.refused = reason
        return result
    init_db(db_path)
    # Before any row is read or written: is this index an index of the library
    # we are about to write to? Keyed by URL alone, it cannot tell otherwise,
    # and pointing it at a different collection splits the sources in half while
    # reporting success for both.
    if identity is not None:
        try:
            bind_identity(db_path, **identity)
        except IndexIdentityMismatch as e:
            logger.error("%s", e)
            result.refused = str(e)
            return result
    now = now or datetime.now(timezone.utc)
    if _is_generated_report(message, origin):
        return result
    raw_urls = extract_urls(message)
    canonicals: list[str] = []
    seen_canonicals: set[str] = set()
    for raw in raw_urls:
        c = canonicalize(raw)
        if is_excluded(c):
            result.urls_excluded += 1
            continue
        if c in seen_canonicals:
            continue
        seen_canonicals.add(c)
        canonicals.append(c)
    result.urls_seen = len(canonicals)

    today_iso = today.isoformat()
    seen_tag = f"seen:{today_iso}"
    context_tag = f"context:{context or DEFAULT_CONTEXT}"
    project_tag = f"project:{project_slug}"

    # Re-enrichment spends a live HTTP fetch, so bound it per run: the Stop hook
    # has seconds, and a message can cite many permanently-unfetchable URLs.
    reenrich_budget = [MAX_REENRICH_PER_RUN]

    def make_title_resolver(url: str) -> Callable[[], str]:
        """Resolve this URL's title, but only while the per-run budget lasts.

        Returning the URL means "still unresolved", so an exhausted budget simply
        defers the retry to a later run rather than spending hook time now.
        """

        def resolve() -> str:
            if reenrich_budget[0] <= 0:
                return url
            reenrich_budget[0] -= 1
            return title_fetcher(url)

        return resolve

    # Note: result.errors may include URLs already counted in urls_new/urls_recurring
    # (the Zotero call succeeded but the local SQLite write failed).
    for url in canonicals:
        try:
            existing = lookup_url(db_path, url)
            if existing is not None and not existing["zotero_key"]:
                existing = _resolve_claim(
                    existing, url=url, db_path=db_path, zotero=zotero, now=now
                )
            if existing is None:
                pending_key = new_zotero_key()
                if reserve_url(db_path, url, today, pending_key=pending_key, now=now):
                    # Claimed before the network call, so a second session cannot
                    # also decide this URL is new while the POST is in flight and
                    # create a duplicate item that dedup could never see again.
                    issued = False
                    try:
                        title = title_fetcher(url)
                        tags = [
                            context_tag,
                            project_tag,
                            seen_tag,
                            f"domain:{_domain(url)}",
                        ]
                        if title == url:
                            tags.append(UNRESOLVED_TITLE_TAG)
                        issued = True
                        _record_intent(
                            ledger_path=ledger_path,
                            incident_id=incident_id,
                            url=url,
                            running_root=running_root,
                            pinned_root=pinned_root,
                            ts=today_iso,
                        )
                        key = zotero.post_webpage_item(
                            url_canonical=url,
                            title=title,
                            access_date=today_iso,
                            tags=tags,
                            item_key=pending_key,
                        )
                    except BaseException:
                        # Release only while it is certain nothing was created.
                        # Once the request has gone out, a failure says nothing
                        # about whether Zotero committed it, and releasing then
                        # is what produced duplicates: the claim stays, and
                        # _resolve_claim settles it later by asking Zotero.
                        if not issued:
                            release_url(db_path, url, pending_key=pending_key)
                        raise
                    if not set_zotero_key(db_path, url, key, pending_key=pending_key):
                        # set_zotero_key is a compare-and-swap and its result
                        # was thrown away. It returns False exactly when the
                        # claim is no longer ours -- taken over, or the row
                        # deleted underneath us by a retire pass -- which means
                        # the item now exists in Zotero with nothing in the
                        # index pointing at it. Dedup will never see it again,
                        # so every future citation of this URL makes another
                        # copy. Silence here was the one moment the system could
                        # have noticed.
                        logger.error(
                            "item %s was created for %s but no index row claims "
                            "it; the claim was lost while the POST was in flight",
                            key,
                            url,
                        )
                        result.errors.append(
                            CaptureFailure(
                                url=url,
                                code="claim_lost",
                                message=(
                                    f"item {key} exists in Zotero but no index "
                                    f"row claims it"
                                ),
                            )
                        )
                    # Another session may have queued its own sighting against
                    # this claim while the POST was in flight. Nothing else will
                    # ever come back for it: the recurring branch only runs on a
                    # LATER citation, and a URL cited once, simultaneously, by
                    # two sessions would silently lose one session's record.
                    _flush_pending(db_path, url, key, zotero)
                    result.urls_new += 1
                    continue
                existing = lookup_url(db_path, url)

            key = (existing or {}).get("zotero_key") or ""
            if not key:
                # Another session holds the claim and its POST has not landed
                # yet. There is no item to tag, and creating one is the very
                # duplicate the claim exists to prevent — but the sighting is
                # real, so queue its provenance for whoever completes the item.
                logger.debug("%s is claimed by another session; deferring", url)
                queue_pending_tags(db_path, url, [seen_tag, context_tag, project_tag])
                continue
            # Peek, write, THEN clear. take_pending_tags() commits its DELETE
            # before add_tags is even called, so a transient Zotero error used
            # to destroy the very sighting queue_pending_tags exists to keep.
            # Applying a tag twice is harmless -- add_tags is idempotent -- so
            # at-least-once is the right trade for provenance.
            queued = peek_pending_tags(db_path, url)
            _record_intent(
                ledger_path=ledger_path,
                incident_id=incident_id,
                url=url,
                running_root=running_root,
                pinned_root=pinned_root,
                ts=today_iso,
            )
            zotero.add_tags(
                key,
                [seen_tag, context_tag, project_tag, *queued],
                title_resolver=make_title_resolver(url),
            )
            clear_pending_tags(db_path, url, queued)
            result.urls_recurring += 1
            update_last_seen(db_path, url, today)
        except ItemGone as e:
            # A person trashed or deleted the item. The row now points at
            # nothing, and leaving it there made every future citation of this
            # URL repeat the same 404 forever — the source silently stopped
            # being recorded, with no way to notice.
            #
            # The row is dropped rather than tombstoned, so the index says only
            # what the library actually holds. The cost is honest and worth
            # stating: citing that URL again recreates the item. Someone who
            # wants a source gone for good should exclude its host, not rely on
            # a deletion that capture is designed to undo.
            logger.warning("item for %s is gone (%s); dropping the stale row", url, e)
            drop_row(db_path, url)
            result.errors.append(
                CaptureFailure(url=url, code="item_gone", message=str(e))
            )
        except ZoteroError as e:
            logger.error("Zotero API error for %s: %s", url, e)
            result.errors.append(
                CaptureFailure(url=url, code="zotero_error", message=str(e))
            )
        except Exception as e:
            logger.error("Unexpected error for %s: %s", url, e)
            result.errors.append(
                CaptureFailure(url=url, code="unexpected", message=str(e))
            )
    return result
