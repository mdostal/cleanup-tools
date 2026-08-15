#!/bin/sh
# Crafts latest.json -- the update manifest src-tauri/tauri.conf.json's
# plugins.updater.endpoints points at (via the stable
# releases/latest/download/latest.json alias, so that config never needs
# to change per release). tauri-plugin-updater expects this exact shape:
# https://v2.tauri.app/plugin/updater/#update-server-json-format
#
# Version and the signature come straight from this build's own output
# (tauri.conf.json + the .app.tar.gz.sig createUpdaterArtifacts produces --
# see tauri.conf.json's bundle.createUpdaterArtifacts) rather than being
# typed by hand, so they can't drift from what was actually built and
# signed.
#
# The download URL is NOT guessed from a naming convention: GitHub
# mangles spaces in uploaded asset filenames (observed directly on this
# project's own v0.1.0 release -- "Cleanup Tools_..." became
# "Cleanup.Tools_..." once uploaded), so this script takes the real,
# already-uploaded asset URL as an explicit argument instead of
# predicting it. Upload the .app.tar.gz FIRST (gh release upload), read
# its real URL back (gh release view --json assets), then run this.
#
# Usage:
#   sh packaging/tauri/make-latest-json.sh <app.tar.gz-asset-url> <notes> <output-path>
#
# Example, as part of cutting a release (see README.md's "Cutting a
# signed release" section for the full sequence):
#   sh packaging/tauri/make-latest-json.sh \
#     "https://github.com/mdostal/cleanup-tools/releases/download/v0.1.1/Cleanup.Tools.app.tar.gz" \
#     "Bug fixes." \
#     /tmp/latest.json

set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <app.tar.gz-asset-url> <notes> <output-path>" >&2
    exit 1
fi

ASSET_URL="$1"
NOTES="$2"
OUTPUT_PATH="$3"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
BUNDLE_ROOT="$REPO_ROOT/src-tauri/target"

SIG_FILE=$(find "$BUNDLE_ROOT" -name "*.app.tar.gz.sig" -path "*/bundle/macos/*" 2>/dev/null | head -n 1)
if [ -z "$SIG_FILE" ]; then
    echo "make-latest-json.sh: no *.app.tar.gz.sig found under $BUNDLE_ROOT -- did you set" \
         "TAURI_SIGNING_PRIVATE_KEY and run \`npm run tauri:build\` first? (createUpdaterArtifacts" \
         "in tauri.conf.json is what makes this file exist at all.)" >&2
    exit 1
fi

python3 "$SCRIPT_DIR/make_latest_json.py" "$REPO_ROOT" "$SIG_FILE" "$ASSET_URL" "$NOTES" "$OUTPUT_PATH"
