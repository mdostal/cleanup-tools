#!/bin/sh
# Stub "sidecar" executable registered with Tauri's externalBin mechanism.
#
# WHY THIS FILE EXISTS (the onedir-vs-externalBin integration problem):
# -----------------------------------------------------------------------
# Tauri's `bundle.externalBin` mechanism (see src-tauri/tauri.conf.json and
# https://v2.tauri.app/develop/sidecar/) expects exactly ONE FILE per
# target-triple -- there is no supported way to register a *directory* of
# files as a sidecar. But this project's chosen PyInstaller build mode is
# `--onedir` (see packaging/pyinstaller/cleanup_ui.spec's docstring for the
# measured reasons: onedir's reported PID is the real Flask process, so a
# plain kill()/SIGTERM reliably frees the port on quit -- onefile's
# bootloader-vs-child PID split does not have that guarantee). A onedir
# build is a *directory*: the real executable (`cleanup-ui-onedir`) plus a
# sibling `_internal/` directory of shared libraries and Python bytecode
# that the executable loads relative to its own on-disk location. You
# cannot just rename that executable to
# `binaries/cleanup-ui-sidecar-aarch64-apple-darwin` and ship it alone via
# `externalBin` -- Tauri's bundler would place it in the .app's
# `Contents/MacOS/` with no `_internal/` sibling, and it would fail to
# start (missing its own bundled Python runtime/stdlib/deps).
#
# THE RESOLUTION: bundle the *directory* via `bundle.resources` instead
# (which does support directories, landing under the .app's
# `Contents/Resources/`, preserving structure), and register THIS tiny
# shell-script stub -- not the real onedir binary -- as the `externalBin`
# sidecar. Tauri copies this stub into `Contents/MacOS/` next to the main
# app binary. When the Rust side spawns it as a sidecar, it locates the
# real onedir executable in `Contents/Resources/` (a fixed, guaranteed
# sibling of `Contents/MacOS/` in every macOS .app bundle -- this is part
# of the .app bundle format itself, not a Tauri implementation detail) and
# `exec`s it, passing through all args.
#
# Using `exec` (not a plain `&&`-chained subshell call) is deliberate and
# load-bearing: `exec` replaces THIS PROCESS's image in place rather than
# forking a child, so the PID the OS reports for "the stub" and the PID
# reported for "the real Flask process" are the SAME PID throughout the
# process's life. That preserves onedir's whole reason for being chosen
# (see cleanup_ui.spec docstring): Tauri's CommandChild::kill() /
# SIGTERM/SIGKILL sent to the pid it tracked from spawn() lands on the
# real Flask/Werkzeug process directly, with no bootloader-vs-child
# indirection to go wrong -- there is now no bootloader at all, just one
# `exec` hop from a ~10-line shell script into the real interpreter.
#
# Path resolution handles both the built .app bundle layout (standard
# macOS Contents/MacOS + Contents/Resources siblings) and `tauri dev`
# (where externalBin binaries run from src-tauri/binaries/ directly and
# resources are NOT copied into a sibling Resources/ dir -- they stay
# under src-tauri/resources/), so this one stub works unmodified in both.

set -e

STUB_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# 1. Built .app bundle: Contents/MacOS/<stub> -> Contents/Resources/cleanup-ui-onedir/cleanup-ui-onedir
BUNDLED="$STUB_DIR/../Resources/cleanup-ui-onedir/cleanup-ui-onedir"

# 2. `tauri dev`: src-tauri/binaries/<stub> -> src-tauri/resources/cleanup-ui-onedir/cleanup-ui-onedir
DEV="$STUB_DIR/../resources/cleanup-ui-onedir/cleanup-ui-onedir"

if [ -x "$BUNDLED" ]; then
    REAL_BIN="$BUNDLED"
elif [ -x "$DEV" ]; then
    REAL_BIN="$DEV"
else
    echo "cleanup-ui-sidecar-stub: could not locate the real cleanup-ui-onedir executable" >&2
    echo "  tried: $BUNDLED" >&2
    echo "  tried: $DEV" >&2
    exit 1
fi

exec "$REAL_BIN" "$@"
