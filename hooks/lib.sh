#!/usr/bin/env bash
# Shared helpers for the zotero-provenance hooks. Sourced, not executed.

zp_log_path() {
	local state="${ZOTERO_CAPTURE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}"
	printf '%s/capture.log' "$state"
}

# Load credentials. Hooks do NOT inherit MCP-scoped env from ~/.claude.json,
# so the secrets file is the supported way to make the key visible here.
zp_load_secrets() {
	local f="${ZOTERO_SECRETS_FILE:-$HOME/.config/zotero-provenance/secrets.env}"
	if [[ -r "$f" ]]; then
		set -a
		# shellcheck disable=SC1090
		source "$f"
		set +a
	fi
}

# Resolve an interpreter that can import this plugin's dependencies. Explicit
# override wins, then a venv if one exists, then whatever python3 is on PATH.
#
# No venv is created for you. This comment used to say "a venv created by
# setup", which setup has never done — so the sentence described a path that
# did not exist and nothing checked. zp_check_deps below is the part that
# actually tells you when the interpreter cannot run the code.
zp_python() {
	if [[ -n "${ZOTERO_PROVENANCE_PYTHON:-}" ]]; then
		printf '%s' "$ZOTERO_PROVENANCE_PYTHON"
		return
	fi
	local state="${ZOTERO_CAPTURE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance}"
	if [[ -x "$state/venv/bin/python" ]]; then
		printf '%s' "$state/venv/bin/python"
		return
	fi
	printf 'python3'
}

# Run a command under a wall-clock limit, wherever one is available.
#
# The hooks used to call GNU `timeout` directly. Stock macOS does not ship it,
# so every capture died with "command not found" — a total, silent outage on a
# supported platform, and one no test could catch because the tests run here.
# coreutils installs it as `gtimeout`; with neither, the command still runs,
# because a capture without a limit is better than no capture at all.
zp_timeout() {
	local secs="$1"
	shift
	if command -v timeout >/dev/null 2>&1; then
		timeout "$secs" "$@"
	elif command -v gtimeout >/dev/null 2>&1; then
		gtimeout "$secs" "$@"
	else
		"$@"
	fi
}

# Report a missing dependency as a sentence instead of a traceback.
#
# url_processing imports idna, linkify_it and markdown_it at module scope, so a
# missing one kills the hook before the staleness guard — before ANY of this
# plugin's own error handling — and the user sees a raw ImportError in a log
# they have no reason to be reading. Returns non-zero and names the fix.
zp_check_deps() {
	local py="$1" log="$2"
	local missing
	missing="$("$py" - <<-'PYEOF' 2>/dev/null
		import importlib, sys
		need = ["httpx", "bs4", "idna", "linkify_it", "markdown_it"]
		out = []
		for m in need:
		    try:
		        importlib.import_module(m)
		    except Exception:
		        out.append(m)
		sys.stdout.write(" ".join(out))
	PYEOF
	)"
	if [[ -n "$missing" ]]; then
		# The ts is load-bearing, not decoration: the health check discards any
		# record it cannot place in time, so an event without one is invisible
		# to the very thing meant to report it. On a fresh install with missing
		# dependencies there may be no other record at all, and health would
		# then stay silent forever.
		local py_json="${py//\\/\\\\}"
		py_json="${py_json//\"/\\\"}"
		printf '{"ts": "%s", "event": "missing-dependencies", "python": "%s", "missing": "%s"}\n' \
			"$(date '+%Y-%m-%dT%H:%M:%S%z')" "$py_json" "$missing" >>"$log"
		printf 'zotero-provenance: %s cannot import: %s\n' "$py" "$missing" >>"$log"
		printf 'Install them for that interpreter, or set ZOTERO_PROVENANCE_PYTHON to one that has them.\n' >>"$log"
		return 1
	fi
	return 0
}
