"""Tests for cleanup_tools.commands.reclaim.run() -- the highest-stakes
command in the project, since --go performs real deletion.

Mirrors the fake-home pattern in test_sort.py/test_survey.py: a MacOSAdapter
subclass whose resolve_home() (and, here, applications_dir) are redirected
under tmp_path, so config_module.load_config()'s default path and
find_installed_app()'s /Applications scan never resolve to -- let alone
read, write, or delete -- anything outside tmp_path. Every root scanned by
reclaim.run() is passed explicitly via args.dir, always a tmp_path
subdirectory. Docker is never actually invoked: subprocess.run is
monkeypatched to intercept only the ``docker ...`` invocation while
delegating every other command (e.g. the real ``du -sk`` calls made by
adapter.dir_size_bytes) to the real subprocess.run.

Sizing note: directories (build_caches, recycle_bin) are sized via
adapter.dir_size_bytes(), which shells out to real ``du -sk`` and therefore
reports block-rounded disk usage, not exact content bytes -- see
_assert_size_close, ported from test_survey.py's identical caveat. Files
(os_junk, orphaned_installers) are sized via a plain stat(), which IS exact,
so those assertions use plain ``==``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.commands import reclaim
from cleanup_tools.config import Config, DEFAULT_BUCKET_RULES, MasterPath, save_config
from cleanup_tools import queue as queue_module
from cleanup_tools.queue import QueueEntry, build_plan_snapshot


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _write(path: Path, size: int) -> None:
    """Create ``path`` (and parents) containing exactly ``size`` bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _make_fake_adapter(home: Path, applications_dir: Path) -> MacOSAdapter:
    """A MacOSAdapter whose resolve_home() points at ``home`` and whose
    find_installed_app() scans ``applications_dir`` instead of the real
    ``/Applications`` -- so config lookups AND installer detection both stay
    off the real filesystem, even if a test forgets to monkeypatch
    find_installed_app directly.
    """

    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

    return FakeHomeAdapter(applications_dir=applications_dir)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture
def adapter(home: Path, tmp_path: Path) -> MacOSAdapter:
    apps_dir = tmp_path / "Applications"
    apps_dir.mkdir()
    return _make_fake_adapter(home, apps_dir)


def _mk_node_modules(base: Path, filler_bytes: int = 50) -> Path:
    """Create ``base/node_modules/pkg/file.js`` and return the node_modules dir."""
    nm = base / "node_modules"
    _write(nm / "pkg" / "file.js", filler_bytes)
    return nm


# du -sk rounds each file's usage up to the filesystem's allocation block
# size, so a du-backed directory size is always >= raw content bytes and
# typically somewhat more. This margin is deliberately generous so the check
# only catches gross regressions (0, double-counting) rather than requiring
# exact equality with content bytes. Ported from test_survey.py.
_DU_PER_FILE_MARGIN = 16 * 1024


def _assert_size_close(actual: int, raw_bytes: int, file_count: int) -> None:
    assert actual >= raw_bytes, (
        f"expected du-reported size >= raw content bytes ({raw_bytes}), got {actual}"
    )
    upper_bound = raw_bytes + file_count * _DU_PER_FILE_MARGIN
    assert actual < upper_bound, (
        f"du-reported size {actual} unexpectedly far above raw content bytes "
        f"({raw_bytes}) + block-rounding margin (upper bound {upper_bound})"
    )


def _entries_by_path(result: dict, category: str) -> dict[Path, dict]:
    return {e["path"]: e for e in result["categories"][category]["entries"]}


# ---------------------------------------------------------------------------
# 1. Dry-run: filesystem unchanged, plan identifies every candidate (with
#    correct sizes) across all 4 filesystem categories.
# ---------------------------------------------------------------------------


def test_dry_run_leaves_filesystem_unchanged_but_plan_identifies_every_candidate(
    adapter, tmp_path, monkeypatch
):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj", filler_bytes=100)
    next_dir = root / "proj" / ".next"
    _write(next_dir / "cache" / "b.bin", 50)
    ds_store = root / ".DS_Store"
    _write(ds_store, 30)
    recycle_bin = root / "$RECYCLE.BIN"
    _write(recycle_bin / "c.dat", 40)
    orphaned_dmg = root / "app.dmg"
    _write(orphaned_dmg, 60)
    kept_dmg = root / "keep.dmg"
    _write(kept_dmg, 70)

    monkeypatch.setattr(
        adapter, "find_installed_app", lambda p: Path(p).name == "app.dmg"
    )

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False)
    result = reclaim.run(adapter, args)

    assert result["go"] is False
    assert result["roots"] == [root]

    # ---- Nothing on disk changed. ----
    assert nm.exists()
    assert next_dir.exists()
    assert ds_store.exists() and ds_store.read_bytes() == b"x" * 30
    assert recycle_bin.exists()
    assert orphaned_dmg.exists()
    assert kept_dmg.exists()

    # ---- build_caches: both node_modules and .next found, correctly sized. ----
    build_entries = _entries_by_path(result, "build_caches")
    assert set(build_entries) == {nm, next_dir}
    for entry in build_entries.values():
        assert entry["master_path_refused"] is False
        assert "deleted" not in entry  # dry-run: no execution outcome yet
    _assert_size_close(build_entries[nm]["size_bytes"], raw_bytes=100, file_count=1)
    _assert_size_close(build_entries[next_dir]["size_bytes"], raw_bytes=50, file_count=1)

    # ---- os_junk: exactly the .DS_Store, exact stat() size. ----
    junk_entries = _entries_by_path(result, "os_junk")
    assert set(junk_entries) == {ds_store}
    assert junk_entries[ds_store]["size_bytes"] == 30
    assert junk_entries[ds_store]["master_path_refused"] is False

    # ---- recycle_bin: the stray $RECYCLE.BIN dir. ----
    recycle_entries = _entries_by_path(result, "recycle_bin")
    assert set(recycle_entries) == {recycle_bin}
    _assert_size_close(recycle_entries[recycle_bin]["size_bytes"], raw_bytes=40, file_count=1)

    # ---- orphaned_installers: only the one find_installed_app() said was
    #      already installed; keep.dmg never even appears as a candidate. ----
    installer_entries = _entries_by_path(result, "orphaned_installers")
    assert set(installer_entries) == {orphaned_dmg}
    assert installer_entries[orphaned_dmg]["size_bytes"] == 60

    # ---- Totals: dry-run counts every non-refused entry (nothing refused
    #      here), matching a hand-computed sum from the entries themselves. ----
    all_entries = [
        *build_entries.values(),
        *junk_entries.values(),
        *recycle_entries.values(),
        *installer_entries.values(),
    ]
    expected_total = sum(e["size_bytes"] for e in all_entries)
    assert result["total_bytes_reclaimed"] == expected_total
    assert result["total_gb_reclaimed"] == round(expected_total / (1024**3), 4)


