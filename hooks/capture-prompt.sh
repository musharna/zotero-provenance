#!/usr/bin/env bash
# UserPromptSubmit hook: capture URLs the USER shared, tagged context:user-shared.
#
# Prints NOTHING to stdout — UserPromptSubmit stdout is injected into the prompt.
# Runs detached so prompt submission is never delayed. Always exits 0.

set -uo pipefail

[[ "${ZOTERO_CAPTURE_DISABLE:-}" == "1" ]] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HOOK_DIR/lib.sh"

INPUT="$(cat)"
PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // empty')"
[[ -z "$PROMPT" ]] && exit 0
printf '%s' "$PROMPT" | grep -qE 'https?://' || exit 0

CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty')"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty')"
LOG="$(zp_log_path)"
mkdir -p "$(dirname "$LOG")"

(
	zp_load_secrets
	PY_BIN="$(zp_python)"
	zp_check_deps "$PY_BIN" "$LOG" || exit 0
	printf '%s' "$PROMPT" | zp_timeout 15 "$PY_BIN" \
		"$HOOK_DIR/../scripts/zotero_capture_main.py" \
		--cwd "$CWD" --session "$SESSION_ID" \
		--context user-shared --origin user --message-from-stdin
) >/dev/null 2>>"$LOG" &
disown 2>/dev/null || true

exit 0
