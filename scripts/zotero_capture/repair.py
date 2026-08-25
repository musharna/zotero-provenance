"""Repair index rows whose URL carries markdown that the old extractor kept.

The extractor used to trim trailing punctuation from markdown it had never
parsed, so a bolded link kept its emphasis: `**[text](url)**` stored the URL with
a trailing `**`. Such a URL can never resolve a title, so the item degrades to
the URL-as-title junk this plugin exists to remove. 116 of ~4,900 rows in the
deployed index are stored that way.

The extractor rewrite stops new damage; it cannot repair what is already there.

Three kinds of damage turn up, and only one of them is safely repairable:

  REWRITE  trailing markdown emphasis on an otherwise ordinary URL. Strip it,
           correct the item's URL, and move the index row.
  MERGE    the same, but the corrected URL is already in the index. The mangled
           item is a duplicate: carry its tags onto the survivor, then trash it.
  SKIP     everything else, listed but never touched:
           - a wildcard or regex the extractor mistook for a URL, such as
             "https://data\\.gramene\\.org/v69/genes.*". Stripping its trailing
             ".*" would invent a URL nobody cited. A different defect, reported
             rather than guessed at, and removed by the retirement pass instead.
           - a query that gained an "=" from the old canonicaliser. "?a=1&b=" and
             "?a=1&b" are not distinguishable after the fact, and both are valid,
             so rewriting would be a guess with no upside.

Rows also arrive with a tail the old blacklist tokenizer swallowed — a shell
backslash, a table pipe, an ANSI reset. Those ARE repairable, because RFC 3986
permits none of them unencoded, so the address plainly ends where the tail
begins. The exception is a tail with URL text after it ("{ID}.pdb"): cutting
there manufactures a real directory nobody cited, so those go to retirement.

Trashing is `deleted: 1`, which is recoverable from any Zotero client.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .sqlite_cache import lookup_url
from .url_processing import _TEMPLATE_RE
from urllib.parse import urlsplit

from .url_processing import canonicalize, is_storable_url, strip_ansi, strip_illegal_tail
from .zotero_client import UNRESOLVED_TITLE_TAG, ZoteroClient

logger = logging.getLogger(__name__)

Action = Literal["rewrite", "merge", "skip"]

# Only a run of TWO OR MORE asterisks is repaired, and "_" is never touched.
#
# Both are legal URL characters — RFC 3986 makes "_" unreserved and "*" a
# sub-delimiter — so stripping them on sight corrupts real URLs. The live index
# holds "https://foo.example/release_", which legitimately ends in one, and an
# earlier cut of this repair proposed to "fix" it. A trailing "**" is bold
# emphasis with no realistic alternative reading; a single trailing "*" has one,
# and the six rows carrying it are wildcards and regexes rather than damage.
EMPHASIS_RUN_RE = re.compile(r"\*{2,}$")


@dataclass
class RepairStep:
    url: str
    zotero_key: str
    action: Action
    corrected: str = ""
    reason: str = ""


def correct_url(url: str) -> str:
    """Strip trailing bold emphasis, re-balance a paren, then re-canonicalize.

    Order matters, and getting it wrong is what caused the damage: the old code
    trimmed punctuation first, so a URL ending "…)**" still ended in "*" when the
    paren rule ran, and the paren rule never fired.

    Canonicalizing at the end matters too. Stripping "**" off ".../bios/**"
    leaves a trailing slash, and a row that is not in canonical form never
    matches a future lookup — the repair would leave behind a second permanent
    near-duplicate instead of removing one.
    """
    out = _cut_at_illegal_tail(EMPHASIS_RUN_RE.sub("", url))
    while out.endswith(")") and out.count("(") < out.count(")"):
        out = out[:-1]
    return canonicalize(out) if out != url else url


def _cut_at_illegal_tail(url: str) -> str:
    """Drop a tail the old blacklist tokenizer swallowed, when there is one.

    Only the TAIL, and only via the same helper capture uses. Cutting at the
    first illegal character wherever it sits manufactures an address: repair
    turned "…/filter[name]|" into "…/filter", a shorter URL that resolves and
    that nobody cited. An illegal character in the middle means the row is a
    literal, and repaired_url() then refuses it rather than inventing something.

    Escape sequences are removed rather than cut at, because they wrap a URL
    instead of ending one: "…/a\x1b[31mcontinued" is a single address wearing a
    colour code.

    A template is refused outright — cutting "…/download/{ID}.pdb" at the brace
    manufactures "…/download/", a real fetchable directory nobody cited.
    """
    url = strip_ansi(url)
    if _TEMPLATE_RE.search(url):
        return url
    return strip_illegal_tail(url)


def repaired_url(url: str) -> str:
    """The address this row should hold, or "" when there is nothing to recover.

    The emptiness cases are as load-bearing as the corrections. Cutting
    "https://\x1b[0m" at the escape leaves "https://" — no host, not an address,
    and rewriting the item to it would replace junk with worse junk while
    reporting a repair. Retirement handles those; this returns "" so it can.
    """
    if _is_not_a_url(url):
        return ""
    out = correct_url(url)
    if out == url or not urlsplit(out).hostname:
        return ""
    # The same question capture asks. Repair used to emit addresses the
    # tokenizer would have rejected — "…/filter[name]" among them — so a
    # "successful" repair could store something capture would never accept.
    if not is_storable_url(out):
        return ""
    return out


def _is_not_a_url(url: str) -> bool:
    """True for a pattern the extractor mistook for a URL.

    An asterisk anywhere but a trailing bold run means a wildcard, like
    "https://*.example.com/*", whose "correction" would be an address nobody
    ever cited.

    A backslash used to disqualify a row outright, on the reasoning that it
    means a regex. That was too broad: it also skipped "https://cloud.r-project
    .org\\", where the backslash is a shell line-continuation swept up by the old
    tokenizer and the address in front of it is real. _cut_at_illegal_tail now
    draws the line properly — it recovers a trailing tail and refuses one that
    has URL text after it, which is what actually distinguishes the two.
    """
    return "*" in EMPHASIS_RUN_RE.sub("", url)


def plan_repair(rows: list[dict]) -> list[RepairStep]:
    """Decide what to do with every row, touching nothing."""
    known = {r["url_canonical"] for r in rows}
    steps: list[RepairStep] = []
    for row in rows:
        url = row["url_canonical"]
        key = row["zotero_key"]
        if _is_not_a_url(url):
            steps.append(
                RepairStep(url, key, "skip", reason="not a URL (regex or wildcard)")
            )
            continue
        corrected = repaired_url(url)
        if not corrected:
            continue
        if not key:
            steps.append(
                RepairStep(url, key, "skip", corrected, "claim never completed")
            )
            continue
        action: Action = "merge" if corrected in known else "rewrite"
        steps.append(RepairStep(url, key, action, corrected))
    return steps


def apply_repair(
    steps: list[RepairStep],
    *,
    db_path: Path,
    zotero: ZoteroClient,
    connect,
) -> dict[str, int]:
    """Carry out a plan. `connect` yields a sqlite connection to the index."""
    counts = {"rewrite": 0, "merge": 0, "skip": 0, "failed": 0}
    for step in steps:
        if step.action == "skip":
            counts["skip"] += 1
            continue
        try:
            if step.action == "rewrite":
                if not zotero.update_url(step.zotero_key, step.corrected):
                    # The item is gone. Rewriting the index row anyway would
                    # leave it claiming a corrected URL with nothing behind it,
                    # and the pass would report a repair that did not happen.
                    logger.warning(
                        "skipping rewrite of %s: item %s no longer exists",
                        step.url,
                        step.zotero_key,
                    )
                    counts["skip"] += 1
                    continue
                with connect(db_path) as conn:
                    conn.execute(
                        "UPDATE url_index SET url_canonical = ? WHERE url_canonical = ?",
                        (step.corrected, step.url),
                    )
                counts["rewrite"] += 1
            else:
                survivor = lookup_url(db_path, step.corrected)
                survivor_key = survivor["zotero_key"] if survivor else ""
                # A merge trashes this row's item on the promise that the
                # corrected URL already has one. If it does not, that promise is
                # false and the trash destroys the only real item, leaving an
                # orphan row pointing at nothing. plan_repair() cannot settle
                # this, because it only sees that the corrected URL is present
                # in the index — not whether that row's claim ever completed —
                # and a plan can go stale between the dry run and the apply.
                #
                # Skip, never fall back to a rewrite: the corrected URL already
                # occupies the primary key, so the UPDATE would fail AFTER the
                # Zotero item had been changed, leaving remote and local
                # disagreeing. An unfinished claim is the reservation protocol's
                # to settle, not this pass's.
                if not survivor_key or not zotero.item_exists(survivor_key):
                    logger.warning(
                        "skipping merge of %s: %s has no item to merge into",
                        step.url,
                        step.corrected,
                    )
                    counts["skip"] += 1
                    continue
                # Provenance moves; state does not. "title:unresolved" describes
                # the DUPLICATE's own title, and carrying it onto a survivor that
                # already has a real one marks a healthy item as junk — and
                # permanently, because title_is_unresolved() trusts the tag over
                # the title it can see.
                tags = [
                    t
                    for t in zotero.get_item_tags(step.zotero_key)
                    if t != UNRESOLVED_TITLE_TAG
                ]
                if tags:
                    zotero.add_tags(survivor_key, tags)
                zotero.trash_item(step.zotero_key)
                with connect(db_path) as conn:
                    conn.execute(
                        "DELETE FROM url_index WHERE url_canonical = ?", (step.url,)
                    )
                counts["merge"] += 1
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the pass
            logger.error("repair failed for %s: %s", step.url, e)
            counts["failed"] += 1
    return counts
