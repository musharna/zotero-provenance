---
description: Mark a captured source URL as triaged, so it stops being flagged as recurring
argument-hint: <url>
---

Add the `triaged` tag to a URL already captured by zotero-provenance.

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/zotero_capture_main.py" --triage "$ARGUMENTS"
```

If the URL has not been captured yet the helper exits non-zero with
`URL not found in capture cache:` — surface that message and stop. Do not create a
triaged entry from nothing: a URL has to have been captured before it can be triaged.

On success, tell the user the `triaged` tag was added, and that future source-delta
reports will list this URL only under `Recurring (triaged — informational)`.
