"""Tests for cleanup_tools.commands.corral_screenshots.run() -- the Python
port of scripts/corral-screenshots.sh.

Mirrors the fake-home pattern in test_sort.py/test_reclaim.py: a
MacOSAdapter subclass whose resolve_home() is redirected under tmp_path, so
config_module.load_config()'s default path (and adapter.resolve_standard_dir()
for desktop/downloads/documents/pictures) never resolve to -- let alone read,
write, or move -- anything outside tmp_path.

set_screenshot_save_location() is ALWAYS monkeypatched to a spy in this file
(never left to call the real MacOSAdapter implementation): that method shells
out to real `defaults write`/`killall` commands that would touch the actual
developer machine's screenshot preference, which must never happen just by
running this test suite. See test_adapters.py for coverage of the real
subprocess calls (also mocked there).

corral_screenshots.run(adapter, args) reads args.dir/args.go/args.from_queue/
args.set_default_location via getattr(), so a plain
types.SimpleNamespace(...) stands in for the argparse Namespace main() would
normally pass.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.commands import corral_screenshots
from cleanup_tools.config import Config, DEFAULT_BUCKET_RULES, save_config
from cleanup_tools import queue as queue_module
from cleanup_tools.queue import QueueEntry, build_plan_snapshot


def _make_fake_adapter(home: Path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

    return FakeHomeAdapter()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture
def adapter(home: Path, monkeypatch) -> MacOSAdapter:
    a = _make_fake_adapter(home)
    # Never let a test in this file reach the real `defaults write`/`killall`
    # calls -- record invocations instead. Individual tests that need to
    # simulate failure (e.g. NotImplementedError) override this further.
    calls: list[Path] = []
    monkeypatch.setattr(a, "set_screenshot_save_location", lambda p: calls.append(Path(p)))
    a._set_default_location_calls = calls  # type: ignore[attr-defined]
    return a


def _default_roots(home: Path) -> tuple[Path, Path, Path]:
    return (home / "Desktop", home / "Downloads", home / "Documents")


def _mkdirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Dry-run: filesystem unchanged, plan describes what WOULD move.
# ---------------------------------------------------------------------------


def test_dry_run_leaves_fixture_tree_unchanged(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    shot1 = desktop / "screenshot-2024.png"
    shot1.write_text("shot1-bytes")
    shot2 = downloads / "Screenshot 2024-01-01.png"
    shot2.write_text("shot2-bytes")
    not_a_shot = documents / "report.pdf"
    not_a_shot.write_text("report-bytes")

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert result["go"] is False
    assert set(result["roots"]) == {desktop, downloads, documents}

    # Nothing on disk moved.
    assert shot1.exists() and shot1.read_text() == "shot1-bytes"
    assert shot2.exists() and shot2.read_text() == "shot2-bytes"
    assert not_a_shot.exists()
    assert not (home / "Pictures" / "Screenshots").exists()

    srcs = {entry["src"] for entry in result["plan"]}
    assert srcs == {shot1, shot2}
    for entry in result["plan"]:
        assert entry["dest"].parent == home / "Pictures" / "Screenshots"
        assert entry["dest"].name == entry["src"].name
        assert entry["dest_exists"] is False
        assert "moved" not in entry  # not executed in dry-run

    # No set_screenshot_save_location call without the flag.
    assert adapter._set_default_location_calls == []


# ---------------------------------------------------------------------------
# 2. --go moves every screenshot* match (depth <= 2) with an exact count.
# ---------------------------------------------------------------------------


def test_go_moves_every_match_with_exact_moved_count(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    top = desktop / "screenshot-top.png"
    top.write_text("top")
    nested = downloads / "sub" / "screenshot-nested.png"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested")
    # list_dir(max_depth=2) collects files up through the directory two
    # levels down (depth 0/1/2 all yield their files; only descent past
    # depth 2 is pruned) -- so a file three levels down is the first one
    # that must NOT match, matching list_dir's own depth semantics (see
    # OSAdapter.list_dir / test_adapters.py's test_list_dir_respects_max_depth).
    too_deep = documents / "a" / "b" / "c" / "screenshot-too-deep.png"
    too_deep.parent.mkdir(parents=True)
    too_deep.write_text("too-deep")
    # Non-screenshot file: must never move.
    other = documents / "notes.txt"
    other.write_text("notes")

    args = SimpleNamespace(dir=[], go=True, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    dest_dir = home / "Pictures" / "Screenshots"
    assert result["moved_count"] == 2
    assert not top.exists()
    assert not nested.exists()
    assert (dest_dir / "screenshot-top.png").read_text() == "top"
    assert (dest_dir / "screenshot-nested.png").read_text() == "nested"

    # Too-deep match never appeared in the plan at all, and was never moved.
    plan_names = {e["src"].name for e in result["plan"]}
    assert plan_names == {"screenshot-top.png", "screenshot-nested.png"}
    assert too_deep.exists()
    assert other.exists()

    for entry in result["plan"]:
        assert entry["moved"] is True
        assert entry["error"] is None


# ---------------------------------------------------------------------------
# 3. dest_exists entries are skipped, never overwritten.
# ---------------------------------------------------------------------------


def test_go_skips_dest_exists_entry_without_overwriting(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    src = desktop / "screenshot-dupe.png"
    src.write_text("new-content")

    dest_dir = home / "Pictures" / "Screenshots"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "screenshot-dupe.png"
    dest.write_text("precious-old-content")

    # A second, non-conflicting match to prove the exact count only reflects
    # entries that actually moved, not every match found.
    other_src = downloads / "screenshot-other.png"
    other_src.write_text("other-content")

    args = SimpleNamespace(dir=[], go=True, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert result["moved_count"] == 1

    by_name = {e["src"].name: e for e in result["plan"]}
    dupe_entry = by_name["screenshot-dupe.png"]
    assert dupe_entry["dest_exists"] is True
    assert dupe_entry["moved"] is False
    assert dupe_entry["error"] == "destination already exists, skipped to avoid overwrite"

    other_entry = by_name["screenshot-other.png"]
    assert other_entry["moved"] is True

    # No overwrite: pre-existing destination content survives, source
    # untouched.
    assert dest.read_text() == "precious-old-content"
    assert src.exists()
    assert src.read_text() == "new-content"
    assert not other_src.exists()


def test_dry_run_flags_dest_exists_without_touching_filesystem(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    src = desktop / "screenshot-dupe.png"
    src.write_text("new-content")
    dest_dir = home / "Pictures" / "Screenshots"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "screenshot-dupe.png"
    dest.write_text("precious-old-content")

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    entry = result["plan"][0]
    assert entry["dest_exists"] is True
    assert "moved" not in entry
    assert dest.read_text() == "precious-old-content"
    assert src.read_text() == "new-content"


# ---------------------------------------------------------------------------
# 4. --from-queue executes only approved move entries under resolved roots,
#    with staleness re-checked.
# ---------------------------------------------------------------------------


def _seed_queue(adapter, entries: list[QueueEntry]) -> Path:
    path = queue_module.default_queue_path(adapter)
    queue_module.save_queue(adapter, entries, path)
    return path


def _reload_queue(adapter) -> list[QueueEntry]:
    return queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_from_queue_executes_only_approved_move_entries_under_roots(adapter, home, status):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    src = desktop / "screenshot-q.png"
    src.write_text("content")
    dest = home / "Pictures" / "Screenshots" / "screenshot-q.png"

    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status=status,
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=False)
    result = corral_screenshots.run(adapter, args)
    report = result["from_queue"]

    if status == "approved":
        assert report["qualifying_count"] == 1
        assert report["executed"] == [{"id": entry.id, "src": str(src), "dest": str(dest)}]
        assert not src.exists()
        assert dest.exists()
    else:
        assert report["qualifying_count"] == 0
        assert report["executed"] == []
        assert src.exists()
        assert not dest.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == status


def test_from_queue_ignores_entries_outside_resolved_roots(adapter, home, tmp_path):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    outside = tmp_path / "elsewhere" / "screenshot-outside.png"
    outside.parent.mkdir(parents=True)
    outside.write_text("content")
    dest = home / "Pictures" / "Screenshots" / "screenshot-outside.png"

    entry = QueueEntry(
        action="move",
        src=str(outside),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(outside),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=False)
    result = corral_screenshots.run(adapter, args)
    report = result["from_queue"]

    assert report["qualifying_count"] == 0
    assert outside.exists()


def test_from_queue_stale_entry_is_skipped_and_reported(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    src = desktop / "screenshot-stale.png"
    src.write_text("original")
    dest = home / "Pictures" / "Screenshots" / "screenshot-stale.png"
    snapshot = build_plan_snapshot(src)
    src.write_text("changed after snapshot, different length")

    entry = QueueEntry(
        action="move", src=str(src), dest=str(dest), status="approved", plan_snapshot=snapshot
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=False)
    result = corral_screenshots.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["stale"]) == 1
    assert report["stale"][0]["id"] == entry.id
    assert src.exists()
    assert not dest.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None


def test_from_queue_dest_exists_is_skipped_not_overwritten(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    src = desktop / "screenshot-dupe.png"
    src.write_text("new-content")
    dest_dir = home / "Pictures" / "Screenshots"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "screenshot-dupe.png"
    dest.write_text("must-survive")

    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=False)
    result = corral_screenshots.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["skipped"]) == 1
    assert dest.read_text() == "must-survive"
    assert src.exists()


# ---------------------------------------------------------------------------
# 5. Default roots resolve from config.search_roots when configured, else
#    the Desktop/Downloads/Documents fallback; CLI-supplied dirs win over
#    both.
# ---------------------------------------------------------------------------


def test_default_roots_fall_back_to_desktop_downloads_documents(adapter, home):
    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert set(result["roots"]) == {
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
    }


def test_search_roots_from_config_override_the_fallback(adapter, home, tmp_path):
    custom_root = tmp_path / "custom-root"
    custom_root.mkdir()
    config = Config(bucket_rules=DEFAULT_BUCKET_RULES, search_roots=[str(custom_root)])
    save_config(adapter, config)

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert result["roots"] == [custom_root]


def test_cli_supplied_dirs_win_over_search_roots_and_fallback(adapter, home, tmp_path):
    custom_root = tmp_path / "custom-root"
    custom_root.mkdir()
    config = Config(bucket_rules=DEFAULT_BUCKET_RULES, search_roots=[str(custom_root)])
    save_config(adapter, config)

    cli_dir = tmp_path / "cli-supplied"
    cli_dir.mkdir()

    args = SimpleNamespace(
        dir=[str(cli_dir)], go=False, from_queue=False, set_default_location=False
    )
    result = corral_screenshots.run(adapter, args)

    assert result["roots"] == [cli_dir]


# ---------------------------------------------------------------------------
# 6. --set-default-location independence: every combination of --go and
#    --set-default-location, checked explicitly.
# ---------------------------------------------------------------------------


def test_go_alone_never_calls_set_default_location(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    (desktop / "screenshot-a.png").write_text("a")

    args = SimpleNamespace(dir=[], go=True, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert result["moved_count"] == 1
    assert adapter._set_default_location_calls == []
    assert "set_default_location" not in result


def test_from_queue_alone_never_calls_set_default_location(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    src = desktop / "screenshot-q.png"
    src.write_text("content")
    dest = home / "Pictures" / "Screenshots" / "screenshot-q.png"
    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=False)
    corral_screenshots.run(adapter, args)

    assert adapter._set_default_location_calls == []


def test_set_default_location_alone_calls_it_without_moving_files(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    shot = desktop / "screenshot-a.png"
    shot.write_text("a")

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=True)
    result = corral_screenshots.run(adapter, args)

    dest_dir = home / "Pictures" / "Screenshots"
    assert adapter._set_default_location_calls == [dest_dir]
    assert result["set_default_location"] == {"skipped": False, "path": dest_dir}

    # No files moved: this was a dry-run plan (go=False), independent of the
    # location flag.
    assert shot.exists()
    assert not (dest_dir / "screenshot-a.png").exists()
    assert "moved" not in result["plan"][0]


def test_go_and_set_default_location_together_do_both(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    shot = desktop / "screenshot-a.png"
    shot.write_text("a")

    args = SimpleNamespace(dir=[], go=True, from_queue=False, set_default_location=True)
    result = corral_screenshots.run(adapter, args)

    dest_dir = home / "Pictures" / "Screenshots"
    assert result["moved_count"] == 1
    assert not shot.exists()
    assert (dest_dir / "screenshot-a.png").exists()

    assert adapter._set_default_location_calls == [dest_dir]
    assert result["set_default_location"] == {"skipped": False, "path": dest_dir}


def test_from_queue_and_set_default_location_together_do_both(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    src = desktop / "screenshot-q.png"
    src.write_text("content")
    dest = home / "Pictures" / "Screenshots" / "screenshot-q.png"
    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[], go=False, from_queue=True, set_default_location=True)
    result = corral_screenshots.run(adapter, args)

    assert result["from_queue"]["executed"] == [{"id": entry.id, "src": str(src), "dest": str(dest)}]
    assert not src.exists()
    assert dest.exists()
    assert adapter._set_default_location_calls == [dest.parent]
    assert result["set_default_location"] == {"skipped": False, "path": dest.parent}


def test_neither_go_nor_set_default_location_moves_nothing_or_calls_nothing(adapter, home):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    shot = desktop / "screenshot-a.png"
    shot.write_text("a")

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)

    assert shot.exists()
    assert adapter._set_default_location_calls == []
    assert "set_default_location" not in result
    assert "moved_count" not in result


def test_set_default_location_not_implemented_is_reported_not_raised(adapter, home):
    """On Arch (or any adapter that doesn't support it), the caught
    NotImplementedError must degrade to a reported "skipped" outcome, never
    propagate and crash the whole corral-screenshots run.
    """
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)

    def _raise(path):
        raise NotImplementedError("not supported on this OS")

    adapter.set_screenshot_save_location = _raise  # type: ignore[method-assign]

    args = SimpleNamespace(dir=[], go=False, from_queue=False, set_default_location=True)
    result = corral_screenshots.run(adapter, args)  # must not raise

    assert result["set_default_location"] == {
        "skipped": True,
        "reason": "not supported on this OS",
    }


# ---------------------------------------------------------------------------
# 7. --go per-entry failure isolation, matching sort.py's pattern.
# ---------------------------------------------------------------------------


def test_go_continues_after_one_entry_fails_and_records_outcome_per_entry(
    adapter, home, monkeypatch
):
    desktop, downloads, documents = _default_roots(home)
    _mkdirs(desktop, downloads, documents)
    good = desktop / "screenshot-good.png"
    good.write_text("good-bytes")
    bad = desktop / "screenshot-bad.png"
    bad.write_text("bad-bytes")

    real_move = adapter.move

    def flaky_move(src, dst):
        if Path(src).name == "screenshot-bad.png":
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{src}'")
        return real_move(src, dst)

    monkeypatch.setattr(adapter, "move", flaky_move)

    args = SimpleNamespace(dir=[], go=True, from_queue=False, set_default_location=False)
    result = corral_screenshots.run(adapter, args)  # must not raise

    by_name = {e["src"].name: e for e in result["plan"]}
    assert by_name["screenshot-good.png"]["moved"] is True
    assert by_name["screenshot-bad.png"]["moved"] is False
    assert by_name["screenshot-bad.png"]["error"] is not None
    assert result["moved_count"] == 1
    assert bad.exists()  # never actually moved
