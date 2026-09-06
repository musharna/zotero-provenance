"""A credential may settle whether a repo exists. It may not manufacture absence.

0.39.0 corrected 204 rows that claimed the owner's own private PRs were dead,
using containment: a 404 whose immediate parent is also hidden is consistent with
absence, one whose parent is visible is not. That leaves repo ROOTS, whose parent
is a public profile page and therefore visible either way -- 17 rows in the live
index still asserting `gone` about repositories that are simply private.

The asymmetry below is the whole design. A token can prove PRESENCE (200: it
exists, we merely could not see it anonymously) and cannot prove ABSENCE (404:
this token cannot see it either, which is the same ambiguity with more confidence
behind it). So VISIBLE downgrades `gone` to `not_visible`, and nothing ever
promotes anything to `gone`.
"""

from __future__ import annotations

import httpx
import pytest

from zotero_capture.github_visibility import (
    UNKNOWN,
    VISIBLE,
    read_token,
    repo_slug,
    repo_visibility,
)


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/someone/private-repo", ("someone", "private-repo")),
        ("https://github.com/someone/private-repo/", ("someone", "private-repo")),
        # `.git` is a suffix of the clone URL, not part of the name.
        (
            "https://github.com/someone/other-repo.git",
            ("someone", "other-repo"),
        ),
        # Deeper paths are already discriminated by containment; asking about
        # them here would query a repo the URL does not name.
        ("https://github.com/someone/repo/pull/332", None),
        ("https://github.com/someone/repo/issues", None),
        # Not a repository at all.
        ("https://github.com/someone", None),
        ("https://github.com/settings/tokens", None),
        ("https://gist.github.com/someone/abc123", None),
        ("https://raw.githubusercontent.com/a/b", None),
        ("https://notgithub.com/a/b", None),
        # A host that merely CONTAINS github.com must not match -- this project
        # has shipped a substring where it meant a token three times.
        ("https://github.com.evil.test/a/b", None),
    ],
)
def test_repo_slug(url: str, slug) -> None:
    assert repo_slug(url) == slug


def _client(status: int, seen: list | None = None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(req.url), dict(req.headers)))
        return httpx.Response(status, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_private_repo_that_exists_is_visible() -> None:
    """The finding: 17 rows call these dead links."""
    with _client(200) as c:
        assert (
            repo_visibility(("someone", "private-repo"), client=c, token="t") == VISIBLE
        )


def test_a_404_with_a_token_is_still_unknown_not_gone() -> None:
    """THE rule. A token that cannot see it either has not proved deletion, and
    a module that returned GONE here would rebuild the original defect with more
    confidence behind it. There is deliberately no GONE in this vocabulary."""
    with _client(404) as c:
        assert repo_visibility(("someone", "deleted"), client=c, token="t") == UNKNOWN


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_or_rate_limited_token_says_nothing_about_the_repo(
    status: int,
) -> None:
    """A bad credential is a fact about the credential. Reading it as evidence
    about the resource is the same error one layer along."""
    with _client(status) as c:
        assert repo_visibility(("a", "b"), client=c, token="t") == UNKNOWN


def test_without_a_token_nothing_is_asked_and_nothing_changes() -> None:
    """No token must mean "no answer", never a fallback guess -- and it must not
    spend a request finding that out."""
    seen: list = []
    with _client(200, seen) as c:
        assert repo_visibility(("a", "b"), client=c, token="") == UNKNOWN
    assert seen == [], "asked GitHub without a credential"


def test_the_token_is_sent_as_a_bearer_credential() -> None:
    seen: list = []
    with _client(200, seen) as c:
        repo_visibility(("a", "b"), client=c, token="SECRET")
    url, headers = seen[0]
    assert url == "https://api.github.com/repos/a/b"
    assert headers["authorization"] == "Bearer SECRET"


def test_only_existence_is_requested() -> None:
    """The privacy property, asserted rather than promised: the request is for
    repo METADATA and nothing reads content. A change that started fetching
    files would fail here."""
    seen: list = []
    with _client(200, seen) as c:
        repo_visibility(("a", "b"), client=c, token="t")
    assert len(seen) == 1
    url, _ = seen[0]
    assert url.endswith("/repos/a/b"), "asked for something other than existence"


def test_read_token_prefers_the_environment_and_returns_empty_when_absent() -> None:
    assert read_token({"GITHUB_TOKEN": "abc"}) == "abc"
    assert read_token({"GITHUB_TOKEN": "   "}) in ("", read_token({}))
