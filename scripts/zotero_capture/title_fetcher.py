"""HTTP GET first ~32KB, parse <title>, 1s budget. URL-as-fallback on any failure."""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 1.0
MAX_BYTES = 32 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_USER_AGENT = "zotero-provenance/0.1"

# Some hosts serve identifiers, not web pages: a DOI, an arXiv id, a PMID, a repo
# path. Each has an authoritative metadata API, and the HTML behind it is slower
# and frequently bot-walled — pubmed and arxiv 403 a plain fetch outright. Ask the
# API instead of scraping whatever the identifier happens to land on.
_CSL_ACCEPT = "application/vnd.citationstyles.csl+json"


def _doi_title(client: httpx.Client, url: str) -> str | None:
    """Ask the DOI resolver for citation metadata. None if it can't be had."""
    try:
        resp = client.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": _CSL_ACCEPT},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        title = resp.json().get("title")
    except Exception as e:  # malformed JSON, transport failure, timeout
        logger.debug("DOI content negotiation failed for %s: %s", url, e)
        return None
    # CSL allows a list of title forms; the first is the primary one.
    if isinstance(title, list):
        title = title[0] if title else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _arxiv_title(client: httpx.Client, url: str) -> str | None:
    """Resolve an arXiv id through the export API (the abs page 403s a plain fetch)."""
    m = re.search(r"(\d{4}\.\d{4,5})", urlsplit(url).path)
    if not m:
        return None
    resp = client.get(
        "http://export.arxiv.org/api/query",
        params={"id_list": m.group(1)},
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        return None
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    # Scope to <entry>: the feed carries its own <title> that is not the paper's.
    entry = ElementTree.fromstring(resp.text).find("atom:entry/atom:title", ns)
    return entry.text.strip() if entry is not None and entry.text else None


def _pubmed_title(client: httpx.Client, url: str) -> str | None:
    """Resolve a PMID through E-utilities."""
    m = re.search(r"/(\d+)", urlsplit(url).path)
    if not m:
        return None
    pmid = m.group(1)
    resp = client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params={"db": "pubmed", "id": pmid, "retmode": "json"},
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        return None
    title = resp.json().get("result", {}).get(pmid, {}).get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _preprint_title(client: httpx.Client, url: str) -> str | None:
    """bioRxiv/medRxiv carry their DOI in the URL path; resolve that."""
    m = re.search(r"(10\.1101/[0-9a-zA-Z.]+)", urlsplit(url).path)
    if not m:
        return None
    return _doi_title(client, f"https://doi.org/{m.group(1)}")


def _github_title(client: httpx.Client, url: str) -> str | None:
    """Resolve a repo through the GitHub API.

    Roughly half of captured GitHub URLs are dead or private repos that 404 here;
    those correctly stay unresolved rather than getting an invented title.
    `GITHUB_TOKEN` lifts the anonymous 60/hour rate limit when one is available.
    """
    m = re.match(r"^/([^/]+)/([^/]+)", urlsplit(url).path)
    if not m:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.get(
        f"https://api.github.com/repos/{m.group(1)}/{m.group(2).removesuffix('.git')}",
        headers=headers,
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        return None
    data = resp.json()
    name, desc = data.get("full_name"), data.get("description")
    if not name:
        return None
    return f"{name}: {desc}" if desc else name


_RESOLVERS: dict[str, Callable[[httpx.Client, str], str | None]] = {
    "doi.org": _doi_title,
    "dx.doi.org": _doi_title,
    "www.doi.org": _doi_title,
    "arxiv.org": _arxiv_title,
    "www.arxiv.org": _arxiv_title,
    "pubmed.ncbi.nlm.nih.gov": _pubmed_title,
    "biorxiv.org": _preprint_title,
    "www.biorxiv.org": _preprint_title,
    "medrxiv.org": _preprint_title,
    "www.medrxiv.org": _preprint_title,
    "github.com": _github_title,
    "www.github.com": _github_title,
}


def _identifier_title(client: httpx.Client, url: str) -> str | None:
    """Resolve via an identifier API when the host serves identifiers, else None."""
    resolver = _RESOLVERS.get((urlsplit(url).hostname or "").lower())
    if resolver is None:
        return None
    try:
        return resolver(client, url)
    except Exception as e:  # malformed payload, transport failure, timeout
        logger.debug("identifier resolution failed for %s: %s", url, e)
        return None


def fetch_title(url: str, *, client: httpx.Client | None = None) -> str:
    """Fetch <title> with 1s wall-clock budget. Return URL on any failure.

    Designed for the Stop hook (T8) — URL-as-fallback is the contract;
    callers distinguish "got real title" from "got URL back" by string equality.

    For batch use (≥2 URLs per Stop turn), pass a shared `httpx.Client`
    to amortize TCP/TLS handshake — caller owns lifecycle.
    """
    owns_client = client is None
    try:
        if client is None:
            client = httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
        deadline = time.monotonic() + DEFAULT_TIMEOUT_S
        identifier_title = _identifier_title(client, url)
        if identifier_title:
            return identifier_title
        # Identifier resolution is an optimisation, not a new point of failure:
        # fall through and scrape the page as before.
        with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}) as resp:
            if resp.status_code >= 400:
                return url
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype.lower():
                return url
            chunks: list[bytes] = []
            received = 0
            for chunk in resp.iter_bytes():
                if time.monotonic() > deadline:
                    return url
                chunks.append(chunk)
                received += len(chunk)
                if received >= MAX_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BYTES]
        try:
            soup = BeautifulSoup(body, "html.parser")
            tag = soup.find("title")
            if tag:
                text = tag.get_text(separator=" ", strip=True)
                if text:
                    return text
        except Exception as e:
            logger.debug("bs4 parse failed for %s: %s", url, e)
            m = _TITLE_RE.search(body.decode("utf-8", errors="replace"))
            if m:
                return m.group(1).strip()
        return url
    except (httpx.HTTPError, OSError) as e:
        logger.debug("title fetch failed for %s: %s", url, e)
        return url
    except Exception as e:
        logger.warning("unexpected title fetch error for %s: %s", url, e)
        return url
    finally:
        if owns_client and client is not None:
            client.close()
