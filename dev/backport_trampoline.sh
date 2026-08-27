#!/usr/bin/env bash
# Put the trampoline into plugin roots that already exist, and prove it took.
#
# A release cannot fix a root that predates it. 0.13.0 gave the hooks a
# trampoline so a session pinned to a superseded root delegates to the installed
# one; 0.22.0 did the same for the COMMAND path. Neither reached backwards: a
# cache root laid down by 0.20.2 contains 0.20.2's launcher forever, and on
# 2026-08-26 fourteen live processes were holding exactly that root and would
# have run its pre-guard /triage against the library.
#
# So on 2026-08-26 the current launcher was copied by hand into 23 roots. That
# worked, and the knowledge lived only in a memory file. This is that operation,
# tracked -- reproducible, reviewable, and re-runnable after the next release
# lays down another root.
#
#   dev/backport_trampoline.sh            # what would change, and why
#   dev/backport_trampoline.sh --apply    # do it, with per-root backups
#   dev/backport_trampoline.sh --verify   # prove superseded roots forward
#
# WHOLE-FILE COPY, NOT A SPLICE. Once the trampoline forwards, the rest of the
# file never executes, so replacing it wholesale is no more invasive than
# splicing a block in and is far easier to verify: the result is byte-identical
# to the pinned root's copy. Splicing into a root whose `lib.sh` predates the
# code being spliced is how a fix arrives half-applied.
#
# ROOTS THAT ALREADY HAVE A TRAMPOLINE ARE LEFT ALONE, deliberately. The three
# capture hooks in the older roots carry an earlier block whose only difference
# is the argv-remapping loop, and hooks are handed JSON on stdin with no argv --
# the block's own comment says so. Rewriting them would be churn dressed up as
# safety. `--stale` lists them if you want to look.
#
# Exit: 0 clean, 1 work outstanding (dry run) or verification failed, 2 broken.
set -uo pipefail

CACHE_DIR="$HOME/.claude/plugins/cache/zotero-provenance/zotero-provenance"
REGISTRY="$HOME/.claude/plugins/installed_plugins.json"
BACKUP_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/zotero-provenance/backports"
HOOKS=(capture-prompt.sh capture-stop.sh session-health.sh run-python.sh)
MARKER='zp_tramp_refuse'

die() {
	printf 'backport_trampoline: %s\n' "$1" >&2
	exit "${2:-2}"
}

APPLY=0 VERIFY=0 SHOW_STALE=0
while [[ $# -gt 0 ]]; do
	case "$1" in
	--apply) APPLY=1 ;;
	--verify) VERIFY=1 ;;
	--stale) SHOW_STALE=1 ;;
	-h | --help)
		sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*) die "unknown option: $1" ;;
	esac
	shift
done

[[ -d "$CACHE_DIR" ]] || die "no plugin cache at $CACHE_DIR"
[[ -r "$REGISTRY" ]] || die "no registry at $REGISTRY"
command -v jq >/dev/null || die "jq is required to read the registry"

# Exact key. Asking for any key beginning "zotero-provenance@" and taking the
# first hit is how a root resolves to whichever marketplace sorted first.
PINNED="$(jq -r '
	.plugins["zotero-provenance@zotero-provenance"] // []
	| map(select(.installPath)) | .[0].installPath // empty
' "$REGISTRY")"
[[ -n "$PINNED" ]] || die "the registry pins no installPath for this plugin"
PINNED="$(cd "$PINNED" 2>/dev/null && pwd -P)" || die "pinned root does not exist: $PINNED"

for h in "${HOOKS[@]}"; do
	[[ -f "$PINNED/hooks/$h" ]] || die "pinned root is missing hooks/$h"
	grep -q "$MARKER" "$PINNED/hooks/$h" ||
		die "pinned root's hooks/$h has no trampoline -- refusing to copy it out"
done

printf 'pinned  %s\n\n' "$PINNED"

