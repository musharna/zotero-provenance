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

# Resolve an interpreter that can import httpx + bs4. Explicit override wins,
# then a venv created by setup, then whatever python3 is on PATH.
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
