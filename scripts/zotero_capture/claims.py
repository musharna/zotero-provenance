"""What a captured URL was cited FOR, kept on this machine and nowhere else.

The library records that a source was consulted. It cannot say what the source
was consulted *for* -- which is the question anyone auditing a bibliography
actually asks, and the one a list of URLs cannot answer six months later.

**This never leaves the machine.** The claim is the surrounding sentence of a
conversation, and the Zotero library SYNCS. Three designs were possible: a child
note, a quote in `extra`, or local-only. Local-only is the one where fragments of
conversations do not get pushed to a third party's servers, and it is the
reversible one -- a local row can be promoted to a note later, but a note that
has already synced cannot be recalled. Nothing in this module talks to Zotero,
and `test_claims.py` asserts the text of a claim appears in NO outbound payload
of a real capture, because a privacy property that rests on nobody adding the
wrong import later is a convention, not a guarantee.

The extraction is deliberately dumb: the sentence around the URL, as written. It
does not summarise, infer, or ask a model what the claim "really" was. A stored
sentence can be read and judged by a person; a generated paraphrase is one more
thing that can be wrong about a source, filed under provenance.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .sqlite_cache import _connect

# Long enough for a real sentence, short enough that the index does not become a
# copy of the conversation. Truncation is marked, so a reader can see that what
# they are looking at is not the whole of what was said.
CLAIM_MAX_CHARS = 400

# Distinct claims kept per URL. A URL cited fifty times for fifty DIFFERENT
# reasons is vanishingly rare -- repeats collapse onto one row -- so this is a
# runaway bound, not a retention policy. At the cap new claims are REFUSED
# rather than the oldest evicted, which is the same choice the retry queue
# makes, and it keeps the earliest reason a source entered the library: that is
# the provenance, and a later mention is not a better record of it.
CLAIMS_PER_URL = 50

# A sentence ends at .!? followed by space, or at a line break. Bullets and
# headings are therefore their own claims, which is what they are. Abbreviations
# ("e.g.", "Fig. 2") split early; the cost is a short claim, not a wrong one, and
# a heuristic that reads a stored sentence is preferable to one that guesses.
_LEFT_BOUNDARY = re.compile(r"[.!?]\s|\n")
_HAS_WORD = re.compile(r"\w")


def claim_for(message: str, raw_url: str) -> str:
    """The sentence `raw_url` appears in, or "" if it appears without one.

    Scanning never enters the URL's own span -- a URL is full of dots and would
    otherwise be split into a "sentence" of its own tail.
    """
    start = message.find(raw_url)
    if start < 0:
        return ""
    end = start + len(raw_url)

    left = 0
    for match in _LEFT_BOUNDARY.finditer(message, 0, start):
        left = match.end()
    right = len(message)
    for match in _LEFT_BOUNDARY.finditer(message, end):
        right = match.start() + 1 if message[match.start()] != "\n" else match.start()
        break

    sentence = " ".join(message[left:right].split())
    # A URL on a line by itself is not a claim about anything. Storing the bare
    # URL back as its own justification would fill the table with rows that
    # answer the question with the question.
    without_url = sentence.replace(raw_url, " ")
    if not _HAS_WORD.search(without_url):
        return ""
    if len(sentence) > CLAIM_MAX_CHARS:
        cut = sentence.rfind(" ", 0, CLAIM_MAX_CHARS)
        sentence = sentence[: cut if cut > 0 else CLAIM_MAX_CHARS].rstrip() + " […]"
    return sentence


def record_claim(
    db_path: Path,
    *,
    url_canonical: str,
    claim: str,
    project: str,
    context: str,
    origin: str,
    now: str,
) -> bool:
    """Store one claim link. Returns False if it was refused at the cap.

    The same sentence citing the same URL again is one link seen twice, not two
    links: `times_seen` counts, and `last_seen` moves.
    """
    if not claim:
        return False
    with closing(_connect(db_path)) as conn:
        existing = conn.execute(
            "SELECT 1 FROM claim_link WHERE url_canonical = ? AND claim = ?",
            (url_canonical, claim),
        ).fetchone()
        if existing is None:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM claim_link WHERE url_canonical = ?",
                (url_canonical,),
            ).fetchone()
            if count >= CLAIMS_PER_URL:
                return False
        conn.execute(
            """
            INSERT INTO claim_link (url_canonical, claim, project, context,
                                    origin, first_seen, last_seen, times_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(url_canonical, claim) DO UPDATE SET
                last_seen  = excluded.last_seen,
                times_seen = times_seen + 1
            """,
            (url_canonical, claim, project, context, origin, now, now),
        )
    return True


def claims_for_url(db_path: Path, url_canonical: str) -> list[sqlite3.Row]:
    with closing(_connect(db_path)) as conn:
        return list(
            conn.execute(
                "SELECT * FROM claim_link WHERE url_canonical = ?"
                " ORDER BY last_seen DESC",
                (url_canonical,),
            )
        )


def search_claims(db_path: Path, term: str, *, limit: int = 50) -> list[sqlite3.Row]:
    """Claims whose text or URL contains `term`. The point of storing them."""
    with closing(_connect(db_path)) as conn:
        return list(
            conn.execute(
                "SELECT * FROM claim_link"
                " WHERE claim LIKE ? OR url_canonical LIKE ?"
                " ORDER BY last_seen DESC LIMIT ?",
                (f"%{term}%", f"%{term}%", limit),
            )
        )


def claim_counts(db_path: Path) -> tuple[int, int]:
    """(claim links, URLs carrying at least one) -- what coverage looks like."""
    with closing(_connect(db_path)) as conn:
        (links,) = conn.execute("SELECT COUNT(*) FROM claim_link").fetchone()
        (urls,) = conn.execute(
            "SELECT COUNT(DISTINCT url_canonical) FROM claim_link"
        ).fetchone()
    return links, urls


def format_claims(rows: list[sqlite3.Row], *, show_url: bool = True) -> list[str]:
    lines: list[str] = []
    for row in rows:
        if show_url:
            lines.append(row["url_canonical"])
        seen = f"{row['last_seen'][:10]}"
        if row["times_seen"] > 1:
            seen += f" (x{row['times_seen']})"
        lines.append(f"  {seen}  [{row['project']}] {row['claim']}")
    return lines
