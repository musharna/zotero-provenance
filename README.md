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

### Requirements

`python3` (3.10+) and `jq`, plus five Python packages **on the interpreter the
hooks will run**:

```
python3 -m pip install httpx beautifulsoup4 idna linkify-it-py markdown-it-py
```

`idna`, `linkify-it-py` and `markdown-it-py` are imported at startup, so a
missing one stops capture before any of this plugin's own error handling. When
that happens the hooks write a line naming the interpreter and the missing
packages to `capture.log` rather than leaving a traceback there; capture is off
until it is fixed.

No virtualenv is created for you. If `python3` is not the interpreter that has
these, point `ZOTERO_PROVENANCE_PYTHON` at one that does — a venv at
`<state-dir>/venv` is also picked up automatically if you make one.

On macOS, `timeout` comes from `coreutils` (`brew install coreutils` provides
`gtimeout`, which is also accepted). Without either, capture still runs, just
without a wall-clock limit.

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
| `ZOTERO_CAPTURE_HEALTH_DISABLE=1`  | Turn off the session-start health check only.                            |
| `ZOTERO_CAPTURE_HEALTH_WINDOW_HOURS` | How far back operational faults are reported. Default 24.             |

Hooks do not inherit MCP-scoped environment from `~/.claude.json`, which is why
credentials come from the secrets file.

### The health check

Every serious failure this plugin has had was silent. Two things now watch for
that, and they are deliberately different in kind.

**Integrity incidents are journalled before the write they describe.** If a
capture is about to touch Zotero from a plugin root that is not the installed
one — or one it cannot verify — the incident is written to a small SQLite ledger
*first*. It used to be recorded afterwards, which meant a hook timeout at the
wrong moment could leave a row in your library with nothing anywhere saying so.
An intent for a write that never happens is a false positive you can close in
one command; a write with no intent is corruption nobody can find.

    python3 scripts/zotero_capture_health.py --list-incidents
    python3 scripts/zotero_capture_health.py --ack <id> [<id> ...]
    python3 scripts/zotero_capture_health.py --ack-all

An unknown id exits non-zero and changes nothing. A healthy install never opens
an incident, so its ledger stays empty.

**Operational faults decay.** Refusals, configuration errors and capture errors
are read from the log inside a recency window (24 h,
`ZOTERO_CAPTURE_HEALTH_WINDOW_HOURS`). Timing a *presence* is sound; timing an
*absence* is not, and nothing here does it. Records dated in the future are
reported as a clock problem rather than treated as perpetually recent.

Keeping these apart is what makes the check bounded: open incidents are a small
queryable set that shrinks when you resolve one, while operational faults expire
on their own and never need suppressing.

On a healthy session it prints nothing. If the check cannot run — no `jq`, no
`lib.sh`, no interpreter, an unreadable log, or an internal crash — it says so
and writes detail to `health-errors.log`. A log that is readable but unparseable,
or whose tail has stopped being parseable, also says so: unassessable is not
healthy.

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

Separately, `&amp;` in a URL is decoded before storing, so a link copied out of
rendered markup (`?a=1&amp;b=2`) lands on the same item as the plain form
instead of duplicating it. Only that one entity: the full HTML entity table
contains the URL's own delimiters (`&sol;` is `/`, `&num;` is `#`), and decoding
those would silently store a different resource than the one cited.

This plugin's own reports — `/source-delta` in particular — show URLs in
backticks and carry a marker that suppresses capture for the whole message.
Without that, displaying a report re-tagged every source it listed, including
the ones it was reporting as dropped.

### Where fetching is allowed to go

Title fetching runs through a transport that resolves each host and refuses
anything that is not publicly routable — loopback, private, link-local (which
carries the cloud metadata endpoint), reserved. Every redirect hop is checked,
not just the URL captured, since a public link can redirect anywhere. Numeric
host spellings (`2130706433`, `0x7f000001`, `127.1`) resolve like any other and
are refused the same way.

The check happens at resolution time, so a DNS record that changes between the
lookup and the connection — a rebinding attack — is not covered.

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

493 tests run by default and need no network. The 10 live ones are opted _into_
with `-m live` rather than out of — `addopts = -m "not live"` is set, because
they used to run on a bare `pytest -q` and reach the internet despite this
section promising otherwise. The hook tests execute the real shell scripts as
subprocesses; the end-to-end tests run the real hook and CLI against a local HTTP
server standing in for the Zotero API, so only the remote service is stubbed.

To run every live test, including the two that write to Zotero:

```bash
set -a; . ~/.config/zotero-provenance/secrets.env; set +a
RUN_LIVE_ZOTERO=1 \
ZOTERO_WEBSOURCES_COLLECTION_KEY_TEST=<a throwaway collection key> \
  python3 -m pytest -m live -q
```

Both write into that **separate** collection and delete after themselves, so
they never touch `web-sources`. Two variables gate them rather than one, and
without the collection key they SKIP — which is how one of them sat broken and
unnoticed: its URL had been switched to the non-resolving fixture host during a
cleanup while its assertion still expected the real page's title. A skipped test
reports the same green as a passing one. Run these after any change to title
fetching or the Zotero client.

## License

MIT
