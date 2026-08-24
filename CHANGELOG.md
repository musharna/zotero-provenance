# Changelog

## 0.11.5 — 2026-08-23

- **A title behind a large inline script is no longer missed.** The reader
  stopped at 32 KiB. experian.com serves 200 with a perfectly good ``<title>`` —
  at byte 167,895, behind a long inline script — so three rows in the live index
  stored their URL as the title, which is precisely the junk this plugin exists
  to remove. The cap was the cause and the 1s budget was not: 32 KiB arrived in
  0.49s and the whole 271 KB page in 0.63s.

  The cap is now 256 KiB, and the read stops the moment ``</title>`` arrives, so
  an ordinary page still reads about a kilobyte and pays nothing for the higher
  ceiling. A blown deadline now *stops* the read instead of discarding it —
  without that, raising the cap would have made things worse for a slow page,
  which used to stop at 32 KiB with a title in hand and would instead have
  streamed past the clock and thrown it away.

  Verified against the live sites: experian.com/help/credit-freeze,
  experian.com/protection/creditlock and a Yahoo Finance article all resolve
  now; myaccount.google.com correctly does not, since it needs a login.

  The existing cap test was written against a literal 32 KiB and would have
  silently become a test of nothing. It is written against ``MAX_BYTES`` now.

### Triage of what remains

16 items were left carrying ``title:unresolved``. Fetching every one of them,
against a positive control so that "they all failed" could not be the harness
lying, gives four causes and only one was ours:

- the read cap above (fixed);
- HTTP 404 — a Fortune article that is gone, a private repo's pull request, and
  a Wikipedia page that never existed (a test fixture);
- authentication — Google account pages cannot resolve without a login;
- a broken certificate chain on ``mirror.oit.ncsu.edu``, which fails identically
  under plain ``httpx``, so it is the host's, not the guard's.

## 0.11.4 — 2026-08-23

- **A host with no dot is never a public document.** 19 rows in the live index
  had one and not one was a source: intranet services (``prometheus:9090``,
  ``homelab:3000``, Ollama on ``host:11434``), a machine name, and this repo's
  own test fixtures (``https://h/R&D``, ``https://a``). A single-label name
  resolves only inside a network that already knows it, so it cannot identify a
  document anyone else can read — the same thing the localhost, tailnet and
  reserved-name rules already say. It goes with them rather than becoming a new
  kind of check, which also means ``prune`` sweeps the backlog with it.

  The rule runs only after an IP literal has been ruled out. A bracketed IPv6
  host has no dot either, and catching it here would exclude every IPv6 URL —
  the same damage as the ``]`` truncation that once stored them all as
  ``https://[::1``. There is a test for exactly that.

- **A merge no longer carries the duplicate's title state onto the survivor.**
  Repairing ``…/ARFDSynInt.git|`` merged it into the clean row and tagged that
  row ``title:unresolved`` — although its title was perfectly good — because the
  merge carried every tag across. Provenance (``project:``, ``seen:``,
  ``context:``) belongs to the sighting and should move; ``title:unresolved``
  describes the duplicate's own title and should not. It sticks, too:
  ``title_is_unresolved()`` trusts the tag over the title in front of it, so one
  bad carry marks a healthy item as junk permanently.

- **An indented code block is not a citation, and there is now a test saying so.**
  This is how six of this repo's test fixtures reached the live library on
  2026-08-22: an audit message demonstrated malformed CommonMark inside a
  four-space indent, and the pre-AST scanner had no notion of an indented block —
  its comment said as much, reasoning that an indented line is usually a list
  item. The stray backtick then left the URL exposed as prose. The AST fixed this
  in 0.11.0 as a side effect and nothing has been captured that way since; the
  test pins it shut. The companion test records the honest limit: unindented,
  the same fragment IS a citation, and the parser is right to take it.

## 0.11.3 — 2026-08-23

Cleaning up what the old tokenizer left in the library. The grammar fix stopped
new damage; these two passes deal with the 96 rows already there.

