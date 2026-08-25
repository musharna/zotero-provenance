#!/usr/bin/env bash
# SessionStart hook: say something only when capture is not working.
#
# Silence is the requirement, not a nicety. A check that speaks on a healthy
# session gets tuned out, and a tuned-out check is worse than none — that is how
# the measurement canary went unheeded for four releases.
#
# No trampoline here, unlike the capture hooks, and the reason is worth stating:
# this reads the LOG and the REGISTRY, which are global rather than per-root, so
# a superseded copy of this script reaches the same conclusion as a current one.
# It also never writes to the library, so a stale copy cannot corrupt anything —
# the worst it can do is give stale advice about a file it read correctly.
#
# Always exits 0. Breaking a session start would be a worse bug than any it
# reports.

set -uo pipefail

[[ "${ZOTERO_CAPTURE_DISABLE:-}" == "1" ]] && exit 0
[[ "${ZOTERO_CAPTURE_HEALTH_DISABLE:-}" == "1" ]] && exit 0

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HOOK_DIR/lib.sh" 2>/dev/null || exit 0

PY_BIN="$(zp_python)"
[[ -x "$PY_BIN" || -n "$(command -v "$PY_BIN")" ]] || exit 0

zp_timeout 10 "$PY_BIN" "$HOOK_DIR/../scripts/zotero_capture_health.py" 2>/dev/null
exit 0
