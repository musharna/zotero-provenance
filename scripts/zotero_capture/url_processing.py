"""URL extraction, canonicalization, exclusion (spec D7 + Section 4)."""

from __future__ import annotations

import ipaddress
import re
import socket

import idna
from linkify_it import LinkifyIt
from markdown_it import MarkdownIt
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Where a bare URL ENDS in prose is decided by linkify-it-py, not by a character
# class of ours. Three hand-rolled attempts at that boundary each shipped a
# silent corruption, found by an external audit of 0.11.5:
#
#   "…/wiki/People's_Republic_of_China" was stored as "…/wiki/People" — a
#   DIFFERENT real Wikipedia page, so it resolved a plausible title and looked
#   fine. The apostrophe is a legal sub-delimiter; excluding it on the evidence
#   of seven corpus observations was not enough against a live counterexample.
#
#   "{{ID}}.pdb" defeated the continuation guard, because that guard looked at
#   exactly one character past the illegal one and the next character was also
#   illegal.
#
#   A curly quote, an em dash and U+00A0 were all absorbed into the address —
#   the IRI range began AT the non-breaking space, so a match could cross a
#   visible word boundary.
#
# linkify-it-py is markdown-it-py's own linkifier and has the boundary rules
# that a decade of real prose produced. Matches are sliced out of the ORIGINAL
# text rather than read from the token href: going through markdown-it would
# percent-encode the result ("München" -> "M%C3%BCnchen"), which changes the
# dedup key and would duplicate every non-ASCII row already in the index.
#
# The RFC grammar keeps a job, but a different one — it VALIDATES what linkify
# delimited instead of deciding the extent. That ordering is what makes repair
# and capture agree, since both now ask the same question of a finished URL.
_URL_CHARS = (
    "A-Za-z0-9"  # unreserved: ALPHA / DIGIT
    r"\-._~"  # unreserved: the rest
    "!$&'()*+,;="  # sub-delims, apostrophe included: RFC 3986 says it is data
    ":/?#@"  # gen-delims that may follow the authority
    "%"  # pct-encoded
    "\u00a1-\U0010ffff"  # RFC 3987, above the C1 block AND above U+00A0
)
_URL_CHAR_RE = re.compile(f"[{_URL_CHARS}]")

# linkify cannot see a bracketed IPv6 literal at all — it returns no match for
# "https://[2001:db8::1]:8443/x" — so that form keeps its own pattern and is
# taken out of the text before linkify runs.
IPV6_URL_RE = re.compile(
    rf"https?://\[[0-9A-Fa-f:.]+\](?::\d+)?[{_URL_CHARS}]*", re.IGNORECASE
)

# Kept for callers and tests that still ask "what shape is a URL": it is no
# longer the tokenizer.
URL_RE = re.compile(rf"{IPV6_URL_RE.pattern}|https?://[{_URL_CHARS}]+", re.IGNORECASE)

# Terminal output pasted into a message carries escape sequences, and they are
# not URL data: "https://example.org/a\x1b[31mcontinued" is one address wearing a
# colour code, not an address that ends at "a". Removing them reconstructs the
# visible URL instead of truncating it. CSI, OSC (including OSC 8 hyperlinks) and
# the two-byte escapes are all covered.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences so they cannot end a URL early."""
    return _ANSI_RE.sub("", text)


_LINKIFY = LinkifyIt()
# Only real schemes. Fuzzy matching would linkify "example.com" and bare emails,
# which are not citations and would flood the collection.
_LINKIFY.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})

# A closer is evidence that the URL ended only when its OPENER sits immediately
# in front of the match. An unmatched quote or bracket proves nothing, which is
# why the previous unconditional closer list mis-handled
# "https://example.org/path}suffix".
_QUOTE_PAIRS = {
    "'": "'",
    '"': '"',
    "\u2018": "\u2019",
    "\u201c": "\u201d",
    "\u00ab": "\u00bb",
    "\u300c": "\u300d",
}

