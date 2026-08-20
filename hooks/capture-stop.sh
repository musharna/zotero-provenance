#!/usr/bin/env bash
# Stop hook: capture URLs the ASSISTANT cited in its last message.
# Always exits 0 — a capture problem must never block the next user turn.

set -uo pipefail

[[ "${ZOTERO_CAPTURE_DISABLE:-}" == "1" ]] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HOOK_DIR/lib.sh"
zp_load_secrets

LOG="$(zp_log_path)"
mkdir -p "$(dirname "$LOG")"

INPUT="$(cat)"
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty')"
CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
	exit 0
fi

# Per-line tolerant parse (-R + fromjson?): one malformed transcript line would
# otherwise abort the whole jq pass and silently strand every URL after it.
ASSISTANT_TEXT="$(jq -Rr 'fromjson? | select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' "$TRANSCRIPT_PATH" 2>/dev/null | tail -200)"
[[ -z "$ASSISTANT_TEXT" ]] && exit 0

# Cheap pre-filter: nothing to do without a URL.
printf '%s' "$ASSISTANT_TEXT" | grep -qE 'https?://' || exit 0

# Optional context marker. `[SOURCE-CONTEXT: name]` in assistant text labels the
# `context:` tag; `[AUDIT-CONTEXT: name]` is accepted as a legacy alias.
CONTEXT="$(printf '%s' "$ASSISTANT_TEXT" |
	grep -oE '\[(SOURCE|AUDIT)-CONTEXT: *[A-Za-z0-9][A-Za-z0-9_-]*\]' |
	tail -1 | sed -E 's/^\[(SOURCE|AUDIT)-CONTEXT: *//; s/\]$//')"

ARGS=(--cwd "$CWD" --session "$SESSION_ID" --message-from-stdin)
[[ -n "$CONTEXT" ]] && ARGS+=(--context "$CONTEXT")

printf '%s' "$ASSISTANT_TEXT" | timeout 10 "$(zp_python)" \
	"$HOOK_DIR/../scripts/zotero_capture_main.py" "${ARGS[@]}" 2>>"$LOG"
exit 0
