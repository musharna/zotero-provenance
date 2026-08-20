---
description: First-run setup for zotero-provenance — pick a library, create the collection, store credentials
---

Set up zotero-provenance for this user.

First check whether it is already configured:

```bash
ls -l "${ZOTERO_SECRETS_FILE:-$HOME/.config/zotero-provenance/secrets.env}" 2>/dev/null || echo "not configured"
```

If it is already configured, say so and ask whether they want to reconfigure before doing anything.

Otherwise tell the user they need a Zotero API key with library **write** access from
https://www.zotero.org/settings/keys — then have them run this themselves, because it
prompts for the key and the key must not pass through the transcript:

```
! "${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/zotero_setup.py"
```

The script lists the libraries the key can reach, creates a `web-sources` collection if
one does not exist, verifies the credentials with a live create/read/delete round-trip,
and writes `~/.config/zotero-provenance/secrets.env` with mode 0600.

Never ask the user to paste their API key into the chat.
