---
description: Show which captured sources are new, persisting, recurring, or dropped for a context
argument-hint: [context-name] [--since 90d]
---

Report how the sources captured under a `context:` label have changed across runs.

Run (defaults to the `general` context when no argument is given):

```bash
"${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/source_delta.py" ${ARGUMENTS:-general}
```

Present the markdown it prints. The buckets mean:

- **New** — first surfaced in this run.
- **Persisting** — surfaced in exactly one prior run and again now.
- **Recurring (untriaged)** — surfaced in two or more prior runs and again now, with no
  `triaged` tag. These are the ones worth a decision; suggest `/triage <url>` for any the
  user is done with.
- **Recurring (triaged)** — same, but already acknowledged. Informational only.
- **Dropped** — seen before, absent from this run.
