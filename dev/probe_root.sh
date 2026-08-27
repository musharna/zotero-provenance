#!/usr/bin/env bash
# Run a command against a deployed plugin root with every side-effect channel
# closed, and PROVE the channels were closed rather than assuming it.
#
# On 2026-08-26 a probe exercised the trampoline's loop-refusal branch as a
# negative control -- the right instinct, proving the guard fires -- and left a
# real `forward-loop-refused` record in the production capture.log, which then
# greeted every new session as a capture fault for the next 24 hours. The probe
# had closed the network channel (ZOTERO_API_BASE at a dead port) and left the
# state directory pointed at the live one. Nothing needed building to prevent
# that: ZOTERO_CAPTURE_STATE_DIR already existed, on every hook, and
# sqlite_cache.py prints advice to use it. The knob was there and went unused,
# which is what a harness is for.
#
# A probe careful enough to build a negative control is exactly the probe that
# reaches production, because it deliberately drives the failure paths that only
# fire in anger. So this script does not merely set the variables:
#
#   * a SEAM CHECK runs first, on BOTH channels. Setting a variable the code
#     under probe does not read is the failure mode that makes a harness report
#     a comfortable zero forever -- dev/measure_extraction.py spent four
#     releases swapping a regex nothing called. Here the root's OWN expressions
#     are evaluated: the shell's `ZP_LOG=` line (yesterday's leak was a shell
#     write, not a Python one) and the Python `_state_dir`. If either resolves
#     outside the sandbox, this refuses to run the command at all.
#
#   * the production state directory is FINGERPRINTED before and after, and any
#     change is a loud failure rather than a silent one.
#
#   * `--control` proves that fingerprint guard can actually fail. A detector
#     nobody has watched detect is not evidence, and an unarmed guard reports
#     the same clean run as a working one.
#
#     dev/probe_root.sh --control                    # prove the guard fires
#     dev/probe_root.sh 0.20.2 -- hooks/run-python.sh scripts/foo.py
#     ZP_FORWARDED_FROM=/somewhere/else \
#       dev/probe_root.sh 0.20.2 -- hooks/run-python.sh x   # yesterday, safely
#
# Exit: 0 clean, 1 production state changed, 2 harness broken, 3 usage.
set -uo pipefail

CACHE_DIR="$HOME/.claude/plugins/cache/zotero-provenance/zotero-provenance"
PROTECTED="${ZOTERO_CAPTURE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}"
PYTHON="${ZP_PROBE_PYTHON:-python3}"
DEAD_API="http://127.0.0.1:9"

die() {
	printf 'probe_root: %s\n' "$1" >&2
	exit "${2:-3}"
}

usage() {
	sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
	exit 3
}

# Every file, not just the ones that changed size. A write that replaces a byte
# leaves mtime and length alone.
fingerprint() {
	local dir="$1"
	if [[ ! -d "$dir" ]]; then
		printf '(absent)\n'
		return
	fi
	find "$dir" -type f -print0 2>/dev/null | LC_ALL=C sort -z |
		xargs -0 -r md5sum 2>/dev/null
}

# --- the control ------------------------------------------------------------
# Tests the DETECTOR, not the root: a plain shell append into the directory
# being guarded. Root-independent on purpose -- the question is whether a write
# to the protected directory is visible to fingerprint(), and a shell redirect
# is the least deniable way to ask. Never touches the real state directory.
run_control() {
	local decoy before after
	decoy="$(mktemp -d)" || die "cannot create a decoy directory" 2
	trap 'rm -rf "$decoy"' EXIT
	printf 'seeded\n' >"$decoy/url_index.db"

	before="$(fingerprint "$decoy")"
	printf '{"event": "control-write"}\n' >>"$decoy/capture.log"
	after="$(fingerprint "$decoy")"

	if [[ "$before" == "$after" ]]; then
		printf 'CONTROL FAILED: a write to the guarded directory was invisible.\n' >&2
		printf 'The fingerprint guard cannot fail, so a clean run from it means nothing.\n' >&2
		exit 2
	fi
	printf 'control ok: the guard sees a write to the directory it protects.\n'
	printf 'A clean run from this harness is therefore worth something.\n'
	exit 0
}

# --- the seam check ---------------------------------------------------------
# Both channels, evaluated from the ROOT'S OWN source. A hardcoded expectation
# of how the root resolves its state directory would pass happily against a root
# that resolves it some other way -- which is the whole failure being guarded.

