"""Asking GitHub whether a repository exists, when anonymity cannot tell.

0.39.0 established that a GitHub 404 is a fact about THIS REQUESTER's view, not
about the resource: GitHub 404s a private repository on purpose, so "deleted" and
"you may not see this" are the same response. Containment discriminated the ones
whose immediate parent is itself a repo, but a repo ROOT's parent is a public
profile page, which is visible whether or not the repo is. For those there is no
credential-free discriminator, and 0.39.0 stopped rather than guess. The library
has since carried rows asserting that the owner's own private repositories are
dead links.

A credential settles it, and the design is shaped entirely by what a credential
may be used FOR here:

  * **Existence only.** This asks `/repos/{owner}/{repo}` and reads the status
    code. It never fetches, hashes, or stores private content. The only thing
    that changes is an outcome label on a URL the library already holds, so no
    private information enters the library that was not already in it.
  * **It may only DOWNGRADE.** A 200 means the repo exists and we simply could
    not see it anonymously, which is `not_visible` -- a fact about us. A 404 is
    NOT promoted to `gone`, even with a token: it means this token cannot see it
    either, which is exactly the ambiguity we started with. Manufacturing absence
    from a stronger failure to observe would rebuild the original defect with
    more confidence behind it.
  * **Opt-in, never the live path.** Capture and snapshot stay credential-free.
    This runs only when a person invokes the corroboration command.

The token is read at call time and never stored. `GITHUB_TOKEN` wins if set,
otherwise `gh auth token` is asked, and if neither is available every answer is
UNKNOWN -- which changes nothing, rather than falling back to a guess.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# owner and repo as GitHub itself allows them. Deliberately not `[^/]+`: a
# fragment, a query, or a trailing `.git@sha` must not be swallowed into the
# name and then queried as if it were one.
_NAME = r"[A-Za-z0-9_.-]+"
_SLUG_RE = re.compile(rf"^/({_NAME})/({_NAME})/?$")

VISIBLE = "visible"  # exists; we could not see it anonymously
UNKNOWN = "unknown"  # this token cannot see it either, or we could not ask


def repo_slug(url: str) -> tuple[str, str] | None:
    """The owner/repo a URL names, or None when it does not name one.

    Only a repo ROOT. A deeper path is already discriminated by containment, and
    a URL that is merely hosted on github.com -- a gist, a raw file, an org
    profile -- is not a repository and must not be asked about as one.
    """
    parts = urlsplit(url)
    if parts.hostname not in ("github.com", "www.github.com"):
        return None
    m = _SLUG_RE.match(parts.path)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    # "github.com/settings" style single-segment paths never reach here, but
    # reserved first segments do look like owners. None of these is a user.
    if owner.lower() in {"settings", "orgs", "sponsors", "features", "about"}:
        return None
    if not repo:
        return None
    return owner, repo


def read_token(env: Mapping[str, str] | None = None) -> str:
    """A token, or "" when there is none. Never cached, never written down."""
    source: Mapping[str, str] = os.environ if env is None else env
    token = (source.get("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("no GitHub token available (%s)", e)
        return ""
    if out.returncode != 0:
        logger.info("gh could not supply a token: %s", (out.stderr or "").strip()[:120])
        return ""
    return (out.stdout or "").strip()


def repo_visibility(slug: tuple[str, str], *, client: Any, token: str) -> str:
    """VISIBLE when the repo exists for this token, UNKNOWN otherwise.

    Two answers, not three, and the missing one is the point: there is no
    "GONE". A 404 here means this credential cannot see it, which is a fact
    about the credential. Reporting that as deletion is the very error this
    module exists to stop making.
    """
    if not token:
        return UNKNOWN
    owner, repo = slug
    resp = client.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if resp.status_code == 200:
        return VISIBLE
    if resp.status_code in (401, 403):
        # A bad or rate-limited token says nothing about the repository.
        logger.warning(
            "GitHub refused the corroboration request for %s/%s (%s); "
            "treating as unknown",
            owner,
            repo,
            resp.status_code,
        )
        return UNKNOWN
    return UNKNOWN