# ---------------------------------------------------------------------------
# 2. --go actually deletes matching candidates; non-matching lookalikes
#    (a plain "modules" dir, a ".DS_Storee" file) are left untouched.
# ---------------------------------------------------------------------------


def test_go_deletes_matches_and_leaves_lookalikes_untouched(adapter, tmp_path):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")
    next_dir = root / "proj" / ".next"
    _write(next_dir / "cache" / "b.bin", 50)
    ds_store = root / ".DS_Store"
    _write(ds_store, 30)
    recycle_bin = root / "$RECYCLE.BIN"
    _write(recycle_bin / "c.dat", 40)

    # Lookalikes that must NOT be touched.
    plain_modules = root / "modules"
    _write(plain_modules / "file.txt", 10)
    ds_storee = root / ".DS_Storee"
    _write(ds_storee, 5)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)

    assert result["go"] is True

    # ---- Real candidates: gone. ----
    assert not nm.exists()
    assert not next_dir.exists()
    assert not ds_store.exists()
    assert not recycle_bin.exists()

    # ---- Lookalikes: untouched. ----
    assert plain_modules.exists()
    assert (plain_modules / "file.txt").exists()
    assert ds_storee.exists()
    assert ds_storee.read_bytes() == b"x" * 5

    # ---- Every deleted entry recorded as such. ----
    for category in ("build_caches", "os_junk", "recycle_bin"):
        for entry in result["categories"][category]["entries"]:
            assert entry["deleted"] is True
            assert entry["error"] is None


# ---------------------------------------------------------------------------
# 3. Master-paths refusal -- the single most important safety property in
#    this file. An unbacked-up master path is NEVER deleted, in EITHER
#    nesting direction, and shows up with the exact refusal reason in BOTH
#    dry-run and --go output. backed_up=True on the same path gets no
#    special protection -- normal deletion applies.
# ---------------------------------------------------------------------------


