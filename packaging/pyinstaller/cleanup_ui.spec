# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the cleanup-tools approvals UI Flask sidecar.

Builds a frozen binary of ``entrypoint.py`` (see that file's docstring) --
the thing the native desktop shell (a separate, parallel piece of work)
spawns as a subprocess and talks HTTP to on localhost.

Two build modes share this one spec file, selected by the ``ONEFILE`` env
var, rather than two near-duplicate spec files:

    ONEFILE=1 pyinstaller packaging/pyinstaller/cleanup_ui.spec   # --onefile
    ONEFILE=0 pyinstaller packaging/pyinstaller/cleanup_ui.spec   # --onedir

DECISION: use --onedir (``ONEFILE=0``) as this project's real build target.
------------------------------------------------------------------------------
Both modes were built and smoke-tested for real (see
``scripts/smoke_test_sidecar.py`` and the story report) before deciding.
The measured evidence:

* Size: onedir is a ~67 MB directory (184 files); onefile is a single
  ~31 MB binary. Onefile is smaller on disk, as expected (its appended
  archive is compressed; onedir's COLLECT copies most binaries
  uncompressed for faster startup).
* Process lifecycle -- the deciding factor. A onedir binary IS the real
  Flask/Werkzeug process: the pid ``subprocess.Popen`` reports is the pid
  actually listening on the port, full stop. A onefile binary is a
  bootloader that FORKS a child running the real interpreter; the pid
  ``Popen`` reports is the bootloader's, not the child's. Verified with
  real ``kill``/``lsof`` (not just process-list inspection):
    - onedir, any signal (SIGTERM or SIGKILL) sent to the reported pid:
      the process dies immediately and ``lsof -iTCP:<port>`` shows the
      port free within well under a second, every time.
    - onefile, SIGTERM to the bootloader's reported pid: happened to be
      forwarded to the child on this bootloader version/platform, so both
      died and the port freed -- but this is undocumented default
      behavior, not a guarantee.
    - onefile, SIGKILL to the bootloader's reported pid (the realistic
      "supervisor gave up waiting for graceful shutdown" case, since
      SIGKILL is by definition never forwardable by any process): the
      bootloader dies instantly, but the real child process is ORPHANED --
      it keeps running, keeps the port bound, and keeps answering real
      HTTP requests indefinitely. ``lsof -iTCP:<port>`` after the "kill"
      still shows it LISTENing.

For a sidecar a native desktop shell spawns and must be able to reliably
terminate (including on the shell's own force-quit/crash-recovery paths,
where a SIGKILL escalation after a graceful-shutdown timeout is a normal,
expected supervisor behavior) -- an orphaned server silently holding the
port is a real reliability bug, not a theoretical one: it can make the
NEXT app launch fail to bind its port, and it leaks a process indefinitely
outside the shell's control. That risk outweighs onefile's ~36 MB size
advantage, which is trivial for a desktop app installer. onedir is
therefore the primary/production target; onefile remains buildable (and is
still exercised by the smoke test) as a documented fallback, not the
recommended default.

datas -- explicit, not globbed
------------------------------
Every file under ``src/cleanup_tools/ui/templates/`` and
``src/cleanup_tools/ui/static/`` is listed individually below rather than
via a glob pattern, and the list was cross-checked against a real
``find src/cleanup_tools/ui/templates src/cleanup_tools/ui/static -type f``
run at the time this docstring was last updated (6 files total: base.html,
dashboard.html, queue.html, keyboard.js, plan-trigger.js, theme.js). Missing even one
of these is a LOUD failure (TemplateNotFound / a 404 on the static route)
in the frozen binary despite working fine from source, since Flask needs
these as real files on disk at runtime, not merely importable Python --
they are never compiled into the PYZ archive the way ``.py`` modules are.

Destination directories mirror the package's own dotted-path layout
(``cleanup_tools/ui/templates``, ``cleanup_tools/ui/static``) because
Flask's ``Blueprint(..., template_folder="templates")`` in routes.py
resolves that folder relative to the *package's* on-disk location at
runtime (``root_path``) -- under PyInstaller's frozen importer that
resolves relative to ``sys._MEIPASS`` (onefile) / the executable's own
directory (onedir), using the same ``cleanup_tools/ui/...`` path shape the
source tree uses. If a future template/static file is added to the source
tree, add it here too -- this list is NOT auto-discovered.

