"""Retire index rows that can never be a source, and the items behind them.

Repair and retirement are different verbs and they need different predicates.
`repair` corrects a URL that was damaged on the way in — there is a right answer
and it recovers it. Retirement is for a row where there is no right answer. The
repair pass deliberately SKIPS these rather than guessing a correction, which is
right, but skipping leaves them in the library for good.

Retirement asks repair first. That is not merely ordering: "https://cloud.r-
project.org\\" fails the address test outright — a backslash is not a hostname
character — yet the citation behind it is recoverable, and judging it alone
would have trashed a real source for a reason that reads convincingly in a log.

Two tiers, because they are two different claims:

  HARD    proof the text cannot be an address at all. A template placeholder, a
          control byte, a name the standards reserve, a host no resolver could
          look up. Applied by default.

  POLICY  a real address this collection chooses not to keep: a page asset, a
          font CDN or DoH endpoint, an intranet or private name. These CAN
          resolve — for whoever is on that network — so retiring them is a
          product decision rather than a fact, and it needs --policy. An
          external audit drew this distinction and it was a fair one: declining
          to capture something going forward is a far weaker claim than reaching
          back and trashing what is already stored.

What "reversible" covers, precisely. Trashing is `deleted: 1`, recoverable from
any Zotero client. Dropping the index row is NOT recoverable that way — Zotero's
trash does not hold `first_seen`, `last_seen` or queued provenance tags — so
every applied run writes the rows it removed to a JSONL journal beside the index
before destroying anything. Earlier wording here claimed the whole operation was
reversible from any client. Only its Zotero half is.

Everything else is left alone, including rows that merely look odd. A wildcard
like "https://lepanthes.example/lepanthes*.htm" is not retired for its asterisk:
"*" is a legal sub-delimiter, and a rule keyed on it would take real URLs with
it. Such rows qualify, if at all, on the address test instead.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .repair import repaired_url
from .url_processing import (
    EXCLUDE_HOSTS_EXACT,
    EXCLUDE_INFRA_HOSTS,
    TS_NET_SUFFIX,
    _is_asset_path,
    _is_real_hostname,
    _is_reserved_name,
    is_unsafe_address,
    parse_ip_literal,
)
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

# A brace never appears unencoded in a real address, so it means a template.
# httpx would percent-encode it into something fetchable, which is exactly the
# danger rather than a reason to keep it: "…/download/{ID}.pdb" becomes a URL
# that resolves to a directory nobody cited.
PLACEHOLDER_RE = re.compile(r"[{}]")

# C0, DEL and C1. An ESC arrives as the head of an ANSI sequence.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

HARD = "hard"
POLICY = "policy"

# The reserved-name set splits in two, and the tiers are exactly why that
# matters. HARD asserts "proof the text cannot be an address at all" -- but
# these five resolve perfectly well for whoever is on the right network:
# localhost is guaranteed to reach loopback, .local answers over mDNS, .onion
# answers through Tor, .internal is a private delegation, and .arpa resolves
# but is never a document. Filing them as PROOF meant a bare `--apply`, with no
# `--policy` flag, reached back and trashed a source that was real to the person
# who cited it. The set is derived from the tier's own definition rather than
# from the names anyone happened to notice.
#
# `_is_reserved_name` itself is deliberately untouched: capture uses it to keep
# another project's fixture URLs out of the library, and that exclusion is right
# for every one of these names. Only the DESTRUCTIVE tier assignment was wrong.
RESOLVES_IN_CONTEXT = frozenset({"localhost", "local", "onion", "internal", "arpa"})


def classify(url: str) -> tuple[str, str]:
    """Return (tier, reason) for a row, or ("", "") to leave it alone."""
    if repaired_url(url):
        return "", ""  # repair has a real address to recover; not ours to judge
    if PLACEHOLDER_RE.search(url):
        return HARD, "template placeholder, not an address"
    if CONTROL_RE.search(url):
        return HARD, "control character, from pasted terminal output"

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return HARD, "no host at all"
    if host.rpartition(".")[2] in RESOLVES_IN_CONTEXT:
        return POLICY, "resolves only in context (loopback, mDNS, Tor, private zone)"
    if _is_reserved_name(host):
        return HARD, "a name the standards reserve (RFC 2606/6761)"

    ip = parse_ip_literal(host)
    if ip is None and not _is_real_hostname(host):
        return HARD, "not a hostname any resolver could look up"

    if _is_asset_path(parts.path or ""):
        return POLICY, "a page asset rather than a document"
    if host in EXCLUDE_INFRA_HOSTS:
        return POLICY, "infrastructure (font CDN, DNS-over-HTTPS)"
    if host in EXCLUDE_HOSTS_EXACT or host.endswith(TS_NET_SUFFIX):
        return POLICY, "reachable only from this machine or tailnet"
    if ip is not None and is_unsafe_address(ip):
        return POLICY, "a private address, not globally addressable"
    if "." not in host:
        # Not "can never identify a document" — a local DNS zone, /etc/hosts or
        # a corporate proxy can make "https://wiki/runbook" perfectly real for
        # whoever is on that network. It is excluded because this collection
        # tracks globally addressable sources: a policy, not a fact.
        return POLICY, "a single-label name, not globally addressable"
    return "", ""


def retire_reason(url: str) -> str:
    """Why this row should go, or "" to leave it alone. Tier-agnostic."""
    return classify(url)[1]


@dataclass
class RetireStep:
    url: str
    zotero_key: str
    reason: str
    tier: str = HARD


def plan_retire(rows: list[dict], *, include_policy: bool = False) -> list[RetireStep]:
    """Decide what to retire, touching nothing.

    A row with no Zotero key AND no outstanding claim never had an item to
    trash; the row is still dropped so the index stops carrying it.

    An empty key with a `pending_key` is a different thing entirely, and this
    used to read them as the same: it is the normal state between reserve_url()
    and the POST returning. Deleting it let capture's POST land against a row
    that no longer existed, so set_zotero_key matched nothing and the item sat
    in Zotero with nothing in the index pointing at it -- invisible to dedup
    forever, which is the exact failure the reservation protocol exists to
    prevent. Whether such a claim is live or abandoned is not answerable from
    the index; capture's _resolve_claim settles it by asking Zotero. So this
    leaves it alone.
    """
    steps: list[RetireStep] = []
    for row in rows:
        if not row["zotero_key"] and row.get("pending_key"):
            continue
        tier, reason = classify(row["url_canonical"])
        if not reason:
            continue
        if tier == POLICY and not include_policy:
            continue
        steps.append(RetireStep(row["url_canonical"], row["zotero_key"], reason, tier))
    return steps


def journal_path(db_path: Path) -> Path:
    """Where removed rows are recorded so the index half stays recoverable."""
    return db_path.with_name(f"{db_path.name}.retired.jsonl")


def apply_retire(
    steps: list[RetireStep],
    *,
    db_path: Path,
    zotero: ZoteroClient,
    connect,
) -> dict[str, int]:
    """Carry out a plan: journal the row, trash the item, then drop the row."""
    counts = {"trashed": 0, "row_only": 0, "failed": 0, "skipped": 0}
    stamp = datetime.now(timezone.utc).isoformat()
    journal = journal_path(db_path)
    for step in steps:
        try:
            # Journal BEFORE anything is destroyed. Zotero's trash restores the
            # item but not the sighting history, so without this the index half
            # of the operation is a one-way door.
            with connect(db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM url_index WHERE url_canonical = ?", (step.url,)
                ).fetchone()
                tags = [
                    r[0]
                    for r in conn.execute(
                        "SELECT tag FROM pending_tags WHERE url_canonical = ?",
                        (step.url,),
                    )
                ]
            with journal.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "retired_at": stamp,
                            "reason": step.reason,
                            "tier": step.tier,
                            "row": dict(row) if row is not None else None,
                            "pending_tags": tags,
                        }
                    )
                    + "\n"
                )
            if step.zotero_key:
                # expect_url: the plan can be minutes old, and an item that has
                # become something else is no longer the one that was judged.
                if not zotero.trash_item(step.zotero_key, expect_url=step.url):
                    counts["skipped"] += 1
                    continue
                counts["trashed"] += 1
            else:
                counts["row_only"] += 1
            # Compare-and-swap on the pair that was PLANNED. Deleting by URL
            # alone deleted whatever row held that URL now -- and between the
            # plan and here, another session can drop this row and capture can
            # recreate the URL under a different key. The old code trashed K1
            # and then deleted K2's row, leaving K2 in the library with nothing
            # indexing it.
            with connect(db_path) as conn:
                dropped = conn.execute(
                    "DELETE FROM url_index"
                    " WHERE url_canonical = ? AND zotero_key = ?",
                    (step.url, step.zotero_key),
                ).rowcount
                if dropped:
                    # Only this row's queued sightings. If the row was replaced,
                    # the queue now belongs to whoever replaced it.
                    conn.execute(
                        "DELETE FROM pending_tags WHERE url_canonical = ?",
                        (step.url,),
                    )
            if not dropped:
                logger.warning(
                    "trashed item %s but its index row was replaced; leaving the "
                    "new row alone",
                    step.zotero_key or "(none)",
                )
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the pass
            logger.error("retire failed for %s: %s", step.url, e)
            counts["failed"] += 1
    return counts