def test_master_path_refusal_is_never_deleted_in_either_direction(adapter, tmp_path):
    root = tmp_path / "root"

    # Case 1: master path IS the candidate itself, backed_up=False -> refused.
    nm1 = _mk_node_modules(root / "case1_master_is_candidate")

    # Case 2: master path IS the candidate itself, backed_up=True -> NOT
    # refused; normal deletion applies.
    nm2 = _mk_node_modules(root / "case2_master_is_candidate_backed_up")

    # Case 3: master path is a file NESTED INSIDE a candidate dir that would
    # otherwise be deleted, backed_up=False -> refused (deleting the
    # candidate would take the master path with it).
    nm3 = _mk_node_modules(root / "case3_master_nested_in_candidate")
    master_file3 = nm3 / "precious" / "config.json"
    _write(master_file3, 20)

    # Case 4: same nesting as case 3, but backed_up=True -> NOT refused; the
    # whole candidate (including the nested "master" file) is deleted
    # normally, since a backed-up master path gets no special protection.
    nm4 = _mk_node_modules(root / "case4_master_nested_in_candidate_backed_up")
    master_file4 = nm4 / "precious" / "config.json"
    _write(master_file4, 20)

    # Case 5 (bonus direction): the CANDIDATE is nested inside a master-path
    # directory, backed_up=False -> refused (the candidate is part of the
    # master path's tree).
    master_dir5 = root / "case5_candidate_inside_master"
    master_dir5.mkdir(parents=True)
    nm5 = _mk_node_modules(master_dir5)

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[
            MasterPath(path=str(nm1), backed_up=False),
            MasterPath(path=str(nm2), backed_up=True),
            MasterPath(path=str(master_file3), backed_up=False),
            MasterPath(path=str(master_file4), backed_up=True),
            MasterPath(path=str(master_dir5), backed_up=False),
        ],
    )
    save_config(adapter, config)

    args_dry = SimpleNamespace(dir=[str(root)], go=False, docker=False)
    dry_result = reclaim.run(adapter, args_dry)
    dry_entries = _entries_by_path(dry_result, "build_caches")

    refused_paths = {nm1, nm3, nm5}
    not_refused_paths = {nm2, nm4}

    for path in refused_paths:
        entry = dry_entries[path]
        assert entry["master_path_refused"] is True
        assert entry["deleted"] is False
        assert entry["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON

    for path in not_refused_paths:
        entry = dry_entries[path]
        assert entry["master_path_refused"] is False
        assert "reason" not in entry

    # The refusal reason and refused-ness are identical whether or not --go
    # is passed: nothing on disk has changed yet, so re-running dry-run
    # again is equivalent to asserting this is computed at plan time, not
    # deletion time.
    assert nm1.exists() and nm3.exists() and master_file3.exists() and nm5.exists()

    # ---- Now --go: refused paths must survive; non-refused ones must not. ----
    args_go = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    go_result = reclaim.run(adapter, args_go)
    go_entries = _entries_by_path(go_result, "build_caches")

    for path in refused_paths:
        entry = go_entries[path]
        assert entry["master_path_refused"] is True
        assert entry["deleted"] is False
        assert entry["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
        assert "error" not in entry

    for path in not_refused_paths:
        entry = go_entries[path]
        assert entry["master_path_refused"] is False
        assert entry["deleted"] is True
        assert entry["error"] is None

    # The actual, ultimate proof: filesystem state after --go.
    assert nm1.exists(), "unbacked-up master path (case 1) must survive --go"
    assert nm3.exists(), "candidate containing an unbacked-up master path (case 3) must survive --go"
    assert master_file3.exists(), "nested unbacked-up master path itself must survive --go"
    assert nm5.exists(), "candidate nested inside an unbacked-up master path (case 5) must survive --go"
    assert master_dir5.exists()

    assert not nm2.exists(), "backed_up=True master path gets no special protection"
    assert not nm4.exists(), "backed_up=True master path (nested) gets no special protection"

    # master_path_refusals surfaces exactly the refused cases, correctly
    # attributed, and none of the backed-up ones.
    refusal_index = {(r["path"], r["master_path"]) for r in go_result["master_path_refusals"]}
    assert refusal_index == {
        (nm1, str(nm1)),
        (nm3, str(master_file3)),
        (nm5, str(master_dir5)),
    }
    assert all(r["category"] == "build_caches" for r in go_result["master_path_refusals"])


# ---------------------------------------------------------------------------
# 4. Per-entry error isolation: one candidate's deletion raising OSError
#    doesn't abort the batch, and every other candidate still gets deleted.
# ---------------------------------------------------------------------------


def test_go_isolates_one_failing_delete_and_completes_the_rest(adapter, tmp_path, monkeypatch):
    root = tmp_path / "root"
    nm_good1 = _mk_node_modules(root / "proj1")
    nm_bad = _mk_node_modules(root / "proj2")
    nm_good2 = _mk_node_modules(root / "proj3")

    real_delete = adapter.delete

    def flaky_delete(path, recursive=False):
        if Path(path) == nm_bad:
            raise OSError("permission denied (simulated)")
        return real_delete(path, recursive=recursive)

    monkeypatch.setattr(adapter, "delete", flaky_delete)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)  # must not raise

    entries = _entries_by_path(result, "build_caches")

    bad_entry = entries[nm_bad]
    assert bad_entry["deleted"] is False
    assert "permission denied" in bad_entry["error"]
    assert nm_bad.exists()  # never actually touched

    for good_path in (nm_good1, nm_good2):
        good_entry = entries[good_path]
        assert good_entry["deleted"] is True
        assert good_entry["error"] is None
        assert not good_path.exists()


# ---------------------------------------------------------------------------
# 5. Orphaned installers: only the one whose app is already installed gets
#    deleted; a NotImplementedError from find_installed_app skips just that
#    category (not a crash) while every other category still runs.
# ---------------------------------------------------------------------------


def test_orphaned_installers_only_deletes_the_one_with_app_installed(
    adapter, tmp_path, monkeypatch
):
    root = tmp_path / "root"
    installed_dmg = root / "good.dmg"
    _write(installed_dmg, 40)
    not_installed_pkg = root / "keep.pkg"
    _write(not_installed_pkg, 60)

    monkeypatch.setattr(
        adapter, "find_installed_app", lambda p: Path(p).name == "good.dmg"
    )

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)

    entries = result["categories"]["orphaned_installers"]["entries"]
    assert {e["path"] for e in entries} == {installed_dmg}
    assert entries[0]["deleted"] is True

    assert not installed_dmg.exists()
    assert not_installed_pkg.exists()


