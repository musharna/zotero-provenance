# zotero-provenance

A Claude Code plugin that records the sources your agent actually used.

Every URL Claude cites in an answer — and every link you paste into a prompt — is
canonicalized, deduplicated, and filed into a Zotero collection as a `webpage` item,
tagged with the project, the context, and the date it was seen. Over time you get a
bibliography of your research trail instead of a scrollback you have to re-read.

It only ever **writes**. Reading and searching your library is well covered by
existing Zotero MCP servers; run one of those alongside this if you want both.

## What a captured item looks like

```
Title:  Attention Is All You Need
URL:    https://arxiv.org/abs/1706.03762
Tags:   context:lit-review  project:my-thesis  seen:2026-08-20  domain:arxiv.org
```

Cite the same URL three weeks later and the item is not duplicated — it gains a second
`seen:` tag. That history is what `/source-delta` reads.

### Identifiers are resolved, not scraped

Some links aren't really web pages — they're identifiers that happen to have a URL. For
these the authoritative metadata API is asked directly, which is faster than following
the link and far more reliable, because the pages behind them are among the most
aggressively bot-walled the plugin meets (`pubmed` and `arxiv` refuse a plain fetch
outright, and most captured arXiv links are PDFs that have no title to scrape at all).

| host                         | resolved via                       |
| ---------------------------- | ---------------------------------- |
| `doi.org`                    | DOI content negotiation (CSL JSON) |
| `arxiv.org`                  | arXiv export API                   |
| `pubmed.ncbi.nlm.nih.gov`    | NCBI E-utilities                   |
| `biorxiv.org`, `medrxiv.org` | the DOI embedded in the URL path   |
| `github.com`                 | GitHub repo API                    |

Resolution is an optimisation, never a new point of failure: if any of it fails, the
ordinary scrape still runs as a fallback. Dead or private GitHub repos 404 and stay
unresolved rather than being given an invented title. Set `GITHUB_TOKEN` to lift
GitHub's anonymous 60-requests/hour limit if you capture a lot of repos.

### When the title can't be fetched

Plenty of pages refuse a plain HTTP fetch — bot walls, JS-rendered markup, PDFs, dead
links. When the title fetch fails, the item is stored with its URL as the title **and
tagged `title:unresolved`**, so a failure stays recognisable instead of passing for real
metadata. You can list the backlog by searching that tag in Zotero.

The next time you cite the same URL, the title is fetched again and the item is corrected
in place — the tag is dropped and the real title replaces the URL. This rides the Zotero
read the recurrence path already performs, so it costs no extra API call. A transient
failure therefore heals itself; only genuinely unfetchable pages keep the tag.

At most three re-fetches are attempted per capture run, so a message citing many
unfetchable URLs cannot blow the Stop hook's time budget. The rest are retried on
later runs.

## Install

```
/plugin marketplace add musharna/zotero-provenance
/plugin install zotero-provenance
```

Then run setup. It needs a Zotero API key with library **write** access from
<https://www.zotero.org/settings/keys>:

```
/zotero-setup
```

Setup lists the libraries your key can reach, creates a `web-sources` collection if
there isn't one, **verifies the credentials with a live create/read/delete round-trip**,
and writes `~/.config/zotero-provenance/secrets.env` with mode `0600`.

Requires `python3` (3.10+) with `httpx` and `beautifulsoup4`, plus `jq`.

## Commands

| Command                   | What it does                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------- |
| `/zotero-setup`           | First-run configuration.                                                           |
| `/triage <url>`           | Mark a captured URL as dealt with, so it stops being flagged as recurring.         |
| `/source-delta [context]` | Show which sources are new, persisting, recurring, or dropped since previous runs. |

## The source delta

Sources get bucketed by their `seen:` history:

- **New** — first surfaced in this run.
- **Persisting** — surfaced once before, and again now.
- **Recurring (untriaged)** — surfaced in two or more prior runs and again now. These are
  the ones that deserve a decision. `/triage` them once you've made it.
- **Recurring (triaged)** — same, already acknowledged.
- **Dropped** — seen before, absent now.

## Configuration