This exact failure mode (a real static file -- ``plan-reclaim.js`` --
shipped in the source tree by a concurrent piece of work, but never added
here) already happened once and shipped a frozen binary with a silently
broken async "Plan: Reclaim" flow (404 on ``GET /static/plan-reclaim.js``,
falling through to raw navigation with no progress/error UI). Rather than
just fix that one entry and trust hand-maintenance going forward, there is
now an automated drift check: ``tests/test_pyinstaller_spec_datas.py``
statically parses this file's ``datas =`` assignment (without running the
``Analysis``/``EXE``/``COLLECT`` pipeline below, which needs PyInstaller's
own injected ``SPECPATH``-style globals to actually build) and asserts its
srcs are exactly the real ``find ... -type f`` output for both
directories -- failing loudly, as a normal ``pytest`` failure, if this
list and the real directory contents ever diverge again in either
direction (a file added to the source tree and forgotten here, or a stale
entry for a file that's been deleted). That test runs in every regular
``pytest`` invocation (no binary or PyInstaller build required), so this
docstring's file count/list above is a convenience cross-reference for
humans reading this file, not the actual enforcement mechanism.

hiddenimports -- anthropic's dependency tree
---------------------------------------------
``anthropic`` pulls in ``httpx`` -> ``httpcore``/``anyio``/``certifi``/
``idna``, and ``pydantic`` -> ``pydantic_core``/``annotated_types``/
``typing_inspection``. ``pyinstaller-hooks-contrib`` (pinned as a build
dependency, see pyproject.toml's ``build`` extra) ships hooks for
``pydantic``, ``anyio``, and ``certifi`` specifically (confirmed by
inspecting its ``stdhooks/`` directory: ``hook-pydantic.py``,
``hook-anyio.py``, ``hook-certifi.py`` exist as of the version pinned
here) -- those are picked up automatically, no entry needed below.
Neither ``httpx`` nor ``anthropic`` itself has a contrib hook, so their
dynamically-resolved bits are listed explicitly. This list was arrived at
empirically: built, ran the smoke test against ``/propose-ai`` with a
throwaway ``ANTHROPIC_API_KEY`` to force the full httpx/pydantic call path
to execute (not just import), and added entries here until no
``ModuleNotFoundError`` remained.
"""

import os

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
UI_TEMPLATES = os.path.join(REPO_ROOT, "src", "cleanup_tools", "ui", "templates")
UI_STATIC = os.path.join(REPO_ROOT, "src", "cleanup_tools", "ui", "static")

datas = [
    (os.path.join(UI_TEMPLATES, "base.html"), "cleanup_tools/ui/templates"),
    (os.path.join(UI_TEMPLATES, "dashboard.html"), "cleanup_tools/ui/templates"),
    (os.path.join(UI_TEMPLATES, "queue.html"), "cleanup_tools/ui/templates"),
    (os.path.join(UI_TEMPLATES, "settings.html"), "cleanup_tools/ui/templates"),
    (os.path.join(UI_STATIC, "keyboard.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "plan-trigger.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "theme.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "settings.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "settings-shell.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "settings-shortcut.js"), "cleanup_tools/ui/static"),
    (os.path.join(UI_STATIC, "update-checker.js"), "cleanup_tools/ui/static"),
    (
        os.path.join(UI_STATIC, "icon-choices", "broom-folder.png"),
        "cleanup_tools/ui/static/icon-choices",
    ),
    (
        os.path.join(UI_STATIC, "icon-choices", "broom-sparkle.png"),
        "cleanup_tools/ui/static/icon-choices",
    ),
    (
        os.path.join(UI_STATIC, "icon-choices", "tidy-folder-check.png"),
        "cleanup_tools/ui/static/icon-choices",
    ),
    (
        os.path.join(UI_STATIC, "icon-choices", "recycle-folder.png"),
        "cleanup_tools/ui/static/icon-choices",
    ),
]

hiddenimports = [
    # anthropic SDK internals PyInstaller's static import scanner doesn't
    # always resolve (lazily imported / referenced by string).
    "anthropic",
    "anthropic._base_client",
    "anthropic.types",
    "anthropic.resources",
    "anthropic.lib",
    "anthropic.lib.streaming",
    # httpx / httpcore transport internals, resolved lazily by httpx based
    # on installed extras (no contrib hook covers these).
    "httpx",
    "httpx._transports",
    "httpx._transports.default",
    "httpcore",
    "httpcore._sync",
    "httpcore._async",
    "h11",
    "idna",
    "certifi",
    "sniffio",
    "distro",
    "jiter",
    # pydantic v2's Rust core + typing helpers -- the contrib pydantic hook
    # covers most of this, these are the few extras seen missing in
    # practice with pydantic 2.x's plugin/deprecated submodules.
    "pydantic",
    "pydantic_core",
    "pydantic.deprecated",
    "pydantic.deprecated.decorator",
    "annotated_types",
    "typing_inspection",
    "typing_extensions",
]

a = Analysis(
    [os.path.join(SPECPATH, "entrypoint.py")],
    pathex=[os.path.join(REPO_ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

ONEFILE = os.environ.get("ONEFILE", "0") == "1"

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="cleanup-ui-onefile",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        cipher=block_cipher,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="cleanup-ui-onedir",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        cipher=block_cipher,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="cleanup-ui-onedir",
    )
