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

| Command | What it does |
|---|---|
| `/zotero-setup` | First-run configuration. |
| `/triage <url>` | Mark a captured URL as dealt with, so it stops being flagged as recurring. |
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

| Variable | Meaning |
|---|---|
| `ZOTERO_API_KEY` | Required. Key with write access. |
| `ZOTERO_LIBRARY_ID` | Required. **No default** — a default would write into the wrong library. |
| `ZOTERO_WEBSOURCES_COLLECTION_KEY` | Required. Target collection. |
| `ZOTERO_LIBRARY_TYPE` | `user` (default) or `group`. |
| `ZOTERO_CAPTURE_DISABLE=1` | Turn capture off entirely. |
| `ZOTERO_CAPTURE_PROJECT` | Force the `project:` tag instead of deriving it. |
| `ZOTERO_CAPTURE_PROJECT_ROOTS` | Extra path roots (`:`-separated) that projects live under. |
| `ZOTERO_CAPTURE_STATE_DIR` | Where the dedup database and log live. |
| `ZOTERO_SECRETS_FILE` | Alternate credentials file. |
| `ZOTERO_PROVENANCE_PYTHON` | Interpreter to use, if the default `python3` lacks the deps. |
| `ZOTERO_API_BASE` | Alternate API root, for tests or an API-compatible server. |

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

105 tests, no network required. The hook tests execute the real shell scripts as
subprocesses; the end-to-end tests run the real hook and CLI against a local HTTP
server standing in for the Zotero API, so only the remote service is stubbed. Live
API tests are skipped unless `RUN_LIVE_ZOTERO=1` and credentials are present.

## License

MIT
