#!/usr/bin/env bash
# UserPromptSubmit hook: capture URLs the USER shared, tagged context:user-shared.
#
# Prints NOTHING to stdout — UserPromptSubmit stdout is injected into the prompt.
# Runs detached so prompt submission is never delayed. Always exits 0.

set -uo pipefail

[[ "${ZOTERO_CAPTURE_DISABLE:-}" == "1" ]] && exit 0

# What this hook does when the trampoline declines. Kept OUT of the trampoline
# block so that block can stay byte-identical in all three hooks — three copies
# of path arithmetic that decides what gets exec'd must not drift silently.
# A capture hook stays quiet: it must never delay or interrupt a turn.
zp_tramp_refuse() {
	cat >/dev/null
	exit 0
}

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
#
# The cache prefix is canonicalised too, not just ZP_MINE. Comparing a physical
# ZP_MINE against a raw $HOME meant a symlinked home directory
# (/home/alice -> /srv/users/alice) never matched, so every superseded root was
# classified as a checkout and ran its own stale code — the v0.3.0 failure,
# reintroduced by the guard meant to prevent it.
ZP_CACHE="$(cd "$HOME/.claude/plugins/cache" 2>/dev/null && pwd -P)"
case "$ZP_MINE" in
"${ZP_CACHE:-/nonexistent-cache}"/*)
	ZP_REG="$HOME/.claude/plugins/installed_plugins.json"
	# Identity is EXACT and derived from where this root lives:
	# .../cache/<marketplace>/<plugin>/<version>. Asking for any key starting
	# "zotero-provenance@" and taking the first hit resolved to whichever entry
	# was serialised first, so a registry legitimately holding this plugin from
	# two marketplaces, or at two scopes, could put the wrong path on the exec
	# line below. Nothing adversarial is needed for that; scopes are ordinary.
	#
	# Parameter expansion rather than basename/dirname: this runs on every hook
	# fire, and four forks cost more than the work.
	ZP_SUBTREE="${ZP_MINE%/*}"
	ZP_PLUGIN="${ZP_SUBTREE##*/}"
	ZP_MARKET_DIR="${ZP_SUBTREE%/*}"
	ZP_MARKET="${ZP_MARKET_DIR##*/}"
	ZP_TARGET=""
	if [[ -r "$ZP_REG" ]] && command -v jq >/dev/null 2>&1; then
		# Each candidate is canonicalised BEFORE they are compared. Deduping raw
		# strings made two spellings of one root ("/p/1" and "/p/1/../1") look
		# like two candidates, and the ambiguity rule then refused every capture.
		# jq's OUTPUT is captured and its EXIT STATUS checked, rather than
		# streamed through process substitution — bash cannot see a producer's
		# status there. With a valid entry followed by a malformed one, jq printed
		# the good path and then exited 5, the loop counted one candidate and
		# accepted it; reversing the entries refused. That made resolution depend
		# on serialisation order again, which is the exact class of bug that
		# started this sequence. Entries are type-checked in jq for the same reason.
		ZP_SEEN="" ZP_COUNT=0
		if ZP_RAW="$(jq -r --arg k "${ZP_PLUGIN}@${ZP_MARKET}" '
			((.plugins // {}) | if type == "object" then .[$k] else null end) // []
			| if (type == "array") and (all(.[]; type == "object"))
			  then .[] else empty end
			| .installPath | select(type == "string" and . != "")' "$ZP_REG" 2>/dev/null)"; then
			while IFS= read -r ZP_CAND; do
				[[ -n "$ZP_CAND" ]] || continue
				ZP_CAND="$(cd "$ZP_CAND" 2>/dev/null && pwd -P)" || continue
				[[ -n "$ZP_CAND" ]] || continue
				case "$ZP_SEEN" in
				*"|$ZP_CAND|"*) continue ;;
				esac
				ZP_SEEN="$ZP_SEEN|$ZP_CAND|"
				ZP_TARGET="$ZP_CAND"
				ZP_COUNT=$((ZP_COUNT + 1))
			done <<<"$ZP_RAW"
		fi
		# One unambiguous root, or nothing. Two that disagree refuse rather than
		# guess: guessing is what put an unverified path on the exec line.
		((ZP_COUNT == 1)) || ZP_TARGET=""
	fi
	# Only ever forward inside this root's own marketplace/plugin subtree.
	case "${ZP_TARGET:-}" in
	"$ZP_SUBTREE"/*) ;;
	*) ZP_TARGET="" ;;
	esac
	if [[ "$ZP_TARGET" != "$ZP_MINE" ]]; then
		# Superseded: never capture from here. The rules this root would apply
		# are the ones a later release has already corrected.
		if [[ -n "${ZP_FORWARDED_FROM:-}" ]]; then
			zp_tramp_log "forward-loop-refused" "${ZP_FORWARDED_FROM}"
			zp_tramp_refuse "a forward returned to a superseded root"
		fi
		if [[ -z "$ZP_TARGET" || ! -f "$ZP_TARGET/hooks/$ZP_SELF" ]]; then
			zp_tramp_log "forward-unresolved" "${ZP_TARGET:-none}"
			zp_tramp_refuse "cannot resolve the installed plugin root"
		fi
		export ZP_FORWARDED_FROM="$ZP_MINE"
		exec bash "$ZP_TARGET/hooks/$ZP_SELF"
	fi
	;;
esac


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
