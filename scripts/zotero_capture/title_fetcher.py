"""HTTP GET first ~32KB, parse <title>, 1s budget. URL-as-fallback on any failure."""

from __future__ import annotations

import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 1.0
MAX_BYTES = 32 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


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
        with client.stream(
            "GET", url, headers={"User-Agent": "zotero-provenance/0.1"}
        ) as resp:
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