- **Repair now recovers a URL from an illegal tail.** Rows exist ending in a
  shell backslash, an unpadded table pipe, or an ANSI reset from pasted terminal
  output. RFC 3986 permits none of them unencoded, so the address ends where the
  tail begins and cutting there recovers it rather than inventing it. Four rows
  in the live index qualify, all as merges into the clean URL already present.

  It refuses the case that looks identical and is not: if URL text *resumes*
  after the illegal character, the run was one literal.
  ``https://files.rcsb.org/download/{ID}.pdb`` cut at ``{`` would manufacture
  ``https://files.rcsb.org/download/`` — a real, fetchable directory nobody
  cited. So would a cut that leaves no host at all. Both return "no repair".

  The blanket "a backslash means a regex" rule is gone with it. That was too
  broad: it also skipped ``https://cloud.r-project.org\``, where the backslash is
  a shell line-continuation and the address in front of it is real.

- **A new retirement pass removes rows that can never be a source.**
  ``scripts/retire_rows.py``, dry run by default. Repair corrects a URL that has
  a right answer; retirement removes one that has none, and the two deliberately
  do not share a predicate. 92 rows qualify: 31 API templates
  (``{locus}``, ``${VERSION}``), and 61 addresses today's rules already refuse —
  fixture names, font CDNs, DNS-over-HTTPS endpoints, badge SVGs, image files —
  captured before those rules existed.

  The predicate is "can never resolve to a document", **not** "contains a
  character RFC 3986 forbids". The two overlap and are not the same test, and
  using the character test to decide deletion is how a real row eventually gets
  thrown away. Retirement also asks repair first: ``https://cloud.r-project.org\``
  fails the address test, yet the citation behind it is recoverable, and judging
  it without asking would have trashed it for a reason that reads convincingly in
  a log.

  Trashing is ``deleted: 1``, recoverable from any Zotero client, never the
  permanent DELETE.

## 0.11.2 — 2026-08-23

- **A URL cut short by a template is dropped, not stored as its prefix.** The
  0.11.1 whitelist changed how a template in plain prose fails, and not for the
  better: ``https://files.rcsb.org/download/{ID}.pdb`` used to be stored whole,
  where no exclusion rule caught it but it could never resolve, so it failed
  loudly as the URL-as-title junk this plugin removes. Stopping at ``{`` instead
  stored ``https://files.rcsb.org/download/`` — a real, fetchable directory that
  acquires a genuine title and reads as a citation nobody made. Quiet wrong data
  is worse than loud junk.

  A match is now discarded when URL text *resumes* after the illegal character:
  ``{`` followed by ``ID}.pdb`` means the run was one literal. Whitespace never
  counts, since that is how a URL normally ends, and neither does a *closing*
  delimiter — a closer can only appear after the thing it closes, so the URL had
  already ended. That second rule is not decoration: without it,
  ``[https://example.org/bar].`` lost a real citation, because the match stops at
  ``]`` and the sentence period reads as resumed URL text. An existing test
  caught it.

  Measured over the same 1,370 real assistant messages: **0 messages change, 0
  URLs dropped**. Forcing the guard to fire on every bare URL drops 852 of the
  4,009, so the measurement can tell a difference when there is one.

  A regex literal still escapes both rules — CommonMark unescapes ``\.``, so
  ``https://data\.gramene\.org/...`` arrives with no illegal character left and
  ``*`` is a legal sub-delimiter. Two such rows are in the live index. That is
  the wildcard class, which needs the code-block judgement rather than the
  grammar; ``test_a_regex_literal_survives_because_commonmark_unescapes_it``
  records the limit instead of hiding it.

## 0.11.1 — 2026-08-23

- **The URL tokenizer asks the grammar instead of a list of exclusions.** The
  bare-URL matcher was a character blacklist, ``[^\s<>"'`\]]+``, so anything
  nobody had thought to exclude was taken as URL data: a trailing ``|`` from an
  unpadded table cell, ``{ID}`` from a template, a raw ANSI escape from pasted
  terminal output. Those addresses can never resolve a title, so they decay into
  the URL-as-title junk this plugin exists to remove — 49 such rows are in the
  live index. Lengthening ``TRAILING_PUNCT`` is the wrong layer: it consumes the
  bad boundary rather than preventing it, and only ever in trailing position.
  The character class is now derived from RFC 3986, with three departures, each
  argued in the source: ``[`` and ``]`` stay out (legal only in an IPv6 host,
  which has its own branch), ``'`` stays out (legal, but across 1,370 real
  messages all seven apostrophes adjacent to a URL were shell, Python or English
  delimiters and none was URL data), and non-ASCII is admitted per RFC 3987,
  because a strict-ASCII class truncates ``.../wiki/München`` to ``.../wiki/M``
  — the same damage as the ``]`` truncation that once broke every IPv6 URL.

  Measured over 1,370 real assistant messages and the 4,009 URLs 0.11.0 extracts
  from them, the change is a **no-op: 0 messages differ, output byte-identical**.
  Every illegal character in that corpus already sat inside a code span or fence,
  which the AST skips, and the live index's bad rows all predate the 0.11.0
  deploy. This removes the mechanism, not a measured defect rate. A deliberately
  broken variant admitting a space changed 218 of the same messages, so the
  measurement could tell a difference when there was one to tell.