| Variable                           | Meaning                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------ |
| `ZOTERO_API_KEY`                   | Required. Key with write access.                                         |
| `ZOTERO_LIBRARY_ID`                | Required. **No default** — a default would write into the wrong library. |
| `ZOTERO_WEBSOURCES_COLLECTION_KEY` | Required. Target collection.                                             |
| `ZOTERO_LIBRARY_TYPE`              | `user` (default) or `group`.                                             |
| `ZOTERO_CAPTURE_DISABLE=1`         | Turn capture off entirely.                                               |
| `ZOTERO_CAPTURE_PROJECT`           | Force the `project:` tag instead of deriving it.                         |
| `ZOTERO_CAPTURE_PROJECT_ROOTS`     | Extra path roots (`:`-separated) that projects live under.               |
| `ZOTERO_CAPTURE_STATE_DIR`         | Where the dedup database and log live.                                   |
| `ZOTERO_SECRETS_FILE`              | Alternate credentials file.                                              |
| `ZOTERO_PROVENANCE_PYTHON`         | Interpreter to use, if the default `python3` lacks the deps.             |
| `ZOTERO_API_BASE`                  | Alternate API root, for tests or an API-compatible server.               |

Hooks do not inherit MCP-scoped environment from `~/.claude.json`, which is why
credentials come from the secrets file.

### How the `project:` tag is chosen

In order: `ZOTERO_CAPTURE_PROJECT`; the basename of the nearest enclosing git repository;
the first path segment under `$HOME` or a configured root; the basename of the working
directory; `home`.

### Labelling a context

By default items are tagged `context:general`, and links you paste are tagged
`context:user-shared`. Put `[SOURCE-CONTEXT: some-label]` in an assistant message to
label everything captured from that turn — useful for keeping a literature review
separate from ordinary browsing.

## What is not captured

`localhost`, tailnet (`*.ts.net`), and private-range IPs are dropped, so internal
dashboards and local dev servers never reach your library. Tracking parameters
(`utm_*`, `fbclid`, `gclid`, …) are stripped before storing.

Page machinery is dropped too: font CDNs, DNS-over-HTTPS endpoints, analytics
beacons, and URLs whose path ends in an asset extension (`.css`, `.js`, `.png`,
`.svg`, …). These are things a page loaded, not sources anyone cited, and they can
never resolve to a title. Paths that are genuine pages despite the extension — a
GitHub `/blob/` view, a Wikimedia `/wiki/File:` page — are kept.

Names the standards reserve are dropped: `example.com`, `example.net`,
`example.org`, and anything under `.test`, `.example`, `.invalid`, `.localhost`,
`.local`, `.onion`, `.alt`, `.arpa` or `.internal`. None of them can resolve to a
real document, and they are what test fixtures use — without this rule another
project's fixture URLs become citations the moment its test output is echoed
into a session. Matching is on label boundaries, so ordinary hosts that merely
contain a reserved name (`myexample.com`, `example.com.evil.co`) are kept.

A host that is not a hostname is dropped too — a display ellipsis captured as
`https://…` can never resolve. The test is IDNA encoding, the same one the HTTP
client applies, so internationalised domains (`münchen.de`) are kept.

### Shown, not cited

A URL inside a fenced code block or inline backticks is treated as a literal
being displayed, not a source being cited, and is not captured. Prose and
markdown links still are.

This matters because the hook reads Claude's own output. Without the rule, asking
Claude to audit your library re-captured the very URLs the audit printed — and
because printing re-escapes them (`&quot;` becomes `&amp;quot;`), each pass
created a _new_ item rather than matching the old one. The loop had no ceiling.

The cost is real but small: measured across 324 sessions' messages, 95.6% of
captured URLs are unaffected, and what drops out is mostly internal
infrastructure (`prometheus`, VPN and API endpoints). A source you only ever
mention in backticks will be missed — write it as prose or a link to capture it.

Separately, HTML entities in a URL are now decoded before storing, so a link
copied out of rendered markup (`?a=1&amp;b=2`) lands on the same item as the
plain form instead of duplicating it.

## Known limitations

- **Sessions bridged with `/remote-control` do not fire local `Stop` hooks**, so nothing
  is captured in those sessions. Plain local CLI sessions work.
- **The retry queue is inert.** A failed Zotero write is logged and dropped, not retried.
  Nothing is enqueued today, precisely so a queue nothing drains cannot grow forever.
- Title lookup has a one-second budget; on timeout the item is stored with the URL as its
  title rather than delaying your session.

## Development

```
python3 -m pytest -q
```

229 tests run by default and need no network. The 10 live ones are opted _into_
with `-m live` rather than out of — `addopts = -m "not live"` is set, because
they used to run on a bare `pytest -q` and reach the internet despite this
section promising otherwise. The hook tests execute the real shell scripts as
subprocesses; the end-to-end tests run the real hook and CLI against a local HTTP
server standing in for the Zotero API, so only the remote service is stubbed.
Live Zotero tests additionally need `RUN_LIVE_ZOTERO=1` and credentials.

## License

MIT
