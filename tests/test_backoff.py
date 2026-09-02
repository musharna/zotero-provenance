"""A 429 was recorded and then ignored.

`RATE_LIMITED = "rate_limited"  # 429 -- back off, conclude nothing` says it in
the constant's own comment, and nothing backed off. `_HostPacer` enforced a
fixed per-host interval; a 429 was classified, counted, and the next row of the
SAME host went out at the same interval. On 2026-09-02 a verify sweep took 13
consecutive 429s from github.com in one burst and would have kept going.

That is the `--sleep` defect's sibling: the CLI parses the flag, the pacer
honours it, and the one thing neither end does is listen to the host's answer.
A fixed interval is a guess about what a host tolerates; a 429 is that host
telling us the guess is wrong, and it was the one input the pacer never took.

The fix is not a branch in each failure handler. There were three `pacer.wait()`
+ `hasher()` pairs across the two passes, and adding backoff to the failure arms
would have put one rule in three places -- the defect class that has now shipped
five times in this repository. So fetching becomes ONE function that waits,
reads, and lets the answer change the interval, and the guard derives that no
page is fetched anywhere else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from zotero_capture import snapshot as snapshot_mod
from zotero_capture.snapshot import PageRead, _HostPacer, _paced_fetch


def _pacer(sleep_s: float = 2.0):
    slept: list[float] = []
    clock = [0.0]

    def sleeper(s: float) -> None:
        slept.append(s)
        clock[0] += s

    p = _HostPacer(sleep_s, sleeper=sleeper, monotonic=lambda: clock[0])
    return p, slept


def _throttled(url: str, max_bytes: int = 0):
    raise httpx.HTTPStatusError(
        "429",
        request=httpx.Request("GET", url),
        response=httpx.Response(429, request=httpx.Request("GET", url)),
    )


def _ok(url: str, max_bytes: int = 0) -> PageRead:
    return PageRead(digest="D", final_url=url, covers_bytes=1, complete=True)


def test_a_throttled_host_waits_longer_next_time() -> None:
    """The defect, stated directly."""
    p, slept = _pacer(2.0)
    with pytest.raises(httpx.HTTPStatusError):
        _paced_fetch(p, _throttled, "https://a.test/1", 0)
    _paced_fetch(p, _ok, "https://a.test/2", 0)
    assert slept, "no delay at all after a 429"
    assert slept[-1] > 2.0, f"a 429 did not widen the interval: {slept}"


def test_an_UNTHROTTLED_host_is_not_slowed() -> None:
    """The other direction, and the one that makes the test above mean
    something. A 'fix' that simply slowed everything down would pass that
    assertion while turning a 2-hour sweep into a week."""
    p, slept = _pacer(2.0)
    _paced_fetch(p, _ok, "https://a.test/1", 0)
    _paced_fetch(p, _ok, "https://a.test/2", 0)
    assert slept == [2.0], f"an unthrottled host was penalised: {slept}"


def test_the_penalty_is_per_HOST() -> None:
    """github refusing us says nothing about arxiv. A global penalty would let
    one hostile host halt the entire corpus."""
    p, slept = _pacer(2.0)
    with pytest.raises(httpx.HTTPStatusError):
        _paced_fetch(p, _throttled, "https://a.test/1", 0)
    _paced_fetch(p, _ok, "https://b.test/1", 0)
    assert slept == [], f"an unrelated host paid for a.test's 429: {slept}"


def test_repeated_429s_back_off_further() -> None:
    p, slept = _pacer(2.0)
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            _paced_fetch(p, _throttled, "https://a.test/x", 0)
    _paced_fetch(p, _ok, "https://a.test/y", 0)
    assert slept[-1] > 4.0, f"three 429s did not compound: {slept}"


def test_retry_after_is_obeyed_when_the_host_states_one() -> None:
    """A host that says how long to wait has given us the answer; guessing an
    exponential when it told us is worse than either."""
    p, slept = _pacer(2.0)

    def throttled_with_header(url: str, max_bytes: int = 0):
        raise httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", url),
            response=httpx.Response(
                429, headers={"Retry-After": "30"}, request=httpx.Request("GET", url)
            ),
        )

    with pytest.raises(httpx.HTTPStatusError):
        _paced_fetch(p, throttled_with_header, "https://a.test/1", 0)
    _paced_fetch(p, _ok, "https://a.test/2", 0)
    assert slept[-1] >= 30.0, f"Retry-After ignored: {slept}"


def test_a_penalty_decays_once_the_host_answers_again() -> None:
    """Otherwise one 429 taxes every remaining row on that host for the rest of
    a multi-hour run."""
    p, slept = _pacer(2.0)
    with pytest.raises(httpx.HTTPStatusError):
        _paced_fetch(p, _throttled, "https://a.test/1", 0)
    first_after = None
    for i in range(2, 7):
        _paced_fetch(p, _ok, f"https://a.test/{i}", 0)
        if first_after is None:
            first_after = slept[-1]
    assert slept[-1] < first_after, f"the penalty never decayed: {slept}"


def test_a_404_does_not_slow_the_host() -> None:
    """Only a host telling us to slow down should slow us. A dead link is an
    answer, not a complaint -- and this corpus has 1,285 of them."""
    p, slept = _pacer(2.0)

    def missing(url: str, max_bytes: int = 0):
        raise httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", url),
            response=httpx.Response(404, request=httpx.Request("GET", url)),
        )

    with pytest.raises(httpx.HTTPStatusError):
        _paced_fetch(p, missing, "https://a.test/1", 0)
    _paced_fetch(p, _ok, "https://a.test/2", 0)
    assert slept == [2.0], f"a 404 was treated as throttling: {slept}"


# --- nothing fetches outside the one place ------------------------------------


def _direct_hasher_calls() -> list[tuple[str, int]]:
    """Every function that calls `hasher(...)` directly.

    After the fix exactly one may: `_paced_fetch`. Anything else is a fetch that
    bypasses both the interval and the host's feedback -- which is precisely how
    `verify` came to fetch 3,744 pages with no pacing at all.
    """
    tree = ast.parse(Path(snapshot_mod.__file__).read_text())
    out: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hasher"
            ):
                out.append((fn.name, node.lineno))
    return out


def test_the_derivation_finds_the_fetch_site() -> None:
    """Positive control: if this found nothing the guard below is vacuous."""
    assert _direct_hasher_calls(), "no hasher() call found; the guard is vacuous"


def test_only_one_function_fetches_a_page() -> None:
    stray = [(fn, ln) for fn, ln in _direct_hasher_calls() if fn != "_paced_fetch"]
    assert not stray, (
        f"hasher() is called outside _paced_fetch at {stray}; that fetch ignores "
        f"both the host interval and the host's 429"
    )