## 0.11.0 — 2026-08-22

A second external audit, this time of the 0.10.0 fixes themselves. Nine findings,
all reproduced locally before being accepted — plus one that neither the audit
nor I had ranked, which turned out to be the largest.

- **A parser now decides where a URL ends.** Extraction scanned raw text: a
  line-oriented fence state machine and hand-paired backtick runs. Measured
  against a CommonMark reference over 1,415 real assistant messages, that lost a
  citation on 0.14% of them but mangled the *boundary* of **10.72%** of the URLs
  it found. Trailing punctuation was trimmed from markdown that had never been
  parsed, so `**[text](url)**` kept its emphasis — `*` is in no trim set, and by
  leaving the URL ending in `*` it also stopped the paren-balance rule from ever
  firing. Lengthening the trim set is not the fix: RFC 3986 makes `_` unreserved
  and `*` a sub-delimiter. Extraction now walks a markdown-it AST — skipping code
  spans, fences and inline HTML, taking a link destination exactly as the parser
  reports it, suppressing the label inside a link, and running the bare-URL
  matcher only over text the parser has already stripped of markdown.
  Re-measured on the same corpus: boundary damage **10.72% → 0.09%**, structural
  loss **0.14% → 0.00%**.

- **116 damaged rows repaired in place.** A URL ending in `**` can never resolve
  a title, so it degrades into exactly the URL-as-title junk this plugin exists
  to remove. `scripts/repair_urls.py` corrects them, merging into the existing
  item where one is already present and carrying the duplicate's tags across
  before retiring it to the trash. It deliberately refuses 17 rows that are not
  URLs at all — regexes, wildcards and escape sequences the old extractor
  captured — because "correcting" those would fabricate an address nobody cited.

- **Queries are no longer rewritten.** Tracking params were dropped by
  `parse_qsl` + `urlencode`, a round trip that does not preserve what it was not
  asked to change: a valueless field gained an `=`, percent-encoding was
  normalised, `+` was reinterpreted. Signed and opaque queries survived none of
  it. Filtering now splits on `&` and rejoins the survivors byte for byte. The
  blanket `&amp;` fold is gone too — it was at the wrong layer, since a literal
  `&amp;` is legal URL data and only the parser knows how the URL was written.

- **A claim is now answerable rather than guessed at.** A POST that raised was
  treated as a POST that created nothing, so the claim was released and the next
  sighting posted a duplicate — but Zotero can commit before the response is
  lost. The item key is now chosen before the request goes out and stored with
  the claim, so an abandoned claim is settled with one GET: item there, complete
  it; item absent, release and retry. Claims younger than 60s are left alone.

- **A hook killed by its own timeout releases its claim.** `timeout 10` sends
  SIGTERM, which Python leaves at its default disposition — the process died
  without unwinding, stranding the URL as both uncaptured and undedupable.

