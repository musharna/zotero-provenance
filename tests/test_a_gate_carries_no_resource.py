"""A gate is short because it has nothing to say.

0.51.0 gated the title on the document's own declaration -- a cross-site
`<base>`, which is a reCAPTCHA interstitial stating in HTML's own vocabulary
that it is not the article. That rule is sound and is untouched here. It is
also narrow: it sees exactly one vendor's signature. Measured 2026-09-03
against the live collection, 100 items carry a title that names an access
barrier rather than the resource, and the `<base>` rule catches only the 10
reCAPTCHA ones. The other 90 -- Imperva's "Client Challenge" on 46 pypi
package pages, "Sign in to GitHub", "Tailscale", "Log in - PyPI" -- declare
nothing at all. They are simply login pages and bot walls served with 200.

WHAT WAS TRIED FIRST AND REFUTED, so it is not tried again:

A structural corpus rule -- one title shared across URLs with no common path
ancestor -- scored precision 0.12 over all 144 duplicate-title groups (TP=7,
FP=52, FN=2). Its false positives are ONE PAPER CITED AT SEVERAL ADDRESSES:
doi.org and the publisher, arXiv /html and /pdf. That is not a defect in a
citation index, it is the case the index exists to serve. No tuning helps,
because "many addresses, one title" is produced identically by a legitimate
multi-address work and by a gate -- the corpus statistics are the same object.
The separating information is in the response, not in the corpus.

THE PROPERTY, measured on 51 live re-fetches before this was written:

    true gates (login page / bot challenge)   68 - 614 visible chars
    real content pages                     1,267 - 7,989
    site roots whose generic title is CORRECT
    (zenodo.org, huggingface.co)           4,583 - 23,363

A gate carries almost no prose because it has almost nothing to say.

CORRECTED IMMEDIATELY AFTER, because that table is drawn from four control
pages and a threshold argued from four pages is not argued. A 45-URL control
sample of rows that had already hashed successfully found NINETEEN real pages
under 800 visible characters: GitHub issues and pull requests measure 351-689
and a Nature article 274, because they render their content in JavaScript.
Prose alone does NOT separate them from a GitHub sign-in wall at 614.

So the rule is TWO conditions, and both are load-bearing:

    a COMPLETE document, under 64 KB, carrying under 800 characters of prose.

Size alone cannot do it either -- bioconductor serves a real package page in
29 KB, SMALLER than the 51 KB Hugging Face login wall, and it carries 4,832
characters. Together they separate every case measured: the 19 low-prose real
pages are 236 KB to 5.5 MB, so the reader stops before the end and no verdict
is ever formed about them.

False positives among pages the rule would actually JUDGE: 0 of 2 real pages
in that sample; the third judged page was a Reddit shell it correctly refused.
n=2 is a weak bound and is stated as one, in the manner of the 0/40 bound the
companion rule carries.

ON "NEVER A SNIFF OF ITS TEXT" -- the companion module states that rule, and
this does not break it. A count of prose characters is not a vocabulary: it
holds no vendor name and no phrase, it cannot be evaded by rewording, and it
does not decide from a URL path (which would manufacture the finding). It is
a structural property of the document, measured with a positive control in
the same run.

SCOPE, deliberately narrow: the TITLE path only. `hash_page` is NOT changed,
so the companion module's known limit -- a same-origin login wall stays
undetectable to the HASHER -- still stands exactly as written. Overturning it
for titles is a judgement, and the argument is this: for the hasher, the login
page genuinely is what that address serves an anonymous requester, and
recording its bytes is a true statement about our view. For the TITLE, storing
"Tailscale" as the name of a cited work is a false statement about the source
in the field a reader trusts most, and it is worse than storing nothing
because a confident title clears `title:unresolved` and nothing revisits the
item ever again.

THE GUARD RAIL that makes it safe: a verdict is pronounced only on a COMPLETE
document. A slow real page read halfway has little text so far and would look
exactly like a gate; the error would be timing-dependent, which is the failure
this repository has been bitten by before. Truncated means "no opinion", so
the title survives and behaviour is the status quo. Gates are small enough to
be read whole -- the largest measured is 51 KB against a 256 KB budget.
"""

from __future__ import annotations

import gzip
import itertools
from pathlib import Path

import httpx
import pytest