def test_orphaned_installers_category_skipped_on_not_implemented_but_others_run(
    adapter, tmp_path, monkeypatch
):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")
    ds_store = root / ".DS_Store"
    _write(ds_store, 30)
    recycle_bin = root / "$RECYCLE.BIN"
    _write(recycle_bin / "c.dat", 40)
    _write(root / "some.dmg", 15)

    def boom(_path):
        raise NotImplementedError("find_installed_app not supported on this OS")

    monkeypatch.setattr(adapter, "find_installed_app", boom)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)  # must not raise

    installers_category = result["categories"]["orphaned_installers"]
    assert installers_category["skipped"] is True
    assert installers_category["reason"] == "not supported on this OS"
    assert installers_category["bytes_reclaimed"] == 0
    assert installers_category["gb_reclaimed"] == 0.0

    # The other three categories ran normally and actually deleted their
    # candidates, proving one category's exception didn't sink the batch.
    assert not nm.exists()
    assert not ds_store.exists()
    assert not recycle_bin.exists()
    assert result["categories"]["build_caches"]["entries"][0]["deleted"] is True
    assert result["categories"]["os_junk"]["entries"][0]["deleted"] is True
    assert result["categories"]["recycle_bin"]["entries"][0]["deleted"] is True

    # some.dmg was never even sized/deleted -- the whole category short-
    # circuited before candidates_with_sizes was built.
    assert (root / "some.dmg").exists()


# ---------------------------------------------------------------------------
# 6. Docker: `docker system prune` runs only when BOTH --go and --docker are
#    true. subprocess.run is monkeypatched to detect the docker invocation
#    while transparently delegating every other command (notably the real
#    `du -sk` calls dir_size_bytes makes) to the real subprocess.run -- so
#    this test never shells out to the real docker binary.
# ---------------------------------------------------------------------------


def test_docker_prune_only_runs_when_go_and_docker_are_both_true(adapter, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    monkeypatch.setattr(adapter, "is_docker_installed", lambda: True)

    docker_calls = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "docker":
            docker_calls.append(cmd)

            class _Result:
                stdout = "Total reclaimed space: 0B\n"

            return _Result()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(reclaim.subprocess, "run", fake_run)

    cases = [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ]
    for go, docker in cases:
        docker_calls.clear()
        args = SimpleNamespace(dir=[str(root)], go=go, docker=docker)
        result = reclaim.run(adapter, args)
        if go and docker:
            assert len(docker_calls) == 1, f"expected docker prune for go={go} docker={docker}"
            assert docker_calls[0][:3] == ["docker", "system", "prune"]
            assert result["docker"]["ran"] is True
        else:
            assert docker_calls == [], f"docker must NOT run for go={go} docker={docker}"
            assert result["docker"]["ran"] is False


# ---------------------------------------------------------------------------
# 7. GB-reclaimed totals: categories sum correctly, and the grand total
#    EXCLUDES docker's separately-reported reclaimed amount.
# ---------------------------------------------------------------------------


def test_total_bytes_reclaimed_sums_categories_and_excludes_docker(
    adapter, tmp_path, monkeypatch
):
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    ds_store1 = root1 / ".DS_Store"
    _write(ds_store1, 5000)
    ds_store2 = root2 / ".DS_Store"
    _write(ds_store2, 3000)
    installer = root1 / "app.dmg"
    _write(installer, 4000)

    monkeypatch.setattr(adapter, "find_installed_app", lambda p: True)
    monkeypatch.setattr(adapter, "is_docker_installed", lambda: True)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "docker":
            class _Result:
                stdout = "Total reclaimed space: 2GB\n"

            return _Result()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(reclaim.subprocess, "run", fake_run)

    args = SimpleNamespace(dir=[str(root1), str(root2)], go=True, docker=True)
    result = reclaim.run(adapter, args)

    assert result["categories"]["os_junk"]["bytes_reclaimed"] == 8000
    assert result["categories"]["orphaned_installers"]["bytes_reclaimed"] == 4000
    assert result["categories"]["build_caches"]["bytes_reclaimed"] == 0
    assert result["categories"]["recycle_bin"]["bytes_reclaimed"] == 0

    # Known, hand-computed from the fixture -- independent of the source's
    # own summation logic.
    assert result["total_bytes_reclaimed"] == 12000
    assert result["total_gb_reclaimed"] == round(12000 / (1024**3), 4)

    # Docker's own (much larger, easily distinguishable) reclaimed figure is
    # reported separately and must NOT be folded into the grand total.
    assert result["docker"]["bytes_reclaimed"] == 2 * 1024**3
    assert result["docker"]["gb_reclaimed"] == 2.0
    assert result["total_bytes_reclaimed"] == 12000  # unchanged by docker's 2GB
    assert result["total_gb_reclaimed"] != result["docker"]["gb_reclaimed"]


# ---------------------------------------------------------------------------
# 8. Deduplication: two overlapping junk-file glob patterns matching the
#    same real file produce exactly one plan entry, not two.
# ---------------------------------------------------------------------------


def test_overlapping_junk_patterns_produce_one_entry_not_two(adapter, tmp_path, monkeypatch):
    root = tmp_path / "root"
    ds_store = root / ".DS_Store"
    _write(ds_store, 30)

    # Two distinct patterns that both match the same real filename:
    # ".DS_Store" (exact match) and ".DS_S*" (glob match) both hit
    # ".DS_Store", so without dedupe this file would be found twice.
    monkeypatch.setattr(reclaim, "OS_JUNK_PATTERNS", (".DS_Store", ".DS_S*"))

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False)
    result = reclaim.run(adapter, args)

    entries = result["categories"]["os_junk"]["entries"]
    assert len(entries) == 1
    assert entries[0]["path"] == ds_store


# ---------------------------------------------------------------------------
# 7. Master-path refusal bypass regressions (the two bugs a reviewer found
#    that could let ``--go`` silently delete a supposedly-protected master
#    path): (a) an un-canonicalized relative/``..``-containing master path
#    not matching an equivalent absolute candidate, and (b) case
#    differences not being recognized as the same on-disk entry.
# ---------------------------------------------------------------------------


