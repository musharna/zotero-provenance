"""URL extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import ipaddress
import re

import idna
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# A closing paren is a legal URL character — Cell Press PII links and the DOIs
# behind them carry one (10.1016/s0092-8674(00)80876-3), as do Wikipedia
# disambiguation pages. Admit it here and let the balance rule in extract_urls
# decide whether a trailing one belongs to the URL or to the prose around it;
# excluding it at the tokenizer truncates the URL before that rule can run.
URL_RE = re.compile(r"https?://[^\s<>\"'`\]]+", re.IGNORECASE)

# ")" is deliberately absent: it is the balance rule's to judge, and stripping it
# here unconditionally would pre-empt that and corrupt a legitimate URL.
TRAILING_PUNCT = ".,;:]}>"

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

# Names the standards guarantee will never resolve to anything real, from the
# IANA Special-Use Domain Names registry (RFC 6761, RFC 2606, RFC 6762, RFC 7686,
# RFC 9476) plus .internal, which ICANN reserved for private use in 2024.
#
# This is the same class as the localhost and private-IP rules above: an address
# that cannot be a source. It earns its place because fixture URLs are the
# ecosystem's default use for these names — another project's security fixtures,
# echoed into a session, landed in the citation library as real items.
RESERVED_TLDS = frozenset(
    {
        "test",
        "example",
        "invalid",
        "localhost",
        "local",  # mDNS (RFC 6762)
        "onion",  # Tor; unreachable without a Tor proxy (RFC 7686)
        "alt",  # non-DNS namespaces (RFC 9476)
        "arpa",  # infrastructure only, never a document
        "internal",  # ICANN private-use delegation, 2024
    }
)

# RFC 2606 reserved these second-level names for documentation specifically.
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})

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

# idna is stricter than the resolver about a few plain-ASCII names (a lone
# underscore label, an over-long one). Those still resolve, so keep them.
_ASCII_HOST_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?")

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


def _is_reserved_name(host: str) -> bool:
    """True for a name the standards reserve, so it can never be a real source.

    Matches on label boundaries, never as a substring: "myexample.com" and
    "example.com.evil.co" are ordinary registrable hosts and must survive.
    """
    if host.rpartition(".")[2] in RESERVED_TLDS:
        return True
    return any(
        host == name or host.endswith(f".{name}") for name in RESERVED_DOMAINS
    )


def _is_real_hostname(host: str) -> bool:
    """True when `host` is something a resolver could actually look up.

    A display ellipsis reached the library as `https://\u2026` and then raised
    "Invalid IDNA hostname" on every fetch for the rest of its life. IDNA is the
    right test rather than a character whitelist, because it is what the HTTP
    client itself applies — and it keeps genuine internationalised domains
    (m\u00fcnchen.de) which a naive ASCII rule would wrongly drop.
    """
    try:
        idna.encode(host, uts46=True)
    except Exception:
        return _ASCII_HOST_RE.fullmatch(host) is not None
    return True


def _is_asset_path(path: str) -> bool:
    """True when the path points at an asset rather than a page.

    Tests the path alone, so a query string like `?ref=x.css` cannot smuggle an
    extension past the check.
    """
    if PAGE_PATH_RE.search(path):
        return False
    return path.lower().endswith(ASSET_EXTENSIONS)


def is_excluded(url: str) -> bool:
    """Drop localhost, tailnet, private-IP, reserved-name, infrastructure and asset URLs."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return True
    if host in EXCLUDE_HOSTS_EXACT or host in EXCLUDE_INFRA_HOSTS:
        return True
    if host.endswith(TS_NET_SUFFIX):
        return True
    if _is_reserved_name(host):
        return True
    if _is_asset_path(parts.path or ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an address, so it has to be a name a resolver could look up.
        return not _is_real_hostname(host)
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return True
    return any(ip in net for net in PRIVATE_RANGES)
