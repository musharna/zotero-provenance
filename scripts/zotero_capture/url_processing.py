"""URL extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"'`\)\]]+", re.IGNORECASE)
TRAILING_PUNCT = ".,;:)]}>"

TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
    }
)

EXCLUDE_HOSTS_EXACT = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})
TS_NET_SUFFIX = ".ts.net"

# Infrastructure a page pulled in, never a source anyone cited: font CDNs,
# DNS-over-HTTPS endpoints, analytics beacons. These can never resolve to a
# title, so without this they accumulate in the collection permanently.
EXCLUDE_INFRA_HOSTS = frozenset(
    {
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cloudflare-dns.com",
        "mozilla.cloudflare-dns.com",
        "dns.google",
        "static.cloudflareinsights.com",
    }
)

# The bytes a page references rather than the page itself.
ASSET_EXTENSIONS = (
    ".css",
    ".js",
    ".mjs",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".map",
)

# Paths that are HTML pages despite ending in an asset extension — a GitHub blob
# view, a workflow badge, a Wikimedia File: description page. Each of these
# resolves to a real title, so the extension test must not claim them.
PAGE_PATH_RE = re.compile(r"/(wiki|blob|tree|releases|actions)/", re.IGNORECASE)

PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
]


def canonicalize(raw: str) -> str:
    """Apply spec D7: lowercase host, strip fragment, strip trailing slash, drop tracking params."""
    parts = urlsplit(raw.strip())
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        userinfo = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{userinfo}@{netloc}"
    path = parts.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    elif path == "/":
        path = ""
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(query_pairs)
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def extract_urls(text: str) -> list[str]:
    """Extract URLs from text (assistant message). Trim punctuation; dedup preserving order."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCT)
        while url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        if url not in seen_set:
            seen.append(url)
            seen_set.add(url)
    return seen


def _is_asset_path(path: str) -> bool:
    """True when the path points at an asset rather than a page.

    Tests the path alone, so a query string like `?ref=x.css` cannot smuggle an
    extension past the check.
    """
    if PAGE_PATH_RE.search(path):
        return False
    return path.lower().endswith(ASSET_EXTENSIONS)


def is_excluded(url: str) -> bool:
    """Drop localhost, tailnet, private-IP, infrastructure and asset URLs."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return True
    if host in EXCLUDE_HOSTS_EXACT or host in EXCLUDE_INFRA_HOSTS:
        return True
    if host.endswith(TS_NET_SUFFIX):
        return True
    if _is_asset_path(parts.path or ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return True
    return any(ip in net for net in PRIVATE_RANGES)