# The sandbox itself counts as inside the sandbox. `_state_dir` returns the
# directory, `ZP_LOG` a file within it, and a bare `$sandbox/*` glob accepts only
# the second -- so the exactly-correct Python answer read as a refusal until this
# was split out. The defect was in the measurement, not the thing measured, which
# is the same shape as the failure this whole script exists to catch.
under() {
	[[ "$1" == "$2" || "$1" == "$2"/* ]]
}

seam_shell() {
	local root="$1" sandbox="$2" line resolved
	line="$(grep -m1 '^ZP_LOG=' "$root/hooks/run-python.sh" 2>/dev/null)"
	[[ -n "$line" ]] || {
		printf 'no ZP_LOG assignment in %s/hooks/run-python.sh\n' "$root" >&2
		return 1
	}
	resolved="$(ZOTERO_CAPTURE_STATE_DIR="$sandbox" \
		bash -c "$line"'; printf "%s" "$ZP_LOG"' 2>/dev/null)"
	under "$resolved" "$sandbox" && return 0
	printf 'shell channel ignores the redirect: ZP_LOG -> %s\n' "${resolved:-<no output>}" >&2
	return 1
}

seam_python() {
	local root="$1" sandbox="$2" resolved
	resolved="$(
		ZOTERO_CAPTURE_STATE_DIR="$sandbox" timeout 30 "$PYTHON" - "$root" <<-'PY' 2>/dev/null
			import os, sys
			sys.path.insert(0, os.path.join(sys.argv[1], "scripts"))
			from zotero_capture.config import _state_dir
			print(_state_dir(os.environ))
		PY
	)"
	under "$resolved" "$sandbox" && return 0
	printf 'python channel ignores the redirect: _state_dir -> %s\n' "${resolved:-<no output>}" >&2
	return 1
}

# --- argument handling ------------------------------------------------------
KEEP=0
[[ $# -gt 0 ]] || usage
while [[ $# -gt 0 ]]; do
	case "$1" in
	--control) run_control ;;
	--keep)
		KEEP=1
		shift
		;;
	-h | --help) usage ;;
	--) die "no root given before --" ;;
	-*) die "unknown option: $1" ;;
	*) break ;;
	esac
done

[[ $# -ge 1 ]] || usage
ROOT="$1"
shift
[[ "${1:-}" == "--" ]] && shift
[[ $# -ge 1 ]] || die "no command given; use: <root> -- <command> [args...]"

# A bare version number is the common case; a path is accepted for a checkout.
[[ "$ROOT" == */* || "$ROOT" == "." ]] || ROOT="$CACHE_DIR/$ROOT"
ROOT="$(cd "$ROOT" 2>/dev/null && pwd -P)" || die "no such root: $1"
[[ -f "$ROOT/hooks/run-python.sh" ]] || die "not a plugin root: $ROOT"

SANDBOX="$(mktemp -d)" || die "cannot create a sandbox" 2
[[ $KEEP -eq 1 ]] || trap 'rm -rf "$SANDBOX"' EXIT

printf 'root      %s\n' "$ROOT"
printf 'sandbox   %s\n' "$SANDBOX"
printf 'protected %s\n' "$PROTECTED"

seam_shell "$ROOT" "$SANDBOX" || die "seam check failed: refusing to probe" 2
seam_python "$ROOT" "$SANDBOX" || die "seam check failed: refusing to probe" 2
printf 'seam      both channels honour the redirect (proved against this root)\n'

BEFORE="$(fingerprint "$PROTECTED")"

# The dead port is a refused connection, not a hang: a probe that silently waits
# is one that gets killed and read as a pass.
ZOTERO_CAPTURE_STATE_DIR="$SANDBOX" \
	ZOTERO_API_BASE="$DEAD_API" \
	bash -c 'cd "$1" && shift && exec "$@"' _ "$ROOT" "$@"
STATUS=$?

AFTER="$(fingerprint "$PROTECTED")"

printf '\ncommand exited %d\n' "$STATUS"
printf '%s\n' '--- what the probe wrote (in the sandbox, not in production) ---'
if [[ -d "$SANDBOX" ]] && find "$SANDBOX" -type f -print -quit | grep -q .; then
	find "$SANDBOX" -type f -printf '%P\n' | sort | sed 's/^/  /'
else
	printf '  (nothing)\n'
fi

if [[ "$BEFORE" != "$AFTER" ]]; then
	printf '\nPRODUCTION STATE CHANGED -- the probe was not isolated.\n' >&2
	diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") >&2
	exit 1
fi
printf '\nproduction state unchanged (%s)\n' "$PROTECTED"
[[ $KEEP -eq 1 ]] && printf 'sandbox kept at %s\n' "$SANDBOX"
exit "$STATUS"