- **The loser of a race keeps its provenance.** It queues its context and project
  tags for whoever completes the item, instead of dropping the sighting silently.

- **Fetches connect to the address that was checked.** The guard resolved a name,
  then handed the *name* onward, so the inner transport resolved it again —
  letting an attacker-controlled DNS answer public for the check and private for
  the connect. The validated address is now pinned for the request, with the Host
  header and TLS SNI preserved so virtual hosts and certificate verification
  still work, and restored afterwards so each redirect hop gets its own check.

- **A report cannot cite itself.** Item titles come from fetched pages, and the
  commonest junk title here is a bare URL — bolded as prose, those were read
  straight back as citations. Titles are now shown as literals in a code span
  built so nothing in the content can close it. The no-capture marker is no
  longer an unauthenticated kill switch: it is honoured only in assistant output,
  and only when it leads the message.

- **A resolved title survives a 412 retry.** The resolver was re-invoked per
  attempt, so a title found on the first try was discarded if the second failed.

## 0.10.0 — 2026-08-22

The rest of the external audit of 0.9.0.

- **The Stop hook never read "the last message."** It selected every assistant
  event in the whole transcript, concatenated them, and kept the last 200 lines.
  Old URLs were therefore re-captured on later turns with today's `seen:` tag and
  the current turn's `context:`, and a code fence opened in an earlier turn
  decided whether this turn's citations were captured at all. It now uses the
  `last_assistant_message` the hook is given, falling back to the final assistant
  event only — base64-framed so a multi-line message survives intact.

- **`/source-delta` re-captured everything it reported.** It emitted markdown
  links, the one form capture deliberately keeps, so displaying the report
  stamped today's `seen:` tag onto the very sources it listed as dropped. Reports
  now show URLs in backticks and carry an explicit no-capture marker that capture
  honours. Two independent layers, because a reformatted report loses its
  backticks and a summarised one loses its marker.

- **The fence and code-span rules were not CommonMark.** Any three-backtick or
  three-tilde line toggled state, so `~~~` closed a `fence and` closed a
  ```` one; only single-backtick same-line spans were understood, so ``` ``url` ```` `
  slipped through. Closing fences now require the opener's character, at least
  its length, and nothing but trailing whitespace; spans pair runs of equal
  length and may cross lines within a paragraph. Blockquoted fences are
  recognised. Span pairing is bounded to a paragraph so one stray backtick cannot
  silently swallow every later citation. Retention on a 324-message replay is
  unchanged at 95.6%.

- **Host filtering was not an SSRF boundary.** It inspected spelling only, so
  `2130706433`, `0x7f000001`, `017700000001` and `127.1` — all 127.0.0.1 to any
  HTTP client — passed straight through, and a public host could simply redirect
  to a private one. Fetching now goes through a guarded transport that resolves
  and validates every hop, redirects included. Known limit, stated rather than
  hidden: validation happens at resolve time, so DNS rebinding is not covered.

- **Dedup was neither atomic nor durable.** `lookup → POST → INSERT OR IGNORE`
  let two sessions both create an item, after which one key was silently dropped
  and the other Zotero item became invisible to dedup forever. A URL is now
  claimed before the POST; a failed POST releases the claim. Verified with real
  concurrent processes, not a simulated race.

- **A 412 lost this session's tags.** Two sessions tagging one item is ordinary,
  and the loser simply raised — with no retry queue behind it, the tags were
  gone. It now refetches and reapplies, merging the other writer's tags instead
  of overwriting them, and gives up loudly after three attempts.

- **`prune` skipped items while paginating.** Trashed items leave Zotero's
  listings, so removing entries from one page shifted the rest left and the next
  offset stepped over exactly as many as had been removed — reporting a clean
  sweep over a collection it had only partly seen. It now snapshots before
  writing. The old test could not catch this because its mock never removed
  patched items.

- **IPv6 was broken end to end.** The tokenizer stopped at `]`, truncating every
  bracketed URL, and canonicalization rebuilt the netloc without brackets. The
  passing IPv6 tests only ever called `is_excluded` directly.

## 0.9.1 — 2026-08-22

Fixes from an external audit of 0.9.0.

- **Decoding the full HTML entity table rewrote the URL's structure.** 0.9.0 ran
  `html.unescape()` over the whole URL, but that table contains the URL's own
  delimiters: `&sol;` is `/`, `&num;` is `#`, `&quest;` is `?`. So a path
  segment could forge a separator, or invent a fragment that canonicalization
  then discarded — storing a different resource than the one cited. Only `&amp;`
  is decoded now. It is the one entity a URL acquires merely by being written
  into HTML, it cannot move a delimiter, and it is what the dedup fold actually
  needed: a re-printed `&quot;` comes back as `&amp;quot;`.