# --- verify -----------------------------------------------------------------
# Behaviour, not grep. Ask a root to run a script path that exists in NEITHER
# root and read which root the resulting error names: a forwarding root names
# the pinned one, because the launcher rewrites argv under its own prefix. The
# probe writes nothing -- the file it names does not exist -- and the state
# directory is redirected anyway, because a check that pollutes what it is
# checking is how this project got a false capture fault in the first place.
if [[ $VERIFY -eq 1 ]]; then
	sandbox="$(mktemp -d)" || die "cannot create a sandbox"
	trap 'rm -rf "$sandbox"' EXIT
	fail=0 forwarded=0 self=0
	for root in "$CACHE_DIR"/*; do
		[[ -d "$root" ]] || continue
		root="$(cd "$root" && pwd -P)"
		name="$(basename "$root")"
		[[ -f "$root/hooks/run-python.sh" ]] || {
			printf '  %-8s no launcher\n' "$name"
			fail=1
			continue
		}
		out="$(ZOTERO_CAPTURE_STATE_DIR="$sandbox" timeout 30 bash \
			"$root/hooks/run-python.sh" "$root/scripts/__absent_probe__.py" 2>&1)"
		if [[ "$root" == "$PINNED" ]]; then
			# The pinned root must NOT forward. Without this control, a script
			# that forwarded everything everywhere would score a clean sweep.
			if [[ "$out" == *"$PINNED/scripts/__absent_probe__.py"* ]]; then
				printf '  %-8s runs itself (pinned)\n' "$name"
				self=1
			else
				printf '  %-8s PINNED ROOT DID NOT RUN ITS OWN SCRIPT: %s\n' "$name" "$out"
				fail=1
			fi
		elif [[ "$out" == *"$PINNED/scripts/__absent_probe__.py"* ]]; then
			printf '  %-8s forwards -> pinned\n' "$name"
			forwarded=$((forwarded + 1))
		else
			printf '  %-8s DOES NOT FORWARD: %s\n' "$name" "$out"
			fail=1
		fi
	done
	printf '\n%d superseded root(s) forward; pinned root runs itself: %s\n' \
		"$forwarded" "$([[ $self -eq 1 ]] && echo yes || echo NO)"
	[[ $self -eq 1 ]] || {
		printf 'Without a root that declines to forward, "everything forwards" is not a result.\n' >&2
		fail=1
	}
	exit "$fail"
fi

# --- plan -------------------------------------------------------------------
missing=() stale=()
for root in "$CACHE_DIR"/*; do
	[[ -d "$root" ]] || continue
	root="$(cd "$root" && pwd -P)"
	[[ "$root" == "$PINNED" ]] && continue
	for h in "${HOOKS[@]}"; do
		target="$root/hooks/$h"
		[[ -f "$target" ]] || continue
		if ! grep -q "$MARKER" "$target"; then
			missing+=("$target")
		elif ! cmp -s "$target" "$PINNED/hooks/$h"; then
			stale+=("$target")
		fi
	done
done

if [[ $SHOW_STALE -eq 1 ]]; then
	printf 'roots with an OLDER trampoline (left alone by design, see header):\n'
	((${#stale[@]})) || printf '  (none)\n'
	for f in "${stale[@]}"; do printf '  %s\n' "${f#"$CACHE_DIR"/}"; done
	printf '\n'
fi

if ((${#missing[@]} == 0)); then
	printf 'every root has a trampoline in every hook. Nothing to do.\n'
	printf '(%d file(s) carry an older block; --stale lists them.)\n' "${#stale[@]}"
	exit 0
fi

printf '%d file(s) have NO trampoline and would run their own code:\n' "${#missing[@]}"
for f in "${missing[@]}"; do printf '  %s\n' "${f#"$CACHE_DIR"/}"; done

if [[ $APPLY -eq 0 ]]; then
	printf '\nDry run. Re-run with --apply to install the pinned copies.\n'
	exit 1
fi

# --- apply ------------------------------------------------------------------
# Backups first, and a manifest, because the thing being overwritten is the only
# copy of what a live process is executing. Atomic rename, because those live
# processes re-read these scripts on every fire: a half-written launcher is a
# broken session, while a rename is either the old file or the new one.
stamp="$(date '+%Y-%m-%d')"
backup="$BACKUP_ROOT/trampoline-$stamp"
mkdir -p "$backup" || die "cannot create $backup"

for target in "${missing[@]}"; do
	rel="${target#"$CACHE_DIR"/}"
	safe="${rel//\//_}"
	cp -p "$target" "$backup/$safe" || die "backup failed for $rel"
	printf '%s\n' "$rel" >>"$backup/manifest.txt"

	h="$(basename "$target")"
	tmp="$(dirname "$target")/.$h.incoming.$$"
	cp "$PINNED/hooks/$h" "$tmp" || die "copy failed for $rel"
	chmod --reference="$PINNED/hooks/$h" "$tmp" 2>/dev/null || chmod 0755 "$tmp"
	mv -f "$tmp" "$target" || die "install failed for $rel"
	printf '  installed %s\n' "$rel"
done

printf '\nbackups %s\n' "$backup"
printf 'Now prove it took: dev/backport_trampoline.sh --verify\n'