def _is_case_insensitive_fs(tmp_path: Path) -> bool:
    """Best-effort runtime probe: does this filesystem fold case on lookup?

    macOS's default APFS/HFS+ does (confirmed empirically for this suite);
    a typical Linux CI runner's ext4 does not. Bug 2's on-disk regression
    test below only exercises anything meaningful on a case-insensitive
    filesystem -- on a case-sensitive one, ``NODE_MODULES`` and
    ``node_modules`` are simply two different, unrelated directories, and
    there is nothing to bypass. The helper-level unit test further down
    covers the same logic unconditionally, regardless of what this probe
    returns.
    """
    probe = tmp_path / "_case_probe_marker"
    probe.mkdir()
    return probe.parent.joinpath("_CASE_PROBE_MARKER").exists()


def test_master_path_relative_and_dotdot_path_is_still_refused(adapter, tmp_path):
    """BUG 1 regression: a master path configured as a relative path (with
    ``..`` segments) that resolves to the exact same directory as an
    absolute candidate must still be refused, not silently bypassed because
    the two strings never compared equal.
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")  # absolute candidate, as found by the scan

    # Build a deliberately relative, ..-laden path string that resolves to
    # the identical directory as `nm`, entirely independent of cwd.
    other_side = tmp_path / "elsewhere" / "unrelated"
    other_side.mkdir(parents=True)
    rel_master_path = os.path.relpath(nm, start=other_side)
    assert not Path(rel_master_path).is_absolute()
    assert ".." in Path(rel_master_path).parts
    # Sanity: resolving it relative to `other_side` really does land on `nm`.
    assert (other_side / rel_master_path).resolve() == nm.resolve()

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=str(other_side / rel_master_path), backed_up=False)],
    )
    save_config(adapter, config)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)

    entry = _entries_by_path(result, "build_caches")[nm]
    assert entry["master_path_refused"] is True
    assert entry["deleted"] is False
    assert entry["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
    assert nm.exists(), "relative/..-path master path bypass: candidate must survive --go"


def test_master_path_pure_relative_path_is_still_refused(adapter, tmp_path, monkeypatch):
    """BUG 1 regression, the other half: a master path configured as a
    genuinely relative path string (no leading ``/`` at all, resolved
    against the process's cwd) must still be refused when it names the same
    directory as an absolute candidate.
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")

    monkeypatch.chdir(tmp_path)
    rel_master_path = os.path.relpath(nm, start=tmp_path)
    assert not Path(rel_master_path).is_absolute()

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=rel_master_path, backed_up=False)],
    )
    save_config(adapter, config)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)

    entry = _entries_by_path(result, "build_caches")[nm]
    assert entry["master_path_refused"] is True
    assert entry["deleted"] is False
    assert nm.exists(), "pure relative-path master path bypass: candidate must survive --go"


def test_master_path_case_mismatch_on_disk_is_refused(adapter, tmp_path):
    """BUG 2 regression: a master path configured with different case than
    the real on-disk candidate must still be refused when the filesystem is
    case-insensitive (confirmed on macOS/APFS for this suite), since the two
    paths are the identical directory on disk despite differing as strings.

    Skips (rather than silently passing) on a case-sensitive filesystem,
    since there the two differently-cased paths are genuinely unrelated
    directories and this scenario cannot be reproduced at the filesystem
    level at all -- see ``test_same_path_ci_and_is_relative_to_ci`` below for
    the filesystem-independent unit-level coverage of the same logic.
    """
    if not _is_case_insensitive_fs(tmp_path):
        pytest.skip(
            "filesystem is case-sensitive; this on-disk repro of bug 2 does "
            "not apply here (see the helper-level unit test for coverage "
            "that doesn't depend on filesystem case-folding)"
        )

    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")  # actual on-disk case: "node_modules"

    mismatched_case_path = nm.parent / "NODE_MODULES"
    assert mismatched_case_path.exists(), "sanity: case-insensitive fs must see it"
    assert mismatched_case_path.samefile(nm), "sanity: same on-disk entry, different case"

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=str(mismatched_case_path), backed_up=False)],
    )
    save_config(adapter, config)

    args = SimpleNamespace(dir=[str(root)], go=True, docker=False)
    result = reclaim.run(adapter, args)

    entry = _entries_by_path(result, "build_caches")[nm]
    assert entry["master_path_refused"] is True
    assert entry["deleted"] is False
    assert entry["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
    assert nm.exists(), "case-mismatched master path bypass: candidate must survive --go"


def test_same_path_ci_and_is_relative_to_ci(tmp_path):
    """Filesystem-independent unit coverage of the case-insensitive helpers
    themselves, so bug 2's logic is verified even on a case-sensitive CI
    runner where the on-disk regression test above has to skip.

    Exercises both the "master path exists on disk" path (via ``samefile``)
    and the "master path doesn't exist" fallback (pure lowercased string
    comparison), plus the boundary-safety requirement that a short sibling
    name (e.g. ``/a/bc``) is never mistaken for being nested under a
    similarly-prefixed one (``/a/b``).
    """
    real_dir = tmp_path / "node_modules"
    real_dir.mkdir()

    # Case-mismatched but existing-on-disk-under-either-spelling paths: on a
    # case-insensitive filesystem this hits the samefile() branch; on a
    # case-sensitive one, Path(...).exists() is False for the mismatched
    # spelling, so this exercises the pure-string fallback instead. Either
    # way, this must report "same path".
    assert reclaim._same_path_ci(real_dir, tmp_path / "NODE_MODULES") is True

    # Neither side exists on disk at all -> pure case-insensitive string
    # fallback must still recognize them as equal.
    ghost_a = tmp_path / "ghost" / "config.json"
    ghost_b = tmp_path / "GHOST" / "CONFIG.JSON"
    assert reclaim._same_path_ci(ghost_a, ghost_b) is True

    # Genuinely different paths must never be conflated.
    assert reclaim._same_path_ci(tmp_path / "a", tmp_path / "b") is False

    # Containment, case-insensitively, in both directions.
    parent = tmp_path / "Proj" / "node_modules"
    child = tmp_path / "proj" / "NODE_MODULES" / "pkg" / "file.js"
    assert reclaim._is_relative_to_ci(child, parent) is True
    assert reclaim._is_relative_to_ci(parent, child) is False
    assert reclaim._is_relative_to_ci(parent, parent) is True  # equality counts as contained

    # Boundary safety: "/a/bc" must never be treated as relative to "/a/b".
    sibling = tmp_path / "abc"
    short_parent = tmp_path / "ab"
    assert reclaim._is_relative_to_ci(sibling, short_parent) is False


# ---------------------------------------------------------------------------
# 8. --from-queue: executes only approved, queue-sourced ``delete`` entries
#    under a resolved root. See reclaim._run_from_queue's docstring for the
#    master-path-refusal/staleness/isolation/single-lock-cycle contract these
#    tests pin down.
#
#    Section 8f below is the highest-stakes coverage in this file: it re-runs
#    the exact master-path bypass regression fixtures from section 7 above,
#    but drives them through an APPROVED queue entry + --from-queue instead
#    of --go, proving the master-path refusal (the one non-negotiable safety
#    rule in this whole module) is not accidentally bypassable just because
#    the delete was requested via the queue path rather than a fresh plan.
# ---------------------------------------------------------------------------


def _seed_queue(adapter, entries: list[QueueEntry]) -> Path:
    """Write ``entries`` straight to the (fake-home) default queue path."""
    path = queue_module.default_queue_path(adapter)
    queue_module.save_queue(adapter, entries, path)
    return path


def _reload_queue(adapter) -> list[QueueEntry]:
    return queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))


