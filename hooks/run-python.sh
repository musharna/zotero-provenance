#!/usr/bin/env bash
# Run a zotero-provenance script with credentials loaded and a working interpreter.
# Used by the slash commands; the hooks call the scripts directly.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HOOK_DIR/lib.sh"
zp_load_secrets

exec "$(zp_python)" "$@"
