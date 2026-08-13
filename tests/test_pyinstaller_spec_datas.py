"""Regression test: keep ``packaging/pyinstaller/cleanup_ui.spec``'s
hardcoded ``datas=`` list in sync with the real files on disk under
``src/cleanup_tools/ui/templates/`` and ``src/cleanup_tools/ui/static/``.

The spec's own docstring explains why ``datas=`` is a hand-maintained,
explicit list rather than a glob: PyInstaller needs these as real files at
runtime (Flask's ``Blueprint(template_folder=...)`` resolves them relative
to the frozen package's on-disk location), not merely importable Python.
That list silently drifting out of sync with the real directory contents
already happened once for real: ``static/plan-reclaim.js`` was added to
the source tree by a concurrent piece of work and never added to the
spec, so a frozen binary shipped with ``GET /static/plan-reclaim.js``
returning 404 -- the async "Plan: Reclaim" click-intercept/polling JS
silently missing -- while working fine when run from source.

This test catches that class of bug WITHOUT needing a PyInstaller build:
it statically extracts the ``datas`` list's *value* from the spec file
(just enough of the file's own top-level code -- imports, the
``REPO_ROOT``/``UI_TEMPLATES``/``UI_STATIC`` path setup, and the
``datas = [...]`` assignment itself -- to compute the real list of
``(src, dest)`` tuples it builds), stopping short of the
``Analysis``/``PYZ``/``EXE``/``COLLECT`` calls that follow, which need
globals (like ``SPECPATH``) that only exist when PyInstaller itself execs
the spec file -- this test supplies ``SPECPATH`` itself instead, so the
extraction works under plain ``pytest``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "packaging" / "pyinstaller" / "cleanup_ui.spec"
UI_TEMPLATES = REPO_ROOT / "src" / "cleanup_tools" / "ui" / "templates"
UI_STATIC = REPO_ROOT / "src" / "cleanup_tools" / "ui" / "static"


def _load_spec_datas() -> list[tuple[str, str]]:
    """Return the ``datas`` list ``cleanup_ui.spec`` actually builds.

    Parses the spec file, keeps only the top-level statements up to and
    including the ``datas = [...]`` assignment, and execs just that
    prefix -- never the full spec (which would require a real PyInstaller
    run to supply ``Analysis``/``EXE``/``COLLECT``/``PYZ``).
    """
    tree = ast.parse(SPEC_PATH.read_text(), filename=str(SPEC_PATH))

    kept_statements = []
    for node in tree.body:
        kept_statements.append(node)
        is_datas_assignment = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "datas" for target in node.targets
        )
        if is_datas_assignment:
            break
    else:
        raise AssertionError(f"{SPEC_PATH} has no top-level `datas = [...]` assignment to extract")

    prefix_module = ast.Module(body=kept_statements, type_ignores=[])
    ast.fix_missing_locations(prefix_module)

    namespace = {
        "__name__": "cleanup_ui_spec_datas_probe",
        # Normally injected by PyInstaller itself when it execs the spec
        # file; the only such global the pre-`datas=` prefix depends on.
        "SPECPATH": str(SPEC_PATH.parent),
    }
    exec(compile(prefix_module, str(SPEC_PATH), "exec"), namespace)
    return namespace["datas"]


def _real_template_and_static_files() -> set[Path]:
    return {
        path.resolve()
        for directory in (UI_TEMPLATES, UI_STATIC)
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_spec_datas_matches_real_template_and_static_files():
    datas = _load_spec_datas()
    listed_srcs = {Path(src).resolve() for src, _dest in datas}
    real_files = _real_template_and_static_files()

    missing_from_spec = real_files - listed_srcs
    assert not missing_from_spec, (
        "packaging/pyinstaller/cleanup_ui.spec's datas= list is missing file(s) "
        f"that exist on disk: {sorted(str(p) for p in missing_from_spec)}. "
        "Add each one to datas= (see the spec's own docstring) -- a missing "
        "entry is a silent 404/TemplateNotFound in the frozen binary only, "
        "never caught by running from source."
    )

    stale_in_spec = listed_srcs - real_files
    assert not stale_in_spec, (
        "packaging/pyinstaller/cleanup_ui.spec's datas= list references file(s) "
        f"that no longer exist on disk: {sorted(str(p) for p in stale_in_spec)}. "
        "Remove the stale entry/entries."
    )
