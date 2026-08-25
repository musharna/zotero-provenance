#!/usr/bin/env bash
# UserPromptSubmit hook: capture URLs the USER shared, tagged context:user-shared.
#
# Prints NOTHING to stdout — UserPromptSubmit stdout is injected into the prompt.
# Runs detached so prompt submission is never delayed. Always exits 0.

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
ZP_MINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ZP_LOG="${ZOTERO_CAPTURE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}/capture.log"

zp_tramp_log() {
	mkdir -p "$(dirname "$ZP_LOG")" 2>/dev/null
	printf '{"ts": "%s", "event": "%s", "self": "%s", "detail": "%s"}\n' \
		"$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$ZP_MINE" "${2:-}" >>"$ZP_LOG" 2>/dev/null
}

# Only a managed cache root can be superseded. A development checkout runs its
# own code, or debugging from one would silently exercise whatever is deployed.
case "$ZP_MINE" in
"$HOME/.claude/plugins/cache/"*)
	ZP_REG="$HOME/.claude/plugins/installed_plugins.json"
	# Identity is EXACT and derived from where this root lives:
	# .../cache/<marketplace>/<plugin>/<version>. Asking for any key starting
	# "zotero-provenance@" and taking the first hit resolved to whichever entry
	# was serialised first, so a registry legitimately holding this plugin from
	# two marketplaces, or at two scopes, could put the wrong path on the exec
	# line below. Nothing adversarial is needed for that; scopes are ordinary.
	ZP_PLUGIN="$(basename "$(dirname "$ZP_MINE")")"
	ZP_MARKET="$(basename "$(dirname "$(dirname "$ZP_MINE")")")"
	ZP_SUBTREE="$(dirname "$ZP_MINE")"
	ZP_TARGET=""
	if [[ -r "$ZP_REG" ]] && command -v jq >/dev/null 2>&1; then
		# One unambiguous installPath, or nothing. Two entries that disagree
		# refuse rather than guess.
		ZP_TARGET="$(jq -r --arg k "${ZP_PLUGIN}@${ZP_MARKET}" '
			[ (.plugins // {})[$k]? // [] | .[]?
			  | .installPath // empty | select(. != "") | sub("/+$"; "") ]
			| unique
			| if length == 1 then .[0] else empty end' "$ZP_REG" 2>/dev/null)"
	fi
	# Canonicalise before comparing. A symlink or ".." spelling would otherwise
	# make a root unequal to ITSELF: it would forward to itself, hit the
	# recursion guard, and lose not one capture but every capture for the life
	# of the session — the 29-hour outage again, reached by a spelling.
	if [[ -n "$ZP_TARGET" ]]; then
		ZP_TARGET="$(cd "$ZP_TARGET" 2>/dev/null && pwd -P || true)"
	fi
	# And only ever forward inside this root's own marketplace/plugin subtree.
	case "${ZP_TARGET:-}" in
	"$ZP_SUBTREE"/*) ;;
	*) ZP_TARGET="" ;;
	esac
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

# Heartbeat: one line per fire, so health can tell "nobody was working" from
# "the hooks are running and writing nothing". Elapsed time cannot separate
# those — a Friday capture and a Monday session is a 70-hour gap with nothing
# wrong — but fire count can. Append-only, so concurrent sessions need no lock.
printf '%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"${ZP_LOG%/*}/hook-fires.log" 2>/dev/null || true

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
