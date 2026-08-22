"""URL extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import ipaddress
import re
import socket

import idna
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# A closing paren is a legal URL character — Cell Press PII links and the DOIs
# behind them carry one (10.1016/s0092-8674(00)80876-3), as do Wikipedia
# disambiguation pages. Admit it here and let the balance rule in extract_urls
# decide whether a trailing one belongs to the URL or to the prose around it;
# excluding it at the tokenizer truncates the URL before that rule can run.
# A bracketed IPv6 literal is matched first and keeps its brackets: the general
# branch stops at "]", which silently truncated every IPv6 URL to an unparseable
# "https://[::1" that then raised for the rest of the item's life.
URL_RE = re.compile(
    r"https?://\[[0-9A-Fa-f:.]+\](?::\d+)?[^\s<>\"'`\]]*"
    r"|https?://[^\s<>\"'`\]]+",
    re.IGNORECASE,
)

# CommonMark fence rules, which the first cut of this did not implement. A fence
# opens on a run of 3+ backticks or tildes indented at most 3 spaces; it closes
# only on the SAME character, a run at least as long, and nothing but whitespace
# after it. Getting the close wrong is the dangerous direction: a fence that
# never closes swallows every citation in the rest of the message.
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

# A fence may sit inside a blockquote, where every line carries a "> " prefix.
BLOCKQUOTE_RE = re.compile(r"^ {0,3}(?:> ?)+")

# Runs of backticks, paired by EQUAL length per CommonMark, so ``code`` is one
# span rather than two empty ones with a URL stranded between them.
TICK_RUN_RE = re.compile(r"`+")

# Deliberately not html.unescape: see canonicalize. One layer per pass is enough,
# since each re-print adds exactly one.
AMP_ENTITY_RE = re.compile(r"&amp;", re.IGNORECASE)

# This plugin's own reports list URLs it already holds, and the Stop hook reads
# Claude's output — so displaying a report re-captured everything in it, stamping
# today's seen: tag onto the sources it was reporting as dropped. Reports carry
# this marker and capture skips the whole message.
#
# It backs up the structural rule rather than replacing it: a report reformatted
# on the way out loses its backticks, one that gets summarised loses its marker.
# Neither layer is sufficient alone. Lives here because this module owns the
# definition of what counts as citable.
NO_CAPTURE_MARKER = "<!-- zotero-provenance: generated report, not citations -->"

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
    """Apply spec D7: lowercase host, strip fragment, strip trailing slash, drop tracking params.

    One layer of "&amp;" escaping is undone first. A URL lifted out of rendered
    markup carries that page's escaping, so "?a=1&b=2" arrives as "?a=1&amp;b=2"
    — a different string for the same source, which misses the dedup lookup and
    creates a second item. Each round of escaping compounds (&amp; -> &amp;amp;),
    so without this the duplicates never converge.

    Only "&amp;" is decoded, never the full entity table. That table contains the
    URL's own delimiters: "&sol;" is "/", "&num;" is "#", "&quest;" is "?", so
    decoding everything lets a path segment forge a separator, or invent a
    fragment that the next line then discards — silently storing a different
    resource than the one cited. "&amp;" cannot do that, and it is the only
    entity a URL acquires merely by being written into HTML.
    """
    parts = urlsplit(AMP_ENTITY_RE.sub("&", raw.strip()))
    host = parts.hostname or ""
    netloc = host.lower()
    # urlsplit strips the brackets off an IPv6 literal, and putting the bare
    # address back produces a netloc whose colons read as a port separator.
    if ":" in netloc:
        netloc = f"[{netloc}]"
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
    """Extract the URLs a message CITES, skipping those it merely displays.

    Markdown already draws this line: a fence or a backtick means "this is a
    literal being shown". The distinction is load-bearing because the capture
    hook reads the agent's own output — an audit that printed a malformed URL
    used to re-capture it, one escape layer deeper each pass, without bound.

    Measured over 260 assistant messages, 96% of real citations arrive as
    markdown links or bare prose; the backticked form is 2.9% and is mostly
    internal hostnames and API endpoints. Indentation is deliberately NOT a
    signal — an indented line is usually a list item, not a code block.

    Trims punctuation and dedups, preserving order.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for chunk in _prose_paragraphs(text):
        for match in URL_RE.finditer(_mask_code_spans(chunk)):
            url = match.group(0).rstrip(TRAILING_PUNCT)
            while url.endswith(")") and url.count("(") < url.count(")"):
                url = url[:-1]
            if url not in seen_set:
                seen.append(url)
                seen_set.add(url)
    return seen