- **`pyproject.toml` still advertised 0.1.0** — eight releases stale, while
  `test_version.py` asserted "exactly one source of truth" in its own docstring.
  The drift guard now covers it.

- **`pytest -q` reached the internet** despite the README promising it did not.
  The live title-fetch tests carry their own fixture and never checked an opt-in.
  `addopts = -m "not live"` makes the documented behaviour the real one.

## 0.9.0 — 2026-08-22

- **A URL that is shown is no longer captured as one that is cited.** The hook
  reads Claude's own output, so auditing the library re-captured the URLs the
  audit printed. Worse, printing re-escapes them (`&quot;` → `&amp;quot;`), which
  is a _different string_ — it missed the dedup lookup and created a new item
  every pass, with no ceiling. `extract_urls` now skips fenced code blocks and
  inline code spans, which is Markdown's own way of marking a literal.

  Measured over 324 sessions' messages, 95.6% of captured URLs are unaffected;
  what drops out is mostly internal infrastructure. Indentation is deliberately
  not a signal, because an indented line is usually a list item. Verified by
  replaying the real transcripts that caused the incident, not only fixtures —
  a synthetic-only check had already led to the wrong conclusion here once, when
  fenced blocks turned out to carry 1 of 8 occurrences and inline spans 7.

- **HTML entities in a URL are decoded before storing.** A link lifted out of
  rendered markup carries the page's escaping, so `?a=1&amp;b=2` used to become a
  second item for a source already held. This is what made the loop above
  unbounded rather than merely repetitive, and it is a real defect on its own.

## 0.8.1 — 2026-08-21

- **Repairing a URL no longer strands the item.** `title_is_unresolved` detects a
  failed fetch by `title == url`. Moving the URL while leaving the old URL as the
  title breaks that equality, so the item silently stops looking unresolved and
  no backfill revisits it again — the same latch the re-enrichment work removed,
  reintroduced from the other side. `update_url` now carries a URL-shaped title
  along with the URL. A real title is metadata and is left untouched.

## 0.8.0 — 2026-08-21

- **A host that is not a hostname is no longer captured.** A display ellipsis
  reached the library as `https://…` and then raised "Invalid IDNA hostname" on
  every fetch attempt for the rest of its life. The test is IDNA encoding — what
  the HTTP client itself applies — rather than an ASCII whitelist, so genuine
  internationalised domains (`münchen.de`, `例え.テスト`) are kept. IP addresses
  are settled before the name test, which is what keeps public IPv6 working.

## 0.7.0 — 2026-08-21

- **Preprint DOIs were being built wrong.** The path regex swallowed the version
  suffix, so `10.1101/2025.05.30.656746v1` went to doi.org — not a DOI, and
  correctly a 404. The earlier audit blamed unregistered preprint DOIs; that was
  wrong. Stripping the suffix resolves the same papers 200. bioRxiv's own
  `api.biorxiv.org/details` answers 200 with an empty body, so it is not the fix
  the audit assumed.
- **The DOI prefix is no longer hardcoded.** bioRxiv minted `10.64898` for 2026
  papers alongside `10.1101`; matching only the latter silently skipped them.
