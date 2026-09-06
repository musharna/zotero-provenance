# Security

## What this plugin touches

- A Zotero API key with **write** access to one collection, stored at
  `~/.config/zotero-provenance/secrets.env` with mode `0600`. It is read by the
  hooks and never logged.
- Outbound HTTP to the pages Claude cites, to `api.zotero.org`, and to the
  metadata resolvers listed in the README. Fetches are SSRF-guarded: private,
  loopback, link-local and tailnet addresses are refused before any request,
  and the resolved IP is pinned across redirects.
- A local SQLite index and journal under `~/.local/state/zotero-provenance/`.
  Claim text (what a citation was cited *for*) is stored only there, never in
  the library, because the library syncs.

## Reporting a vulnerability

Open a **private** security advisory on the GitHub repository
(`Security` tab, "Report a vulnerability"), or email the address on the
repository owner's profile. Please include the plugin version
(`.claude-plugin/plugin.json`) and a reproduction. You will get an
acknowledgement within a week.

Do not open a public issue for anything that could expose a user's API key,
library contents, or the addresses they cite.
