#!/usr/bin/env bash
# Stop hook: capture URLs the ASSISTANT cited in its last message.
# Always exits 0 — a capture problem must never block the next user turn.

set -uo pipefail

[[ "${ZOTERO_CAPTURE_DISABLE:-}" == "1" ]] && exit 0

# --- trampoline: a superseded root delegates instead of refusing ---------------
# A session keeps whichever plugin root it resolved at its own start and cannot
# be made to re-resolve without restarting. This SCRIPT, though, is re-read from
# disk on every fire, so it is the one place a running session's behaviour can
# still be corrected — and the correction is to hand the work to the root the
# plugin manager currently pins, rather than to refuse it. Refusing is safe but
# takes capture down for every live session until it restarts: on 2026-08-25
# that was 18 sessions and 29 hours of silence.
#
# Deliberately INLINE rather than in lib.sh. A root that predates this code has
# a lib.sh that predates it too; the whole point is to be correctable by
# replacing the file that actually runs.
#
# Authority is the pinned installPath, not a version comparison: it is what the
# manager actually resolves, it needs no parsing, and it follows a rollback in
# the right direction. staleness.py keeps its guard as defence in depth for the
# case where no target can be resolved at all.
ZP_SELF="$(basename "${BASH_SOURCE[0]}")"
ZP_MINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZP_LOG="${ZOTERO_CAPTURE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}/capture.log"

zp_tramp_log() {
	mkdir -p "$(dirname "$ZP_LOG")" 2>/dev/null
	printf '{"ts": "%s", "event": "%s", "self": "%s", "detail": "%s"}\n' \
		"$(date -Iseconds)" "$1" "$ZP_MINE" "${2:-}" >>"$ZP_LOG" 2>/dev/null
}

# Only a managed cache root can be superseded. A development checkout runs its
# own code, or debugging from one would silently exercise whatever is deployed.
case "$ZP_MINE" in
"$HOME/.claude/plugins/cache/"*)
	ZP_REG="$HOME/.claude/plugins/installed_plugins.json"
	ZP_TARGET=""
	if [[ -r "$ZP_REG" ]] && command -v jq >/dev/null 2>&1; then
		ZP_TARGET="$(jq -r '
			.plugins | to_entries[]
			| select(.key | startswith("zotero-provenance@"))
			| .value[]? | .installPath // empty' "$ZP_REG" 2>/dev/null | head -1)"
	fi
	if [[ "$ZP_TARGET" != "$ZP_MINE" ]]; then
		# Superseded: never capture from here. The rules this root would apply
		# are the ones a later release has already corrected.
		if [[ -n "${ZP_FORWARDED_FROM:-}" ]]; then
			zp_tramp_log "forward-loop-refused" "${ZP_FORWARDED_FROM}"
			cat >/dev/null
			exit 0
		fi
		if [[ -z "$ZP_TARGET" || ! -f "$ZP_TARGET/hooks/$ZP_SELF" ]]; then
			zp_tramp_log "forward-unresolved" "${ZP_TARGET:-none}"
			cat >/dev/null
			exit 0
		fi
		export ZP_FORWARDED_FROM="$ZP_MINE"
		exec bash "$ZP_TARGET/hooks/$ZP_SELF"
	fi
	;;
esac

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
