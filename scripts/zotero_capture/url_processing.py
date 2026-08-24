"""URL extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import ipaddress
import re
import socket

import idna
from markdown_it import MarkdownIt
from urllib.parse import unquote_plus, urlsplit, urlunsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# What a URL may contain is decided by the grammar, not by a list of characters
# someone remembered to exclude. This was a blacklist — [^\s<>"'`\]]+ — so every
# character nobody had thought of was taken as URL data: a trailing "|" from an
# unpadded table cell, "{ID}" from a template, a raw ANSI escape from pasted
# terminal output. Those addresses can never resolve a title, so they decay into
# the URL-as-title junk this plugin exists to remove. Lengthening TRAILING_PUNCT
# does not fix it: that is the consumer of the bad boundary, not its producer,
# and it only ever sees the trailing position.
#
# Two deliberate departures from RFC 3986, both narrowing:
#
#   "[" and "]" are gen-delims, but legal only inside an IPv6 host — which is
#   matched by its own branch first and keeps its brackets. Admitting them to
#   the general branch would let a "filter[name]" template through.
#
#   "'" is a sub-delimiter and therefore legal, but in 1,370 real assistant
#   messages all 7 apostrophes adjacent to a URL were delimiters — a shell
#   quote, a Python string, an English possessive — and none was URL data. A
#   URL that genuinely needs one writes %27.
#
# Non-ASCII is admitted (RFC 3987): a real URL may carry UTF-8 unencoded, and a
# strict-ASCII class truncated ".../wiki/München" to ".../wiki/M" — the same
# damage as the "]" truncation that once left every IPv6 URL as "https://[::1".
#
# A closing paren stays in: Cell Press PII links and the DOIs behind them carry
# one (10.1016/s0092-8674(00)80876-3), as do Wikipedia disambiguation pages.
# Whether a trailing one belongs to the URL or to the prose is the balance rule's
# call in extract_urls, and excluding it here would pre-empt that rule.
#
# Measured over those 1,370 messages / 4,009 extracted URLs, the switch from
# blacklist to whitelist changes nothing: every illegal character in that corpus
# already sat inside a code span or fence, which the AST skips. It removes the
# mechanism, not a measured defect rate. (A variant admitting a space, used as
# the control, changed 218 of the messages.)
_URL_CHARS = (
    "A-Za-z0-9"  # unreserved: ALPHA / DIGIT
    r"\-._~"  # unreserved: the rest
    "!$&()*+,;="  # sub-delims, less "'"
    ":/?#@"  # gen-delims that may follow the authority
    "%"  # pct-encoded
    "\u00a0-\U0010ffff"  # RFC 3987, above the C0/C1 control blocks
)
URL_RE = re.compile(
    rf"https?://\[[0-9A-Fa-f:.]+\](?::\d+)?[{_URL_CHARS}]*"
    rf"|https?://[{_URL_CHARS}]+",
    re.IGNORECASE,
)

# One legal URL character, for asking why a match stopped where it did.
_URL_CHAR_RE = re.compile(f"[{_URL_CHARS}]")

# Illegal characters that can only CLOSE something, so a match ending at one is
# a URL that was wrapped, not a URL cut in half. See _stopped_mid_literal.
_LITERAL_CLOSERS = "]}>\"'`"

# Strict CommonMark: no linkification of bare URLs, so a URL in prose stays in a
# text token and URL_RE still has a job. What the parser buys is that the text it
# hands over has already had the markdown taken out of it.
_MD = MarkdownIt("commonmark")

# Inline tokens that are showing a literal rather than citing a source. Block
# tokens need no list: only "inline" tokens are walked, and a fence, an indented
# block and an HTML block are all block-level.
_UNCITED_INLINE = frozenset({"code_inline", "html_inline"})

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

    Byte-preserving except for the four things it is asked to change. It used to
    undo one layer of "&amp;" escaping here as well, so that a URL lifted out of
    rendered markup deduped against the same source written plainly. That belongs
    to the parser, not here: a literal "&amp;" is legal URL data, and nothing at
    this layer can tell the escaped separator from the data. CommonMark already
    settles it — a link destination has its entity references decoded, an
    autolink's content does not — so extract_urls hands over a URL that has
    already been decoded exactly as much as it should be.
    """
    parts = urlsplit(raw.strip())
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
    return urlunsplit(
        (parts.scheme.lower(), netloc, path, _drop_tracking_params(parts.query), "")
    )