- **Titles are normalised.** Publisher CSL metadata is typeset, not plain text —
  titles arrived carrying `<sup>`/`<i>` markup, HTML entities and the newlines of
  the source XML, and were stored in Zotero verbatim. Normalisation happens once
  at `fetch_title`'s boundary so a new resolver cannot forget it. A bare `<` is
  left alone: "Cost < 5% of baseline" is a real title, not markup.

## 0.6.0 — 2026-08-21

- **`backfill_titles.py --prune`** sweeps the exclusion rules back over items
  captured before those rules existed. `is_excluded` stays the single definition
  of what is not a source, so a rule added later needs no second list.
  Items are moved to the Zotero **trash**, not deleted — the collection is a
  provenance record, so a bulk cleanup has to be reversible. Pair with
  `--dry-run` first; it prints every URL it would remove.
- **A bulk pass no longer gives up on one slow response.** Paging the collection
  retries a timed-out page and raises after three attempts rather than returning
  early — a truncated sweep is indistinguishable from a short collection to the
  caller, so it must fail loud. Maintenance passes also get a 30s Zotero timeout
  instead of the hook's interactive 5s.

## 0.5.0 — 2026-08-21

- **Give the version one source of truth.** Four copies disagreed —
  `plugin.json` at 0.4.0, the package `__version__` at 0.1.0, and three
  User-Agent strings at 0.4, 0.1 and 0.1. Version and UA are now defined once in
  the package, and a test fails the build when they drift from the manifest.
- **Drop names the standards reserve.** `example.com`/`.net`/`.org` and the
  reserved TLDs (`.test`, `.example`, `.invalid`, `.localhost`, `.local`,
  `.onion`, `.alt`, `.arpa`, `.internal`) can never be a real source, and they
  are exactly what test fixtures use — five rows in the library came from
  another project's security fixtures being echoed into a session. Matching is
  on label boundaries, so `myexample.com` and `example.com.evil.co` survive.
- Test fixtures moved off `example.com` (and the end-to-end fixture off
  `.invalid`) so the suite still exercises the capture path it is testing.

## 0.4.0 — 2026-08-21

Backfilled entry. This release is about what the capture path should _never_
have stored, plus recovering what it stored badly.

- **Stop capturing page assets and infrastructure as sources.** Font CDNs,
  DNS-over-HTTPS endpoints and analytics beacons can never resolve to a title,
  so they accumulated forever. Asset extensions (`.css`, `.js`, `.woff`,
  images, `.map`) are excluded by path, with carve-outs for paths that are
  genuinely pages — GitHub `/blob/` and `/tree/` views, workflow badges,
  Wikimedia `/wiki/File:` description pages.
- **Send a contactable User-Agent.** Wikimedia's policy rejects a UA with no
  way to reach the operator; both `zotero-provenance/0.1` and a plain
  `Mozilla/5.0` got 403 from en.wikipedia.org. The UA now carries the project
  URL as its contact point. Recovered 49 items.
- **Stop truncating URLs that contain a closing paren.** `URL_RE` excluded `)`
  from the URL character class, so a match ended at the first one — corrupting
  Cell Press PII links and the DOIs behind them
  (`10.1016/s0092-8674(00)80876-3`) and Wikipedia disambiguation pages. The
  balanced-paren rule that exists to judge a trailing paren could never see
  one. Affected 31 items, all unresolved; several had been misattributed to
  publisher WAFs.
- **`--host` filter on the backfill pass**, so a fix that helps one host costs
  one host's requests instead of re-fetching a mostly-unfetchable backlog.

Known gap: the 31 already-corrupted URLs are not recoverable from the library.
The truncated tail was never stored.

## 0.3.0 — 2026-08-21

- Resolve DOIs by content negotiation instead of scraping.
- Resolve arXiv, PubMed, bioRxiv and GitHub through their own APIs.
- Add a backfill pass for items captured before re-enrichment existed.

## 0.2.0 — 2026-08-21

- Re-enrich unresolved titles instead of latching them forever.

## 0.1.0 — 2026-08-20

- Initial release: capture hook, commands, end-to-end tests, README.