# A brace never appears unencoded in a real address; it means a template. Such a
# row can never resolve, and — worse — cutting it at the brace manufactures a
# parent directory that CAN resolve and reads as a citation nobody made.
_TEMPLATE_RE = re.compile(r"[{}]")

_MD = MarkdownIt("commonmark")

# Inline tokens that are showing a literal rather than citing a source. Block
# tokens need no list: only "inline" tokens are walked, and a fence, an indented
# block and an HTML block are all block-level.
_UNCITED_INLINE = frozenset({"code_inline"})

# href from a raw HTML anchor. Deliberately narrow: only <a href=...>, only
# quoted, because a bare attribute value has no reliable end in a fragment.
_HTML_HREF_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

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
    raw = raw.strip()
    if not _has_usable_authority(raw):
        # Total by construction. urlsplit().port raises on an impossible port,
        # and this function is called from a loop that processes a whole
        # message: one bad URL must cost that URL, not the turn. Returned
        # unchanged rather than repaired, because there is nothing to repair to
        # — is_storable_url() refuses it moments later.
        return raw
    parts = urlsplit(raw)
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
                    if _destination_is_citable(href):
                        emit(href)
            elif child.type == "link_close":
                in_link = max(0, in_link - 1)
            elif child.type == "image":
                # A badge or screenshot is a page asset, not a cited source, and
                # its alt text is not prose that cites anything either.
                continue
            elif child.type == "html_inline":
                # An anchor RENDERS as a hyperlink, so it cites a source; it is
                # not a literal the way a code span is. Only the href is taken —
                # the tag's other attributes are markup, not citations.
                for href in _HTML_HREF_RE.findall(child.content):
                    if href.lower().startswith(("http://", "https://")):
                        if _destination_is_citable(href):
                            emit(href)
            elif child.type in _UNCITED_INLINE:
                continue
            elif child.type == "text" and not in_link:
                # Inside a link the label is decoration — `[displayed](cited)`
                # cites only the destination, which link_open already emitted.
                for url in bare_urls(child.content):
                    emit(url)
    return seen


def bare_urls(text: str) -> list[str]:
    """Every URL this text CITES in prose, with its extent decided by linkify.

    Order matters. A bracketed IPv6 literal is taken first and blanked out,
    because linkify does not recognise that form at all and would otherwise
    leave the URL uncaptured — the same class of loss as the "]" truncation that
    once stored every IPv6 URL as "https://[::1".

    Each match is then sliced out of the ORIGINAL text, so nothing is
    re-encoded, and put through three steps that linkify does not do:
    a paired closing quote is dropped, sentence punctuation is trimmed, and the
    result must pass the URI grammar or it is discarded rather than stored.
    """
    text = strip_ansi(text)
    found: list[str] = []
    masked = list(text)
    for match in IPV6_URL_RE.finditer(text):
        found.append(_trim_prose_url(match.group(0)))
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    scan = "".join(masked)

    for match in _LINKIFY.match(scan) or []:
        raw = scan[match.index : match.last_index]
        if not raw.lower().startswith(("http://", "https://")):
            continue
        # A brace touching the boundary means linkify stopped INSIDE a template,
        # so the match is a prefix of a literal rather than an address. Keeping
        # it would store "https://example.org/path" out of "…/path}suffix" — a
        # shorter URL that resolves and that nobody cited. This is deliberately
        # about braces and not about closers in general: an unmatched "]" or
        # quote proves nothing, which is why the old unconditional closer list
        # was wrong, but a brace is never URL data in any position.
        if _TEMPLATE_RE.match(scan[match.last_index : match.last_index + 1] or ""):
            continue
        # Same reasoning, different character. linkify balances parentheses, so
        # a URL holding an UNBALANCED "(" is cut at it — and "(" is a legal
        # sub-delimiter, so what gets stored is a silent truncation that often
        # still resolves. The corpus has one:
        # ".../File:Hericium_erinaceus_(Bearded_Tooth..." was stored as
        # ".../File:Hericium_erinaceus_". A prefix of an address is not the
        # address, and inventing one is the harm the template rule exists to
        # stop. Note this fires only when "(" IMMEDIATELY follows the match:
        # "see (https://example.org/foo)" ends on a space and is untouched.
        if scan[match.last_index : match.last_index + 1] == "(":
            continue
        raw = _strip_paired_closer(scan, match.index, raw)
        raw = _trim_prose_url(strip_illegal_tail(raw))
        if is_storable_url(raw):
            found.append(raw)
    return found


