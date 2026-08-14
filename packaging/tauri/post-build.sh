#!/bin/sh
# Ad-hoc codesign step for the Tauri macOS `.app` bundle, run AFTER `tauri
# build` (there is no Tauri v2 `build.afterBundleCommand`/`afterBuildCommand`
# hook as of the 2.11.x line used here -- only `beforeBuildCommand`,
# `beforeDevCommand`, and `beforeBundleCommand`, all of which fire before
# the .app exists, i.e. before there is anything to sign. Hence: a small
# wrapper script run manually/via npm script after the build, not a config
# hook).
#
# WHY THIS EXISTS, AND WHY IT DOES NOT (BY ITSELF) FIX GATEKEEPER:
# -----------------------------------------------------------------------
# An unsigned `tauri build` on this project's macOS version produces an
# .app with a technically-broken default ad-hoc signature: `codesign
# --verify` fails with "code has no resources but signature indicates they
# must be present" (no `Contents/_CodeSignature/CodeResources` seal was
# ever written). Confirmed independently twice (implementer + reviewer).
#
# Running `codesign --force --deep -s -` (this script) produces a
# genuinely valid ad-hoc signature -- `codesign --verify` passes
# afterward -- but this does NOT restore a Gatekeeper bypass for a
# quarantined (downloaded/AirDropped/etc.) copy. A quarantined copy of
# this app shows a hard "'Cleanup Tools' is damaged and can't be opened.
# You should move it to the Trash" dialog, with NO right-click-Open
# option -- this is NOT the classic "unidentified developer" dialog some
# older docs/designs for this project assumed, and codesigning alone does
# not change that. Confirmed by rebuilding, signing, then quarantining a
# copy and observing the "damaged" dialog persists.
#
# The only thing that actually works for a personal install on this
# machine is removing the quarantine attribute entirely:
#   xattr -d com.apple.quarantine "Cleanup Tools.app"   # or the .dmg
# See README.md's desktop app section for the full explanation. This
# script still runs ad-hoc codesign anyway because it's free, produces a
# technically valid signature (useful for other tooling/behavior that
# checks signature validity rather than Gatekeeper's quarantine flag),
# and is a reasonable step to keep even though it doesn't solve
# Gatekeeper on its own.
#
# Usage: run after `npm run tauri build` (or `npx tauri build`), from
# anywhere -- paths below are relative to this script's own location, not
# the caller's cwd.
#   npm run tauri build && sh packaging/tauri/post-build.sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
BUNDLE_ROOT="$REPO_ROOT/src-tauri/target"

# Covers both a plain `tauri build` (target/release/bundle/macos) and an
# explicit `--target <triple>` build (target/<triple>/release/bundle/macos).
APPS=$(find "$BUNDLE_ROOT" -type d -name "*.app" -path "*/bundle/macos/*" 2>/dev/null || true)

if [ -z "$APPS" ]; then
    echo "post-build.sh: no .app bundle found under $BUNDLE_ROOT -- did \`tauri build\` run first?" >&2
    exit 1
fi

echo "$APPS" | while IFS= read -r app; do
    [ -z "$app" ] && continue
    echo "post-build.sh: ad-hoc signing $app"
    codesign --force --deep -s - "$app"

    if codesign --verify --deep --strict "$app" 2>&1; then
        echo "post-build.sh: codesign --verify passed for $app"
    else
        echo "post-build.sh: WARNING -- codesign --verify still failing for $app" >&2
    fi

    echo "post-build.sh: reminder -- ad-hoc signing does NOT bypass Gatekeeper for a" \
         "quarantined copy. See README.md for the real fix (xattr -d com.apple.quarantine)."
done
