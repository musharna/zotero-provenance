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
           - a row containing a backslash is not a URL at all but a regex or an
             escaped string that the extractor mistook for one (for example
             "https://data\\.gramene\\.org/v69/genes.*"). Stripping its trailing
             ".*" would invent a URL nobody cited. A different defect, reported
             rather than guessed at.
           - a query that gained an "=" from the old canonicaliser. "?a=1&b=" and
             "?a=1&b" are not distinguishable after the fact, and both are valid,
             so rewriting would be a guess with no upside.

Trashing is `deleted: 1`, which is recoverable from any Zotero client.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .sqlite_cache import lookup_url
from .url_processing import canonicalize
from .zotero_client import ZoteroClient

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
    out = EMPHASIS_RUN_RE.sub("", url)
    while out.endswith(")") and out.count("(") < out.count(")"):
        out = out[:-1]
    return canonicalize(out) if out != url else url


def _is_not_a_url(url: str) -> bool:
    """True for a pattern the extractor mistook for a URL.

    A raw backslash means a regex or an escaped string. An asterisk anywhere but
    a trailing bold run means a wildcard, like "https://*.example.com/*", whose
    "correction" would be an address nobody ever cited.
    """
    if "\\" in url:
        return True
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
        corrected = correct_url(url)
        if corrected == url:
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
                zotero.update_url(step.zotero_key, step.corrected)
                with connect(db_path) as conn:
                    conn.execute(
                        "UPDATE url_index SET url_canonical = ? WHERE url_canonical = ?",
                        (step.corrected, step.url),
                    )
                counts["rewrite"] += 1
            else:
                survivor = lookup_url(db_path, step.corrected)
                # Carry the duplicate's provenance across before trashing it,
                # or the merge would throw away exactly the sighting history
                # this plugin exists to keep.
                if survivor and survivor["zotero_key"]:
                    tags = zotero.get_item_tags(step.zotero_key)
                    if tags:
                        zotero.add_tags(survivor["zotero_key"], tags)
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