def strip_illegal_tail(url: str) -> str:
    """Drop trailing characters the URI grammar does not permit unencoded.

    linkify decides where the run of URL-ish text ends; it does not promise that
    every character in it is legal. A pipe from a table cell or a stray backtick
    can survive at the end, and those are punctuation, not address. Only the TAIL
    is touched — an illegal character in the middle means the whole thing is a
    literal, which is_storable_url() then refuses outright rather than truncating
    into an address nobody cited.
    """
    while url and _URL_CHAR_RE.match(url[-1]) is None:
        url = url[:-1]
    return url


_VCS_REQUIREMENT_RE = re.compile(r"\.git@[^/]+$")


def is_vcs_requirement(url: str) -> bool:
    """True when this is an install specifier rather than a web address.

    `https://github.com/owner/repo.git@<ref>` is what a person pastes from a
    `pip install git+...` line. It names a real repository at a real commit, but
    it is not a page: fetching it 404s because the PATH is wrong, not because
    anything is missing. Stamping such a row `gone` reports link rot about a
    repository that is very much alive.

    Deliberately NOT repaired into the repo root. Stripping `.git@<ref>` would
    produce an address that resolves, but it is not the one that was cited --
    the commit pin is the whole point of a requirement line, and inventing a
    different URL that happens to work is the same error as appending a closing
    paren. `malformed` says the true thing: we never asked a well-formed
    question, so we learned nothing about the source.
    """
    return bool(_VCS_REQUIREMENT_RE.search(url))


def unbalanced_brackets(url: str) -> bool:
    """True when this address's parentheses or square brackets do not balance.

    A stored URL that fails this is very likely a PREFIX of the one that was
    cited, not the address itself. Until 2026-08-21 the tokenizer was a
    character blacklist -- `[^\\s<>"\'`\\)\\]]+` -- so every cited URL holding a
    legal `)` or `]` was written to the index cut off at it. 34 live rows are
    still in that state, and `snapshot` was stamping them `gone`: an affirmative
    claim that a source no longer exists, manufactured out of our own damage.

    Depth is counted, not merely paired. A closer standing before its opener is
    damage too, and a rule that only compared totals would call "…/a)b(c" clean.

    Percent-encoded octets are not brackets, so `%28` is correctly ignored --
    only literal characters are counted.

    This is a SUSPICION and not a proof, deliberately. `(` is a legal
    sub-delimiter, so "https://example.org/a(b" is a real address that this
    calls suspect. That error costs one downgraded claim about one source. The
    opposite error prints link rot that never happened, which is the exact
    finding this tool exists to report truthfully.
    """
    for opener, closer in (("(", ")"), ("[", "]")):
        depth = 0
        for ch in url:
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth < 0:
                    return True
        if depth != 0:
            return True
    return False


def _strip_paired_closer(text: str, index: int, url: str) -> str:
    """Drop a closing quote whose OPENER sits immediately before the match.

    Pairing is the whole test. An unmatched quote or bracket is not evidence
    that a URL ended — treating one as evidence unconditionally is what made
    "https://example.org/path}suffix" resolve to the prefix. But when the text
    reads 'https://…/People\'s_Republic_of_China', the leading quote proves the
    trailing one is syntax, and the apostrophe in the middle is data.
    """
    opener = text[index - 1] if index > 0 else ""
    closer = _QUOTE_PAIRS.get(opener)
    if closer and url.endswith(closer) and len(url) > 1:
        return url[:-1]
    return url


