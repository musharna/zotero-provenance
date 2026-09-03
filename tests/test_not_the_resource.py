"""A page can answer 200 and not be the page.

`raise_for_status` is the only gate this module had on "are these bytes the
resource we asked for", and it is keyed on transport status. A bot challenge
served with HTTP 200 passes it, so its bytes reached the comparison and the
disagreement with the real article was written down as `unstable` -- a claim
that the SOURCE is too volatile to characterise, when what actually happened
is that one of our two reads was not the document at all. A fact about us,
recorded in the library as a finding about a citation, which is the precise
harm this whole tool exists to prevent.

Measured live 2026-09-03 before any of this was written:

  * `pmc.ncbi.nlm.nih.gov` served the real article 5 times in 6 and a
    reCAPTCHA interstitial once. The wall is INTERMITTENT -- it is not a
    property of the host, and an earlier note in this repository that called
    it "101/101 a CAPTCHA wall" was drawn from two probes that both happened
    to land on the challenge.
  * the pair-read that caught one produced coverage 0.0000 (360,179 bytes
    over 3,222 lines against 21,382 bytes over 33). That is not a volatile
    document; volatility on this corpus measures 75-99%. Near-zero coverage
    means two categorically different responses.
  * a row already recorded as SUCCESSFULLY HASHED -- a GEO accession under
    www.ncbi.nlm.nih.gov -- was serving the challenge when re-probed, so the
    damage is not confined to the unstable bucket.

The evidence is the document's own words, never a sniff of its text. A
reCAPTCHA interstitial sets

    <base href="https://www.google.com/recaptcha/challengepage/">

which is the document declaring, through the mechanism HTML provides for
exactly that purpose, that it is a Google challenge page and not the article.
Deciding from a title string or a phrase in the body would be a blacklist,
and this repository has already shipped one of those (`URL_RE`).

False-positive rate MEASURED before the rule was written, not assumed: 40
pages fetched across 40 distinct hosts, sampled at random from rows that had
hashed successfully. Three carried a `<base>` at all, exactly one was
cross-origin, and that one was a challenge page. 0 false positives in 40 --
a bound, not a proof of zero, and the bound is the honest claim.

KNOWN LIMIT, deliberately not fixed here: a SAME-ORIGIN login wall is
undetectable by this rule and stays undetectable. Five `login.tailscale.com`
rows are baselined at ~28,000 stable bytes -- a login page recorded as the
cited document. There is no honest discriminator: for an unauthenticated
requester, the login page genuinely IS what that address serves, the same
way GitHub's 404 on a private repository is a fact about the requester's
view. Guessing from a URL path would manufacture the finding.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import date
from pathlib import Path

import httpx
import pytest

from zotero_capture.snapshot import (
    BLOCKED,
    NotTheResource,
    classify_failure,
    hash_page,
    responding_url,
    snapshot,
)
from zotero_capture.sqlite_cache import init_db, insert_url, row_for_url

SEEN = date(2026, 9, 3)


class _Stamper:
    def record_content_hash(self, item_key, digest, *, expect_url=None):
        return True

ARTICLE = "https://publisher.invalid/articles/PMC1"
CHALLENGE_BASE = "https://www.google.com/recaptcha/challengepage/"

# The shape a real interstitial has: a short document whose `<base>` points at
# somebody else entirely. Byte-for-byte structure taken from the live probe.
WALL = (
    b"<html><head><base href=\"" + CHALLENGE_BASE.encode() + b"\">"
    b"<title>Checking your browser - reCAPTCHA</title></head>"
    b"<body>nonce=abc123</body></html>"
)
REAL = b"<html><head><base href=\"/\"><title>An article</title></head><body>" \
       + b"x" * 4000 + b"</body></html>"


def _serving(body: bytes, *, host_url: str = ARTICLE) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


# --- the document's own declaration ------------------------------------------


def test_a_challenge_page_is_refused_rather_than_hashed() -> None:
    """THE property. A 200 whose body declares a foreign base is not the page."""
    with _serving(WALL) as http:
        with pytest.raises(NotTheResource):
            hash_page(ARTICLE, client=http)


def test_a_page_that_declares_its_own_site_is_hashed_normally() -> None:
    """The positive control. Without it, a rule that refused EVERYTHING would
    satisfy the test above and read as a pass."""
    with _serving(REAL) as http:
        read = hash_page(ARTICLE, client=http)
    assert read.digest == hashlib.sha256(REAL).hexdigest()


def test_a_protocol_relative_base_on_the_same_site_is_hashed() -> None:
    """`//www.bioconductor.org` answered by bioconductor.org is one site. Seen
    in the live sample, so it is a real shape and not an invented one."""
    body = b"<html><head><base href=\"//www.publisher.invalid/\"></head>ok</html>"
    with _serving(body) as http:
        assert hash_page(ARTICLE, client=http).digest


def test_the_challenge_names_ITSELF_as_what_answered_us() -> None:
    """The address recorded must be the challenge, not the host we asked. This
    is the 0.38.0 lesson one layer along: a refusal that names the wrong host
    puts the blame on a publisher that did nothing."""
    with _serving(WALL) as http:
        with pytest.raises(NotTheResource) as caught:
            hash_page(ARTICLE, client=http)
    assert responding_url(caught.value, ARTICLE) == CHALLENGE_BASE
    assert responding_url(caught.value, ARTICLE) != ARTICLE


def test_a_challenge_is_a_refusal_not_a_finding_about_the_source() -> None:
    """`blocked` -- reused, not a new word. "Refused; the page may be perfectly
    fine" is already exactly what happened, and a fifth outcome meaning the
    same thing would be a second copy of one rule."""
    exc = NotTheResource(declared=CHALLENGE_BASE, requested=ARTICLE)
    assert classify_failure(exc) == BLOCKED


# --- what reaches the index --------------------------------------------------


def test_snapshot_stores_no_hash_for_a_challenge(tmp_path: Path) -> None:
    """The harm end to end: a wall must never become a provenance baseline."""
    db = tmp_path / "i.db"
    init_db(db)
    insert_url(db, ARTICLE, "K1", SEEN)
    with _serving(WALL) as http:
        snapshot(
            db,
            zotero=_Stamper(),
            hasher=lambda u, n: hash_page(u, client=http, max_bytes=n),
            clock=lambda: "NOW",
        )
    row = row_for_url(db, ARTICLE)
    assert row["last_outcome"] == BLOCKED
    assert not row["content_hash"]
    assert row["final_url"] == CHALLENGE_BASE


def test_snapshot_still_hashes_a_real_page(tmp_path: Path) -> None:
    """Positive control inside the same surface: a negative result needs one."""
    db = tmp_path / "i.db"
    init_db(db)
    insert_url(db, ARTICLE, "K1", SEEN)
    with _serving(REAL) as http:
        snapshot(
            db,
            zotero=_Stamper(),
            hasher=lambda u, n: hash_page(u, client=http, max_bytes=n),
            clock=lambda: "NOW",
        )
    row = row_for_url(db, ARTICLE)
    assert row["content_hash"] == hashlib.sha256(REAL).hexdigest()


# --- the guard that stops this becoming a second copy ------------------------


def test_every_fetch_site_recognises_the_same_faults() -> None:
    """DERIVED from the fetch sites, never a list of the ones alive today.

    Three `except` arms each repeated `isinstance(e, (httpx.HTTPError,
    httpx.InvalidURL))`. Adding a fourth kind of failure to two of three is the
    defect class that has shipped FIVE times in this repository, and a guard
    naming its subjects cannot fail on a call site nobody added it to -- which
    is the same defect in the test that the test exists to prevent in the code.

    So the obligation is computed: every `try` that fetches must hand its
    handler the one shared tuple.
    """
    source = Path("scripts/zotero_capture/snapshot.py").read_text()
    tree = ast.parse(source)
    fetch_sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        fetches = any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_paced_fetch"
            for c in ast.walk(node)
        )
        if not fetches:
            continue
        fetch_sites += 1
        names = {n.id for h in node.handlers for n in ast.walk(h)
                 if isinstance(n, ast.Name)}
        assert "FETCH_FAULTS" in names, (
            "a fetch site whose handler does not use the shared fault tuple"
        )
    assert fetch_sites >= 3, f"expected the known fetch sites, found {fetch_sites}"
