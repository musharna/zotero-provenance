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

# Claude Code hands the Stop hook the final assistant text directly, and its docs
# say to use it rather than re-reading the transcript. Prefer it.
ASSISTANT_TEXT="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // empty')"

if [[ -z "$ASSISTANT_TEXT" ]]; then
	# Fallback for clients that predate that field. This hook fires once per
	# turn, so it must read ONE turn: the previous version concatenated every
	# assistant event in the whole transcript and kept the last 200 lines, which
	# re-captured old URLs with today's seen: tag and the current turn's
	# context:, and let a code fence opened in an earlier turn decide whether
	# this turn's citations were captured at all.
	#
	# base64 keeps each message on a single line, so `tail -1` selects a whole
	# final message instead of the tail of several concatenated ones.
	if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
		exit 0
	fi
	# Per-line tolerant parse (-R + fromjson?): one malformed transcript line
	# would otherwise abort the whole jq pass and strand everything after it.
	ENCODED="$(jq -Rr '
		fromjson?
		| select(.type=="assistant")
		| [.message.content[]? | select(.type=="text") | .text]
		| join("\n")
		| select(. != "")
		| @base64
	' "$TRANSCRIPT_PATH" 2>/dev/null | tail -1)"
	[[ -n "$ENCODED" ]] && ASSISTANT_TEXT="$(printf '%s' "$ENCODED" | base64 -d 2>/dev/null)"
fi

[[ -z "$ASSISTANT_TEXT" ]] && exit 0

# Cheap pre-filter: nothing to do without a URL.
printf '%s' "$ASSISTANT_TEXT" | grep -qE 'https?://' || exit 0

# Optional context marker. `[SOURCE-CONTEXT: name]` in assistant text labels the
# `context:` tag; `[AUDIT-CONTEXT: name]` is accepted as a legacy alias.
CONTEXT="$(printf '%s' "$ASSISTANT_TEXT" |
	grep -oE '\[(SOURCE|AUDIT)-CONTEXT: *[A-Za-z0-9][A-Za-z0-9_-]*\]' |
	tail -1 | sed -E 's/^\[(SOURCE|AUDIT)-CONTEXT: *//; s/\]$//')"

ARGS=(--cwd "$CWD" --session "$SESSION_ID" --origin assistant --message-from-stdin)
[[ -n "$CONTEXT" ]] && ARGS+=(--context "$CONTEXT")

PY_BIN="$(zp_python)"
zp_check_deps "$PY_BIN" "$LOG" || exit 0

printf '%s' "$ASSISTANT_TEXT" | zp_timeout 10 "$PY_BIN" \
	"$HOOK_DIR/../scripts/zotero_capture_main.py" "${ARGS[@]}" 2>>"$LOG"
exit 0
