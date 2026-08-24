"""Retire index rows that can never be a source, and the items behind them.

Repair and retirement are different verbs and they need different predicates.
`repair` corrects a URL that was damaged on the way in — there is a right answer
and it recovers it. Retirement is for a row where there is no right answer: the
text was never an address, or it is one today's rules would refuse outright. The
repair pass deliberately SKIPS these rather than guessing a correction, which is
right, but skipping leaves them in the library for good.

The predicate is "this can never resolve to a document", NOT "this contains a
character RFC 3986 forbids". The two overlap heavily and are not the same test,
and using the character test to decide deletion is how something real eventually
gets thrown away. Three reasons qualify:

  PLACEHOLDER  a template, not a URL: "{ID}", "{locus}", "${VERSION}". Nobody
               cited "https://files.rcsb.org/download/{ID}.pdb"; it is a shape.
  CONTROL      a control byte, from terminal output pasted into a message — an
               ANSI colour reset on the end of a stack-trace URL.
  EXCLUDED     an address today's rules already refuse: a reserved or fixture
               name (example.com, .test), infrastructure (a font CDN, DoH), an
               asset (a badge SVG, a photo). These rows predate the rule that
               would have stopped them; capture has not produced one in months.

Everything else is left alone, including rows that merely look odd. A wildcard
or regex like "https://data\\.gramene\\.org/v69/genes.*" is NOT retired here:
CommonMark unescapes the backslashes, "*" is a legal sub-delimiter, and deciding
those needs the code-block judgement rather than a rule about addresses.

Trashing is `deleted: 1`, recoverable from any Zotero client, never the
permanent DELETE.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .repair import repaired_url
from .url_processing import is_excluded
from .zotero_client import ZoteroClient

logger = logging.getLogger(__name__)

# "{" or "}" anywhere, or a shell-style "${...}". A brace is illegal in a URL
# unencoded (RFC 3986), so its presence is not ambiguous the way "*" is.
PLACEHOLDER_RE = re.compile(r"[{}]")

# C0, DEL and C1. An ESC arrives as the head of an ANSI sequence.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass
class RetireStep:
    url: str
    zotero_key: str
    reason: str


def retire_reason(url: str) -> str:
    """Why this row can never be a source, or "" to leave it alone.

    Repair gets first refusal, and this is not merely an ordering convenience.
    "https://cloud.r-project.org\\" fails the address test — a backslash is not
    a hostname character — yet the address in front of the backslash is real and
    the repair pass recovers it. Deciding retirement without asking repair first
    would trash a citation that was one character away from being correct, and it
    would do so for a reason that sounds convincing in the log.
    """
    if repaired_url(url):
        return ""
    if PLACEHOLDER_RE.search(url):
        return "template placeholder, not an address"
    if CONTROL_RE.search(url):
        return "control character, from pasted terminal output"
    if is_excluded(url):
        return "an address today's rules refuse (reserved, infra or asset)"
    return ""


def plan_retire(rows: list[dict]) -> list[RetireStep]:
    """Decide what to retire, touching nothing.

    A row with no Zotero key never completed its claim, so there is no item to
    trash; the row is still dropped so the index stops carrying it.
    """
    steps: list[RetireStep] = []
    for row in rows:
        reason = retire_reason(row["url_canonical"])
        if reason:
            steps.append(RetireStep(row["url_canonical"], row["zotero_key"], reason))
    return steps


def apply_retire(
    steps: list[RetireStep],
    *,
    db_path: Path,
    zotero: ZoteroClient,
    connect,
) -> dict[str, int]:
    """Carry out a plan: trash each item, then drop its row."""
    counts = {"trashed": 0, "row_only": 0, "failed": 0}
    for step in steps:
        try:
            if step.zotero_key:
                zotero.trash_item(step.zotero_key)
                counts["trashed"] += 1
            else:
                counts["row_only"] += 1
            with connect(db_path) as conn:
                conn.execute(
                    "DELETE FROM url_index WHERE url_canonical = ?", (step.url,)
                )
                conn.execute(
                    "DELETE FROM pending_tags WHERE url_canonical = ?", (step.url,)
                )
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the pass
            logger.error("retire failed for %s: %s", step.url, e)
            counts["failed"] += 1
    return counts