def _prose_paragraphs(text: str) -> list[str]:
    """Drop fenced blocks, then split what is left on blank lines.

    Splitting into paragraphs bounds the damage an unpaired backtick can do. A
    code span cannot cross a blank line in CommonMark, so pairing runs only
    within a paragraph stops a single stray tick from masking — and silently
    discarding — every citation in the rest of the message.
    """
    out: list[str] = []
    current: list[str] = []
    fence: tuple[str, int] | None = None
    for raw_line in text.split("\n"):
        line = BLOCKQUOTE_RE.sub("", raw_line)
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = (match.group(1)[0], len(match.group(1)))
                continue
            if line.strip():
                current.append(line)
            elif current:
                out.append("\n".join(current))
                current = []
            continue
        if not match:
            continue
        char, length = fence
        run = match.group(1)
        # Closes only on the same character, at least as long, nothing trailing.
        if run[0] == char and len(run) >= length and not match.group(2).strip():
            fence = None
    if current:
        out.append("\n".join(current))
    return out


def _mask_code_spans(text: str) -> str:
    """Blank out backtick-delimited spans, pairing runs of EQUAL length.

    Blanking rather than deleting keeps every other offset intact, so the
    trailing-paren balance rule still sees the prose exactly where it sat. An
    unpaired run is left alone: a lone backtick is prose, not an open span.
    """
    runs = [(m.start(), m.end()) for m in TICK_RUN_RE.finditer(text)]
    if not runs:
        return text
    chars = list(text)
    i = 0
    while i < len(runs):
        start, end = runs[i]
        width = end - start
        closer = next(
            (j for j in range(i + 1, len(runs)) if runs[j][1] - runs[j][0] == width),
            None,
        )
        if closer is None:
            i += 1
            continue
        for pos in range(start, runs[closer][1]):
            chars[pos] = " "
        i = closer + 1
    return "".join(chars)


def _is_reserved_name(host: str) -> bool:
    """True for a name the standards reserve, so it can never be a real source.

    Matches on label boundaries, never as a substring: "myexample.com" and
    "example.com.evil.co" are ordinary registrable hosts and must survive.
    """
    if host.rpartition(".")[2] in RESERVED_TLDS:
        return True
    return any(host == name or host.endswith(f".{name}") for name in RESERVED_DOMAINS)


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
    ip = parse_ip_literal(host)
    if ip is None:
        # Not an address, so it has to be a name a resolver could look up.
        return not _is_real_hostname(host)
    return is_unsafe_address(ip)


def parse_ip_literal(host: str) -> IPAddress | None:
    """Parse a host as an IP in any notation a resolver would accept, else None.

    `ipaddress.ip_address` only understands the dotted-quad spelling, but
    `inet_aton` — and therefore every HTTP client — also accepts decimal, hex,
    octal and short forms. `2130706433`, `0x7f000001`, `017700000001` and
    `127.1` are all 127.0.0.1, and all four used to sail past a check that only
    knew what loopback looks like written out.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None


def is_unsafe_address(ip: IPAddress) -> bool:
    """True for any address a fetch must never reach.

    Deliberately broader than "private": link-local carries the cloud metadata
    endpoint, and reserved/unspecified ranges have no business being a source.
    """
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and is_unsafe_address(mapped):
        return True
    return any(ip in net for net in PRIVATE_RANGES)