def is_storable_url(url: str) -> bool:
    """True when this is an address worth keeping, by the URI grammar.

    Capture and repair both ask this, which is the point: repair used to emit
    URLs the tokenizer would have rejected, so a "successful" repair could store
    something capture would never have accepted.

    A brace is refused outright rather than trimmed. It means a template, and
    cutting "…/download/{ID}.pdb" at the brace manufactures "…/download/" — a
    real, fetchable directory that acquires a genuine title and reads as a
    citation nobody made. Loud absence beats quiet invention.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    if _TEMPLATE_RE.search(url):
        return False
    if not _has_usable_authority(url):
        return False
    rest = url.split("://", 1)[1]
    if not rest:
        return False
    # The IPv6 form carries the only brackets a URL may hold unencoded.
    if rest.startswith("["):
        rest = rest.partition("]")[2]
    return not any(_URL_CHAR_RE.match(ch) is None for ch in rest)


def _has_usable_authority(url: str) -> bool:
    """Whether the authority parses at all.

    `urlsplit().port` RAISES for a port outside 0-65535 rather than returning
    None. capture canonicalises before its per-URL guard, so one such link —
    "[bad](https://example.com:99999/path)" — aborted the whole message and took
    every real citation in that turn with it. Asking the question here turns a
    thrown exception into an ordinary refusal of one URL.
    """
    try:
        urlsplit(url).port
    except ValueError:
        return False
    return True


def _destination_is_citable(href: str) -> bool:
    """Whether a parser-supplied link destination may be stored.

    A destination does NOT arrive the way prose does: CommonMark percent-encodes
    it, so "{ID}" reaches us as "%7BID%7D" and the template rule — which looks
    for a literal brace — sees nothing wrong. That is how
    "files.rcsb.org/download/%7BID%7D.pdb" entered the live library: a fetchable
    directory listing nobody cited, stored while every guard reported success.

    So the brace test is applied to the DECODED form, and the ordinary grammar
    test to the destination as written.
    """
    if _TEMPLATE_RE.search(unquote(href)):
        return False
    return is_storable_url(href)


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


# A DOI is a registrant prefix "10." plus four or more digits, then "/", then a
# suffix that must not be empty. Anything else on doi.org names no document.
DOI_PATH_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _is_asset_path(path: str) -> bool:
    """True when the path points at an asset rather than a page.

    Tests the path alone, so a query string like `?ref=x.css` cannot smuggle an
    extension past the check.
    """
    if PAGE_PATH_RE.search(path):
        return False
    return path.lower().endswith(ASSET_EXTENSIONS)


def _is_malformed_doi(host: str, path: str) -> bool:
    """A doi.org URL whose path is not a DOI identifies no document.

    The live library held four: `10.1/ABC` and `10.x` (placeholders someone
    typed in prose), `GSE12345` (a GEO accession given a doi.org prefix by
    mistake), and a bare `…` — an ellipsis the extractor lifted out of truncated
    text and stored as a source.

    The syntax is the whole check and it is not a guess: a DOI is a registrant
    prefix `10.` followed by four or more digits, then `/`, then a suffix. This
    rejects only strings that cannot be a DOI at all, so it never has to ask
    whether a well-formed one resolves — `doi.org` answers that, and a valid DOI
    that 404s is a dead source rather than a malformed one.
    """
    if host not in ("doi.org", "dx.doi.org"):
        return False
    return not DOI_PATH_RE.match((path or "").lstrip("/"))


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
    if _is_malformed_doi(host, parts.path or ""):
        return True
    ip = parse_ip_literal(host)
    if ip is None:
        # Not an address, so it has to be a name a resolver could look up —
        # and a name with no dot in it can only be looked up from inside a
        # network that already knows it, so it cannot identify a document
        # anyone else can read. 19 rows in the live index had one and not one
        # was a source: "prometheus:9090", "homelab:3000", Ollama on
        # "host:11434", a machine name, and this project's own test fixtures.
        #
        # This runs AFTER the IP literal is ruled out, deliberately. A bracketed
        # IPv6 host has no dot either, and catching it here would exclude every
        # IPv6 URL — the same damage as the "]" truncation that once stored them
        # all as an unparseable "https://[::1".
        if "." not in host:
            return True
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
