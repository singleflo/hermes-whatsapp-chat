#!/usr/bin/env bash
# Re-vendor the Baileys WhatsApp bridge from a Hermes checkout and re-apply our
# local patches (patches/*.patch next to this script, in lexical order).
#
# Usage: update_bridge.sh [HERMES_CHECKOUT]   (run from anywhere)
#   HERMES_CHECKOUT defaults to ~/.hermes/hermes-agent
#
# Everything is staged in a temp dir first: if a patch does not apply, the
# script fails loudly and whatsapp-bridge/ (next to this script) is left untouched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Bridge location relative to the plugin dir: the patches are `patch -p1` diffs against the
# plugin dir layout, so the staging tree mirrors it.
BRIDGE_REL="sidecar/whatsapp-bridge"
BRIDGE_DIR="$SCRIPT_DIR/whatsapp-bridge"
PATCH_DIR="$SCRIPT_DIR/patches"
UPSTREAM_FILE="$BRIDGE_DIR/UPSTREAM"

HERMES="${1:-$HOME/.hermes/hermes-agent}"

die() { echo "update_bridge.sh: $*" >&2; exit 1; }

[ -f "$UPSTREAM_FILE" ] || die "missing $UPSTREAM_FILE"
[ -d "$HERMES" ] || die "Hermes checkout not found: $HERMES"

field() { sed -n "s/^$1:[[:space:]]*//p" "$UPSTREAM_FILE" | head -n 1; }

UP_REPO="$(field repo)"
UP_PATH="$(field path)"
UP_FILES="$(field files)"
UP_LICENSE="$(field license)"
[ -n "$UP_REPO" ] && [ -n "$UP_PATH" ] && [ -n "$UP_FILES" ] || die "UPSTREAM must define repo:, path: and files:"

SRC="$HERMES/$UP_PATH"
[ -d "$SRC" ] || die "upstream directory not found: $SRC"
for f in $UP_FILES; do
  [ -f "$SRC/$f" ] || die "upstream file missing: $SRC/$f"
done
if [ -n "$UP_LICENSE" ]; then
  [ -f "$HERMES/$UP_LICENSE" ] || die "upstream license missing: $HERMES/$UP_LICENSE"
fi

COMMIT="unknown"
if git -C "$HERMES" rev-parse --git-dir >/dev/null 2>&1; then
  COMMIT="$(git -C "$HERMES" rev-parse HEAD)"
  if [ -n "$(git -C "$HERMES" status --porcelain -- "$UP_PATH")" ]; then
    echo "warning: $HERMES/$UP_PATH has uncommitted changes; recorded commit $COMMIT does not describe the copied files" >&2
  fi
else
  echo "warning: $HERMES is not a git checkout; commit will be recorded as 'unknown'" >&2
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$BRIDGE_REL"

for f in $UP_FILES; do
  cp "$SRC/$f" "$STAGE/$BRIDGE_REL/$f"
done
if [ -n "$UP_LICENSE" ]; then
  cp "$HERMES/$UP_LICENSE" "$STAGE/$BRIDGE_REL/$(basename "$UP_LICENSE")"
fi

PATCHES=()
shopt -s nullglob
for p in "$PATCH_DIR"/*.patch; do
  PATCHES+=("$p")
done
shopt -u nullglob

for p in ${PATCHES[@]+"${PATCHES[@]}"}; do
  echo "applying $(basename "$p")"
  patch -p1 --forward --no-backup-if-mismatch --reject-file=- -d "$STAGE" < "$p" \
    || die "patch $(basename "$p") does not apply to $COMMIT; $BRIDGE_REL left unchanged. Rebase the patch and retry."
done

# All patches applied cleanly: install the staged tree.
CHANGED=0
for f in $UP_FILES ${UP_LICENSE:+"$(basename "$UP_LICENSE")"}; do
  if ! cmp -s "$STAGE/$BRIDGE_REL/$f" "$BRIDGE_DIR/$f"; then
    echo "updated $BRIDGE_REL/$f"
    CHANGED=1
  fi
  cp "$STAGE/$BRIDGE_REL/$f" "$BRIDGE_DIR/$f"
done
[ "$CHANGED" = 1 ] || echo "no file changes"

PATCH_NAMES=""
for p in ${PATCHES[@]+"${PATCHES[@]}"}; do
  PATCH_NAMES="${PATCH_NAMES:+$PATCH_NAMES }$(basename "$p")"
done

cat > "$UPSTREAM_FILE" <<EOF
repo: $UP_REPO
path: $UP_PATH
commit: $COMMIT
date: $(date +%F)
files: $UP_FILES
license: $UP_LICENSE
patches: ${PATCH_NAMES:-none}

The files above are copied verbatim from the upstream commit, then the patches
in ../patches/ are applied in order (patch -p1 from the plugin directory).
Refresh with: ../update_bridge.sh [HERMES_CHECKOUT]
EOF
echo "wrote $BRIDGE_REL/UPSTREAM (commit $COMMIT, patches: ${PATCH_NAMES:-none})"

echo
echo "Reminder: dependencies may have changed. The channel service runs npm ci on start when"
echo "node_modules is missing; to refresh by hand:"
echo "  rm -rf \"$BRIDGE_DIR/node_modules\" && npm ci --omit=dev --prefix \"$BRIDGE_DIR\""
