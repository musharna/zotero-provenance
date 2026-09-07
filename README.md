# zotero-provenance

[![ci](https://github.com/musharna/zotero-provenance/actions/workflows/ci.yml/badge.svg)](https://github.com/musharna/zotero-provenance/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/musharna/zotero-provenance)](https://github.com/musharna/zotero-provenance/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A Claude Code plugin that records every source your agent actually used.**

Each URL Claude cites in an answer, and each link you paste into a prompt, is
filed into a Zotero collection from a hook the model cannot skip. Over time you
get a bibliography of your research trail instead of a scrollback to re-read.

```
Title:  Attention Is All You Need
URL:    https://arxiv.org/abs/1706.03762
Tags:   context:lit-review  project:my-thesis  seen:2026-08-20  domain:arxiv.org
```

- **Non-discretionary.** Capture runs on every turn, whether or not anyone asked.
- **Deduplicated.** Cite the same URL again and the item gains a second `seen:` tag.
- **Verifiable.** Pages are hashed, so you can later prove one still says what it said.
- **Write-only.** Reading your library is what Zotero MCP servers do; run one alongside.

## Install

Inside Claude Code:

```
/plugin marketplace add musharna/zotero-provenance
/plugin install zotero-provenance
/zotero-setup
```

The hooks need `python3` 3.10+, five packages on that interpreter, and `jq`:

```
python3 -m pip install httpx beautifulsoup4 idna linkify-it-py markdown-it-py
sudo apt install jq        # macOS: brew install jq coreutils
```

Setup asks for a Zotero API key with **write** access
(<https://www.zotero.org/settings/keys>), verifies it with a live round-trip,
and stores it at `~/.config/zotero-provenance/secrets.env` (mode `0600`).

| platform        | status                                        |
| --------------- | --------------------------------------------- |
| Linux           | supported; where it is developed and tested   |
| macOS           | supported; `brew install jq coreutils`        |
| Windows, WSL    | supported inside WSL                          |
| Windows, native | **not supported**; the hooks are bash scripts |

If `python3` is not the interpreter with the packages, set
`ZOTERO_PROVENANCE_PYTHON`. A venv at `<state-dir>/venv` is picked up automatically.

## Commands

| Command                   | What it does                                                             |
| ------------------------- | ------------------------------------------------------------------------ |
| `/zotero-setup`           | First-run configuration.                                                 |
| `/source-delta [context]` | Which sources are new, persisting, recurring, or dropped since last run. |
| `/triage <url>`           | Mark a recurring source as dealt with.                                   |

## What gets captured

Every URL in prose or a markdown link. Identifier hosts (`doi.org`, `arxiv.org`,
PubMed, bioRxiv, GitHub) are resolved through their metadata API rather than
scraped. When a title cannot be fetched the item is tagged `title:unresolved`
and corrected the next time the URL is cited.

Not captured: URLs shown in code blocks or backticks, localhost and private
addresses, tracking parameters, page assets, and reserved names like
`example.com`. Title fetching refuses anything not publicly routable, on every
redirect hop.

Details: [what is captured](docs/design.md#what-is-not-captured),
[identifier resolution](docs/design.md#identifiers-are-resolved-not-scraped),
[unresolved titles](docs/design.md#when-the-title-cant-be-fetched).

## Beyond capture

**Prove a page still says what it said.** Hashes are stamped onto the item's
`extra` field and never overwritten by a later check.

```
python3 scripts/snapshot_pages.py             # hash items that lack one
python3 scripts/snapshot_pages.py --verify    # what has changed since
```

**See what a source was cited for.** The sentence around each URL is kept in a
local index, never sent to Zotero.

```
python3 scripts/show_claims.py --search retraction
```

**Retry failed writes.** A failed Zotero write is queued, not dropped.

```
python3 scripts/drain_queue.py
```

Details: [page hashing](docs/design.md#proving-a-page-still-says-what-it-said),
[claims](docs/design.md#what-a-source-was-cited-for),
[the retry queue](docs/design.md#known-limitations).

## Configuration

Credentials come from the secrets file, because hooks do not inherit
MCP-scoped environment from `~/.claude.json`.

| Variable                             | Meaning                                                    |
| ------------------------------------ | ---------------------------------------------------------- |
| `ZOTERO_API_KEY`                     | Required. Key with write access.                           |
| `ZOTERO_LIBRARY_ID`                  | Required. No default; a default would write elsewhere.     |
| `ZOTERO_WEBSOURCES_COLLECTION_KEY`   | Required. Target collection.                               |
| `ZOTERO_LIBRARY_TYPE`                | `user` (default) or `group`.                               |
| `ZOTERO_CAPTURE_DISABLE=1`           | Turn capture off entirely.                                 |
| `ZOTERO_CAPTURE_PROJECT`             | Force the `project:` tag instead of deriving it.           |
| `ZOTERO_CAPTURE_PROJECT_ROOTS`       | Extra path roots (`:`-separated) that projects live under. |
| `ZOTERO_CAPTURE_STATE_DIR`           | Where the dedup database and log live.                     |
| `ZOTERO_SECRETS_FILE`                | Alternate credentials file.                                |
| `ZOTERO_PROVENANCE_PYTHON`           | Interpreter to use.                                        |
| `ZOTERO_API_BASE`                    | Alternate API root, for tests or a compatible server.      |
| `ZOTERO_CAPTURE_HEALTH_DISABLE=1`    | Turn off the session-start health check only.              |
| `ZOTERO_CAPTURE_HEALTH_WINDOW_HOURS` | How far back operational faults are reported. Default 24.  |

A health check runs at session start and prints nothing when all is well. When
capture is broken it says so in one line. Details:
[the health check](docs/design.md#the-health-check).

## How this differs from a Zotero MCP server

[zotero-mcp](https://github.com/54yyyu/zotero-mcp),
[Zoteus](https://forums.zotero.org/discussion/132067/),
[ZoFiles](https://github.com/X1AOX1A/ZoFiles) and
[llm-for-zotero](https://github.com/yilewang/llm-for-zotero) let the model read or
write your library **when asked**. This plugin records **every URL the model
cited**, asked or not, and keeps checking that those pages still say what they
said. One is a tool the agent uses. This is a record of what the agent used.

## Known limitations

- Claude Code on the web captures nothing; sessions bridged with `/remote-control` do.
- Title lookup has a one-second budget. On timeout the URL stands in as the title.
- A GitHub 404 is recorded as `not_visible`, never `gone`: private repos 404 on purpose.
- Citations captured before 2026-08-21 may under-count sources with `)` in the URL.

Full list with remedies: [known limitations](docs/design.md#known-limitations).

## Development

```
pip install -e ".[dev]"
python3 -m pytest -q
```

About 1,200 tests run by default and need no network. Ten live tests are opted
into with `-m live`. Design rationale, the deployed-root probe, and live-test
setup: [docs/design.md](docs/design.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