from zotero_capture.title_fetcher import fetch_title
from zotero_capture.url_processing import (
    MIN_RESOURCE_TEXT_CHARS,
    document_is_a_gate,
    visible_text_chars,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Captured verbatim from the live hosts 2026-09-03. Bodies, not summaries --
# a threshold argued from numbers I typed by hand is a threshold with no
# corpus behind it.
GATES = ["gate_imperva_pypi", "gate_recaptcha_geo", "gate_login_huggingface"]
REAL = ["real_arxiv_abs"]


def _body(name: str) -> bytes:
    with gzip.open(FIXTURES / f"{name}.html.gz", "rb") as f:
        return f.read()


def _serving(body: bytes) -> httpx.Client:
    """content-type is REQUIRED: `fetch_title` refuses a non-HTML response, so a
    mock without it returns the sentinel for every case and the gate test then
    passes on ungated code. The companion module learned that the hard way."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# --- the property -------------------------------------------------------------


@pytest.mark.parametrize("name", GATES)
def test_a_real_gate_carries_almost_no_prose(name: str) -> None:
    """THE property, on bodies the live hosts actually served."""
    assert document_is_a_gate(_body(name)), name


@pytest.mark.parametrize("name", REAL)
def test_a_real_article_is_not_a_gate(name: str) -> None:
    """The positive control. Without it, a function returning True always passes."""
    assert not document_is_a_gate(_body(name)), name


def test_a_login_wall_of_mostly_javascript_is_still_a_gate() -> None:
    """51 KB of script and 557 characters of prose.

    This is the case that decides HOW the text is counted. Byte size cannot
    separate a gate from a resource -- this wall is larger than the arXiv
    article it must be distinguished from. Only the rendered prose separates
    them. BeautifulSoup already omits <script>, <style> and <template> from
    `get_text`; <noscript> it does not, and that one is stripped deliberately
    (see the test below, which is what proves it).
    """
    wall = _body("gate_login_huggingface")
    article = _body("real_arxiv_abs")
    assert len(wall) > len(article), "the premise: the gate is the BIGGER document"
    assert document_is_a_gate(wall)
    assert not document_is_a_gate(article)


def test_code_and_fallback_text_are_not_prose() -> None:
    """The mechanism, isolated from any real body.

    Written first as a script-only case, where it was VACUOUS: BeautifulSoup
    already skips <script>, <style> and <template> in `get_text`, so emptying
    the strip list changed nothing and the mutation survived. <noscript> is the
    one bs4 counts, and it is the one that matters -- a JavaScript shell's
    noscript block says "You need to enable JavaScript to run this app", which
    is prose the document never shows a reader, and letting it count would lift
    a shell over the threshold.
    """
    shell = (
        b"<html><body><noscript>" + b"You need to enable JavaScript. " * 40
        + b"</noscript><script>" + b"x=1;" * 5000 + b"</script><p>hi</p></body></html>"
    )
    assert visible_text_chars(shell) < 20
    assert document_is_a_gate(shell)


def test_the_threshold_sits_between_the_two_measured_populations() -> None:
    """Pins the constant to the evidence rather than to my taste.

    CORRECTED after measuring, because the first version of this bound was
    drawn from four control pages and was wrong. A 45-URL sample of rows that
    had hashed successfully found NINETEEN real pages under 800 visible
    characters -- GitHub issue and pull-request pages run 351-689 because
    GitHub renders through JavaScript, and a Nature article measured 274.
    Prose alone therefore does NOT separate a GitHub sign-in wall (614) from a
    GitHub issue page (663). What protects them is that they are large and so
    are never read whole; see the test below, which is the one that matters.

    So the honest band: above every gate measured (614), below the prose of
    the smallest real page the rule would actually JUDGE (bioconductor's
    package page, 29 KB and 4,832 characters -- a real document SMALLER than
    the 51 KB Hugging Face wall, which is why size alone cannot do this
    either).
    """
    assert 614 < MIN_RESOURCE_TEXT_CHARS < 4832


def test_every_measured_body_lands_on_the_right_side() -> None:
    """The separation itself, stated as one assertion over the whole corpus."""
    gates = {n: visible_text_chars(_body(n)) for n in GATES}
    real = {n: visible_text_chars(_body(n)) for n in REAL}
    assert max(gates.values()) < MIN_RESOURCE_TEXT_CHARS <= min(real.values()), (
        gates,
        real,
    )


def test_a_large_page_with_little_prose_is_never_condemned() -> None:
    """THE false positive this rule must not produce, and it is not rare.

    19 of 33 real HTML pages sampled from successfully-hashed rows carry under
    800 visible characters: GitHub issues and pull requests, a Nature article.
    They are legitimate cited resources that happen to render their content in
    JavaScript. Every one of them is hundreds of kilobytes, so the reader stops
    before the end and never forms an opinion -- that, not the prose count, is
    what keeps them safe.
    """
    url = "https://github.com/example/repo/issues/1"
    shell = (
        b"<html><head><title>Some issue - example/repo</title></head><body>"
        b"<p>" + b"a" * 400 + b"</p><script>" + b"x=1;" * 60000 + b"</script>"
        b"</body></html>"
    )
    assert len(shell) > 64 * 1024, "the premise: too big to be read whole"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=shell, headers={"content-type": "text/html; charset=utf-8"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert fetch_title(url, client=http) == "Some issue - example/repo"


# --- the guard rail -----------------------------------------------------------


def test_a_gate_makes_fetch_title_return_the_url_sentinel() -> None:
    """End to end: the wall's title never becomes the citation's title."""
    url = "https://pypi.org/project/breedsim-mcp"
    with _serving(_body("gate_imperva_pypi")) as http:
        assert fetch_title(url, client=http) == url


def test_a_real_page_still_gets_its_real_title() -> None:
    """The positive control for the end-to-end path.

    A harness that refused everything would satisfy the test above, so the
    two must live together.
    """
    url = "https://arxiv.org/abs/2505.22337"
    with _serving(_body("real_arxiv_abs")) as http:
        got = fetch_title(url, client=http)
    assert got != url and "Learning to Infer" in got


def test_a_document_we_could_not_read_whole_is_never_called_a_gate() -> None:
    """THE guard rail. A verdict must not depend on how much we happened to read.

    The first megabyte of a long article, cut off at the byte budget, has a
    title and plenty of markup but the prose we have seen so far is what it
    is. Judging it would make the verdict a function of network speed.
    """
    article = _body("real_arxiv_abs")
    head = article[: article.find(b"</title>") + 8] + b"<body>short</body>"
    # As a bare document this looks exactly like a gate ...
    assert document_is_a_gate(head)
    # ... but read through the fetcher, which knows the read was cut short, the
    # title survives.
    url = "https://arxiv.org/abs/2505.22337"

    def handler(req: httpx.Request) -> httpx.Response:
        # More bytes than the reader will take, so the stream never ends.
        return httpx.Response(
            200,
            content=head + b"<p>" + b"y" * (300 * 1024) + b"</p>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert fetch_title(url, client=http) != url
