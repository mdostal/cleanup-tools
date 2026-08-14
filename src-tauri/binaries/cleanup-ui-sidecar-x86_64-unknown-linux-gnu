#!/bin/sh
# Stub "sidecar" executable registered with Tauri's externalBin mechanism --
# Linux (x86_64-unknown-linux-gnu) counterpart of cleanup-ui-sidecar-stub.sh
# (the macOS aarch64-apple-darwin stub, unchanged by this file). See that
# file's own header comment for the full onedir-vs-externalBin rationale:
# why a tiny stub script -- not the real PyInstaller onedir binary -- gets
# registered as the `externalBin` sidecar at all, and why `exec` (not a
# forked `&&`-chained subshell call) is load-bearing for onedir's
# kill()-targets-the-real-Flask-process guarantee (see cleanup_ui.spec's
# docstring). None of that reasoning is platform-specific and none of it
# is repeated here beyond this pointer -- this file only adapts the PATH
# RESOLUTION half of the pattern to Tauri's Linux (.deb) bundle layout.
#
# LINUX PATH LAYOUT -- read from tauri-bundler's own source, NOT observed
# on a real installed package (see "NOT VERIFIED ON REAL HARDWARE" below):
# -----------------------------------------------------------------------
# Tauri's Debian bundler (crates/tauri-bundler/src/bundle/linux/debian.rs)
# installs:
#   - the main app binary AND every `externalBin` sidecar into the SAME
#     directory: `data/usr/bin/` -> `/usr/bin/` once installed. This stub
#     therefore lands in `/usr/bin/`, right next to the main
#     `cleanup-desktop` binary -- the direct analog of the macOS stub
#     landing in `Contents/MacOS/` next to the main .app binary.
#   - `bundle.resources` (this project's `resources/cleanup-ui-onedir` ->
#     `cleanup-ui-onedir`, see tauri.conf.json) into
#     `data/usr/lib/<settings.product_name()>/` -> `/usr/lib/<product_name>/`
#     once installed, where `product_name()` returns tauri.conf.json's
#     `productName` field VERBATIM -- for this project, "Cleanup Tools"
#     (a literal space and capital letters -- NOT "cleanup-desktop", the
#     Cargo/binary name, and not a slugified/kebab-cased variant of either).
#
# So, unlike macOS's `Contents/MacOS/` + `Contents/Resources/` (both owned
# by, and named after, the one .app bundle), `/usr/bin/` and
# `/usr/lib/Cleanup Tools/` relate to each other only via
# `../lib/<product_name>` from the binary's own directory -- there is no
# single shared bundle-root name to lean on. Handled below by trying that
# exact path first, then a defensive fallback.
#
# NOT VERIFIED ON REAL ARCH/LINUX HARDWARE: nobody working on this project
# has access to Arch Linux hardware (see packaging/arch/PKGBUILD's own
# top-of-file notice and README.md's Arch Linux section for the full
# honesty caveat). The `/usr/lib/<productName>/` path above is read
# directly from tauri-bundler's current source, not observed from a real
# installed .deb. If it turns out to be wrong (a Tauri version difference,
# or a misreading of that source), this script tries a second candidate
# keyed off the Cargo package/binary name instead, then the same dev-mode
# fallback the macOS stub uses, and reports a specific, actionable error
# (never a silent hang) if all three fail.

set -e

STUB_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 1. Installed .deb, primary candidate: /usr/bin/<stub> ->
#    /usr/lib/Cleanup Tools/cleanup-ui-onedir/cleanup-ui-onedir
#    ("Cleanup Tools" = tauri.conf.json's productName, used verbatim --
#    space included -- by tauri-bundler's Debian resource-dir path; see
#    header comment above.)
BUNDLED_BY_PRODUCT_NAME="$STUB_DIR/../lib/Cleanup Tools/cleanup-ui-onedir/cleanup-ui-onedir"

# 2. Installed .deb, defensive fallback: same idea, keyed off the Cargo
#    package/binary name (src-tauri/Cargo.toml's [package] name =
#    "cleanup-desktop") instead of productName, in case a real build
#    resolves this differently than the source reading above predicts.
BUNDLED_BY_BINARY_NAME="$STUB_DIR/../lib/cleanup-desktop/cleanup-ui-onedir/cleanup-ui-onedir"

# 3. `tauri dev`: same relative shape as the macOS stub's own dev-mode
#    fallback (src-tauri/binaries/<stub> ->
#    src-tauri/resources/cleanup-ui-onedir/...) -- dev mode isn't
#    OS-bundle-specific, so this candidate is identical in form across
#    platforms.
DEV="$STUB_DIR/../resources/cleanup-ui-onedir/cleanup-ui-onedir"

if [ -x "$BUNDLED_BY_PRODUCT_NAME" ]; then
    REAL_BIN="$BUNDLED_BY_PRODUCT_NAME"
elif [ -x "$BUNDLED_BY_BINARY_NAME" ]; then
    REAL_BIN="$BUNDLED_BY_BINARY_NAME"
elif [ -x "$DEV" ]; then
    REAL_BIN="$DEV"
else
    echo "cleanup-ui-sidecar-stub-linux: could not locate the real cleanup-ui-onedir executable" >&2
    echo "  tried: $BUNDLED_BY_PRODUCT_NAME" >&2
    echo "  tried: $BUNDLED_BY_BINARY_NAME" >&2
    echo "  tried: $DEV" >&2
    echo "  If this is a real installed .deb, run:" >&2
    echo "    dpkg -L cleanup-desktop | grep cleanup-ui-onedir" >&2
    echo "  to see where the resources actually landed, and fix this script's candidate list to match." >&2
    exit 1
fi

exec "$REAL_BIN" "$@"