def _drop_tracking_params(query: str) -> str:
    """Remove tracking fields, leaving every surviving byte exactly as it came.

    Splitting on "&" and rejoining rather than parse_qsl + urlencode, because
    that round trip rewrites what it was not asked to touch: a valueless field
    gains an "=", percent-encoding is normalised, and "+" is reinterpreted as a
    space. A signed or opaque query does not survive any of those, and the
    result names a resource nobody cited. Only the field NAME is decoded, and
    only far enough to decide whether it matches.
    """
    if not query:
        return ""
    kept = [
        field
        for field in query.split("&")
        if unquote_plus(field.split("=", 1)[0]).lower() not in TRACKING_PARAMS
    ]
    return "&".join(kept)


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

    A parser draws the line rather than a scan over the raw text, because the
    scan could not say where a URL *ended*. It trimmed trailing punctuation from
    markdown it had never parsed, so `**[text](url)**` kept its emphasis — `*`
    is in no trim set, and by ending the URL in `*` it also stopped the
    paren-balance rule from firing. That mangled 10.72% of URLs in real traffic
    against 0.14% lost to the container bugs, and left 116 of 4,893 live rows
    unable to ever resolve a title. Lengthening the trim set is not the fix:
    `*` and `_` are legal URL characters (RFC 3986), so trimming them corrupts
    real URLs. Only a parse can tell markdown punctuation from URL data.

    Dedups preserving order. A link destination is taken exactly as the parser
    reports it; only a bare URL recovered from prose is trimmed, and then only
    of sentence punctuation, which is genuinely ambiguous in a way markdown is
    not.
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def emit(url: str) -> None:
        if url and url not in seen_set:
            seen.append(url)
            seen_set.add(url)

    for token in _MD.parse(text):
        # Only inline tokens carry citable text. A fence, an indented code block
        # and an HTML block are block-level and simply never appear here.
        if token.type != "inline":
            continue
        in_link = 0
        for child in token.children or []:
            if child.type == "link_open":
                in_link += 1
                href = child.attrGet("href") or ""
                if href.lower().startswith(("http://", "https://")):
                    emit(href)
            elif child.type == "link_close":
                in_link = max(0, in_link - 1)
            elif child.type == "image":
                # A badge or screenshot is a page asset, not a cited source, and
                # its alt text is not prose that cites anything either.
                continue
            elif child.type in _UNCITED_INLINE:
                continue
            elif child.type == "text" and not in_link:
                # Inside a link the label is decoration — `[displayed](cited)`
                # cites only the destination, which link_open already emitted.
                for match in URL_RE.finditer(child.content):
                    if _stopped_mid_literal(child.content, match.end()):
                        continue
                    emit(_trim_prose_url(match.group(0)))
    return seen


def _stopped_mid_literal(text: str, end: int) -> bool:
    """True when the match ended inside a template rather than at the URL's end.

    Narrowing the tokenizer to the URI grammar changed how a template fails. A
    prose "https://files.rcsb.org/download/{ID}.pdb" used to be stored whole:
    no exclusion rule caught it, but it could never resolve, so it failed loudly
    as the URL-as-title junk this plugin removes. Stopping at "{" instead stores
    "https://files.rcsb.org/download/" — a real, fetchable directory that will
    acquire a genuine title and read as a citation nobody made. Quiet wrong data
    is worse than loud junk, so the prefix is dropped rather than kept.

    The signal is that URL text RESUMES after the illegal character: "{" followed
    by "ID}.pdb" means the run was one literal. Whitespace is never suspicious —
    it is how a URL normally ends.

    A *closing* delimiter is not suspicious either, and the distinction is not a
    taste call: a closer can only appear after the thing it closes, so the URL
    had already ended. Without that, "[https://example.org/bar]." lost a real
    citation — the match stops at "]", a legal "." follows, and the sentence
    period reads as resumed URL text. An *opening* brace or a separator has no
    such reading; the run simply continues.
    """
    if end + 1 >= len(text):
        return False
    stopper = text[end]
    if stopper.isspace() or stopper in _LITERAL_CLOSERS:
        return False
    return _URL_CHAR_RE.match(text[end + 1]) is not None


def _trim_prose_url(url: str) -> str:
    """Strip sentence punctuation from a URL recovered from running prose.

    Applies ONLY to bare URLs in text. A link destination arrives with its
    extent already decided by the parser, and trimming it would corrupt a
    destination that legitimately ends in one of these characters.
    """
    url = url.rstrip(TRAILING_PUNCT)
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


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