# ---- 8a. Only approved delete entries execute. -----------------------------


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_from_queue_executes_only_approved_delete_entries(adapter, tmp_path, status):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")

    entry = QueueEntry(
        action="delete", src=str(nm), status=status, plan_snapshot=build_plan_snapshot(nm)
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    if status == "approved":
        assert report["qualifying_count"] == 1
        assert report["executed"] == [{"id": entry.id, "src": str(nm)}]
        assert not nm.exists()
    else:
        assert report["qualifying_count"] == 0
        assert report["executed"] == []
        assert nm.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == status


def test_from_queue_ignores_approved_non_delete_action(adapter, tmp_path):
    """An approved queue entry whose action isn't "delete" must never
    execute through reclaim's --from-queue, confirming the "right action
    type" half of the filter.
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")

    entry = QueueEntry(
        action="move",
        src=str(nm),
        dest=str(root / "elsewhere"),
        status="approved",
        plan_snapshot=build_plan_snapshot(nm),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["qualifying_count"] == 0
    assert report["executed"] == []
    assert nm.exists()


# ---- 8b. Staleness: skipped, status/execution fields untouched, reported. --


@pytest.mark.parametrize("stale_mode", ["changed", "deleted"])
def test_from_queue_stale_entry_is_skipped_status_and_execution_fields_untouched(
    adapter, tmp_path, stale_mode
):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")
    snapshot = build_plan_snapshot(nm)

    if stale_mode == "changed":
        # A directory's plan_snapshot only records mtime (no content_hash),
        # so an unambiguous, filesystem-resolution-proof mtime change is
        # enough to make it stale.
        os.utime(nm, (0, 0))
    else:
        adapter.delete(nm, recursive=True)

    entry = QueueEntry(
        action="delete", src=str(nm), status="approved", plan_snapshot=snapshot
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["qualifying_count"] == 1
    assert report["executed"] == []
    assert report["failed"] == []
    assert report["refused"] == []
    assert len(report["stale"]) == 1
    assert report["stale"][0]["id"] == entry.id
    assert report["stale"][0]["src"] == str(nm)

    if stale_mode == "changed":
        assert nm.exists()  # never touched

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None
    assert reloaded.execution_error is None


# ---- 8c. Successful execution: executed_at set, execution_error cleared,
#          status unchanged. --------------------------------------------------


def test_from_queue_successful_execution_sets_executed_at_and_clears_error(adapter, tmp_path):
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")

    entry = QueueEntry(
        action="delete", src=str(nm), status="approved", plan_snapshot=build_plan_snapshot(nm)
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)

    assert result["from_queue"]["executed"] == [{"id": entry.id, "src": str(nm)}]
    assert not nm.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"  # execution never touches status
    assert reloaded.executed_at is not None
    assert reloaded.execution_error is None
    datetime.fromisoformat(reloaded.executed_at)  # real, parseable timestamp


# ---- 8d. Failed execution: isolated per-entry, batch continues. -----------


def test_from_queue_failed_execution_isolated_per_entry(adapter, tmp_path, monkeypatch):
    root = tmp_path / "root"
    nm_good = _mk_node_modules(root / "proj1")
    nm_bad = _mk_node_modules(root / "proj2")

    good_entry = QueueEntry(
        action="delete",
        src=str(nm_good),
        status="approved",
        plan_snapshot=build_plan_snapshot(nm_good),
    )
    bad_entry = QueueEntry(
        action="delete",
        src=str(nm_bad),
        status="approved",
        plan_snapshot=build_plan_snapshot(nm_bad),
    )
    _seed_queue(adapter, [bad_entry, good_entry])

    real_delete = adapter.delete

    def flaky_delete(path, recursive=False):
        if Path(path) == nm_bad:
            raise OSError("permission denied (simulated)")
        return real_delete(path, recursive=recursive)

    monkeypatch.setattr(adapter, "delete", flaky_delete)

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)  # must not raise
    report = result["from_queue"]

    assert len(report["failed"]) == 1
    assert report["failed"][0]["id"] == bad_entry.id
    assert report["failed"][0]["error"] == "permission denied (simulated)"

    assert len(report["executed"]) == 1
    assert report["executed"][0]["id"] == good_entry.id

    by_id = {e.id: e for e in _reload_queue(adapter)}

    bad_reloaded = by_id[bad_entry.id]
    assert bad_reloaded.status == "approved"
    assert bad_reloaded.executed_at is not None  # set on failure too
    assert bad_reloaded.execution_error == "permission denied (simulated)"
    assert nm_bad.exists()  # never actually touched

    good_reloaded = by_id[good_entry.id]
    assert good_reloaded.status == "approved"
    assert good_reloaded.executed_at is not None
    assert good_reloaded.execution_error is None
    assert not nm_good.exists()


def test_from_queue_non_oserror_mid_batch_does_not_lose_prior_entrys_result(
    adapter, tmp_path, monkeypatch
):
    """CRITICAL regression: a non-OSError exception raised while deleting one
    entry must not prevent save_queue from persisting the outcome already
    recorded on an *earlier* entry in the same batch.

    This is the reclaim-specific worst case called out in the bug report: a
    real deletion can succeed on disk, but if the exception that follows it
    (for some other, unrelated entry) escapes uncaught, save_queue never
    runs -- so a later run would see the first entry's now-missing src and
    misreport it as merely "stale, re-plan" instead of "already executed",
    permanently losing the record that the delete actually happened. Before
    the fix, _run_from_queue only caught ``except OSError``, so a ValueError
    (or any other non-OSError) would propagate straight out of the whole
    function, past save_queue entirely.
    """
    root = tmp_path / "root"
    nm_first = _mk_node_modules(root / "proj1")
    nm_second = _mk_node_modules(root / "proj2")

    first_entry = QueueEntry(
        action="delete",
        src=str(nm_first),
        status="approved",
        plan_snapshot=build_plan_snapshot(nm_first),
    )
    second_entry = QueueEntry(
        action="delete",
        src=str(nm_second),
        status="approved",
        plan_snapshot=build_plan_snapshot(nm_second),
    )
    _seed_queue(adapter, [first_entry, second_entry])

    real_delete = adapter.delete

    def flaky_delete(path, recursive=False):
        if Path(path) == nm_second:
            raise ValueError("unexpected bug (simulated), not an OSError")
        return real_delete(path, recursive=recursive)

    monkeypatch.setattr(adapter, "delete", flaky_delete)

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)  # must not raise
    report = result["from_queue"]

    assert len(report["executed"]) == 1
    assert report["executed"][0]["id"] == first_entry.id

    assert len(report["failed"]) == 1
    assert report["failed"][0]["id"] == second_entry.id
    assert report["failed"][0]["error"] == "unexpected bug (simulated), not an OSError"

    # The queue file was still saved -- the first entry's already-completed
    # deletion is not lost just because the second entry blew up with
    # something other than OSError.
    by_id = {e.id: e for e in _reload_queue(adapter)}

    first_reloaded = by_id[first_entry.id]
    assert first_reloaded.status == "approved"
    assert first_reloaded.executed_at is not None
    assert first_reloaded.execution_error is None
    assert not nm_first.exists()  # the deletion really happened

    second_reloaded = by_id[second_entry.id]
    assert second_reloaded.status == "approved"
    assert second_reloaded.executed_at is not None
    assert second_reloaded.execution_error == "unexpected bug (simulated), not an OSError"
    assert nm_second.exists()  # never actually deleted


# ---- 8e. Single lock/save cycle per invocation, regardless of entry count. -


def test_from_queue_saves_queue_exactly_once_per_invocation(adapter, tmp_path, monkeypatch):
    root = tmp_path / "root"
    entries = []
    for i in range(4):
        nm = _mk_node_modules(root / f"proj{i}")
        entries.append(
            QueueEntry(
                action="delete",
                src=str(nm),
                status="approved",
                plan_snapshot=build_plan_snapshot(nm),
            )
        )
    _seed_queue(adapter, entries)

    save_calls: list[list[QueueEntry]] = []
    real_save = queue_module.save_queue

    def spy_save(adapter_, entries_, path=None):
        save_calls.append(list(entries_))
        return real_save(adapter_, entries_, path)

    monkeypatch.setattr(reclaim.queue_module, "save_queue", spy_save)

    lock_calls: list[Path] = []
    real_lock = queue_module.with_queue_lock

    @contextlib.contextmanager
    def spy_lock(adapter_, path):
        lock_calls.append(path)
        with real_lock(adapter_, path):
            yield

    monkeypatch.setattr(reclaim.queue_module, "with_queue_lock", spy_lock)

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)

    assert result["from_queue"]["qualifying_count"] == 4
    assert len(result["from_queue"]["executed"]) == 4
    assert len(save_calls) == 1
    assert len(lock_calls) == 1


# ---- 8f. CRITICAL: the master-path bypass regression fixtures from section
#          7, re-driven through --from-queue instead of --go. -------------


def test_master_path_relative_and_dotdot_path_is_still_refused_via_from_queue(adapter, tmp_path):
    """BUG 1 regression, via --from-queue: a master path configured as a
    relative path (with ``..`` segments) that resolves to the exact same
    directory as an approved queue entry's ``src`` must still be refused,
    not silently executed because the two strings never compared equal.
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")  # absolute src, as an approved entry would carry

    other_side = tmp_path / "elsewhere" / "unrelated"
    other_side.mkdir(parents=True)
    rel_master_path = os.path.relpath(nm, start=other_side)
    assert not Path(rel_master_path).is_absolute()
    assert ".." in Path(rel_master_path).parts
    assert (other_side / rel_master_path).resolve() == nm.resolve()

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=str(other_side / rel_master_path), backed_up=False)],
    )
    save_config(adapter, config)

    entry = QueueEntry(
        action="delete", src=str(nm), status="approved", plan_snapshot=build_plan_snapshot(nm)
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["refused"]) == 1
    assert report["refused"][0]["id"] == entry.id
    assert report["refused"][0]["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
    assert (
        nm.exists()
    ), "relative/..-path master path bypass: candidate must survive --from-queue"

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None
    assert reloaded.execution_error is None


def test_master_path_pure_relative_path_is_still_refused_via_from_queue(
    adapter, tmp_path, monkeypatch
):
    """BUG 1 regression, the other half, via --from-queue: a master path
    configured as a genuinely relative path string (resolved against the
    process's cwd) must still be refused when it names the same directory as
    an approved queue entry's ``src``.
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")

    monkeypatch.chdir(tmp_path)
    rel_master_path = os.path.relpath(nm, start=tmp_path)
    assert not Path(rel_master_path).is_absolute()

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=rel_master_path, backed_up=False)],
    )
    save_config(adapter, config)

    entry = QueueEntry(
        action="delete", src=str(nm), status="approved", plan_snapshot=build_plan_snapshot(nm)
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["refused"]) == 1
    assert report["refused"][0]["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
    assert nm.exists(), "pure relative-path master path bypass: candidate must survive --from-queue"

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None


def test_master_path_case_mismatch_on_disk_is_refused_via_from_queue(adapter, tmp_path):
    """BUG 2 regression, via --from-queue: a master path configured with
    different case than the real on-disk src must still be refused when the
    filesystem is case-insensitive, since the two paths are the identical
    directory on disk despite differing as strings. Skips on a
    case-sensitive filesystem, mirroring section 7's on-disk repro (the
    filesystem-independent unit coverage in
    ``test_same_path_ci_and_is_relative_to_ci`` already covers the logic
    unconditionally).
    """
    if not _is_case_insensitive_fs(tmp_path):
        pytest.skip(
            "filesystem is case-sensitive; this on-disk repro of bug 2 does "
            "not apply here (see the helper-level unit test for coverage "
            "that doesn't depend on filesystem case-folding)"
        )

    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")  # actual on-disk case: "node_modules"

    mismatched_case_path = nm.parent / "NODE_MODULES"
    assert mismatched_case_path.exists(), "sanity: case-insensitive fs must see it"
    assert mismatched_case_path.samefile(nm), "sanity: same on-disk entry, different case"

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=str(mismatched_case_path), backed_up=False)],
    )
    save_config(adapter, config)

    entry = QueueEntry(
        action="delete", src=str(nm), status="approved", plan_snapshot=build_plan_snapshot(nm)
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["refused"]) == 1
    assert report["refused"][0]["reason"] == reclaim.MASTER_PATH_REFUSAL_REASON
    assert (
        nm.exists()
    ), "case-mismatched master path bypass: candidate must survive --from-queue"

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None


def test_master_path_refusal_beats_staleness_via_from_queue(adapter, tmp_path):
    """The master-path check always runs and always wins, even for an entry
    that is ALSO stale -- refusal is unconditional and never short-circuited
    by the staleness result (see _run_from_queue's docstring).
    """
    root = tmp_path / "root"
    nm = _mk_node_modules(root / "proj")
    snapshot = build_plan_snapshot(nm)
    # Make it stale too.
    os.utime(nm, (0, 0))

    config = Config(
        bucket_rules=DEFAULT_BUCKET_RULES,
        master_paths=[MasterPath(path=str(nm), backed_up=False)],
    )
    save_config(adapter, config)

    entry = QueueEntry(action="delete", src=str(nm), status="approved", plan_snapshot=snapshot)
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(root)], go=False, docker=False, from_queue=True)
    result = reclaim.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert report["stale"] == []  # refusal took it before staleness was reported
    assert len(report["refused"]) == 1
    assert report["refused"][0]["id"] == entry.id
    assert nm.exists()
