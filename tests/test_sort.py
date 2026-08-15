"""Tests for cleanup_tools.commands.sort.run().

Mirrors the fake-home pattern in test_survey.py/test_config.py: a MacOSAdapter
subclass whose resolve_home() is redirected under tmp_path, so
config_module.load_config()'s default path (~/.config/cleanup-tools/config.yaml)
never resolves to -- let alone reads or writes -- the real user home. The
directory being *sorted* is always a separate tmp_path subdirectory passed
explicitly via args.dir, so no test here ever touches a real filesystem
location outside of tmp_path.

sort.run(adapter, args) reads args.dir/args.go via getattr(), so a plain
types.SimpleNamespace(dir=..., go=...) stands in for the argparse Namespace
main() would normally pass. args.dir is a list (cli.py's "dir" argument is
nargs="*", matching reclaim/corral-screenshots) -- single-root tests below
pass a one-element list, e.g. dir=[str(target)].
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.commands import sort
from cleanup_tools.config import (
    DEFAULT_BUCKET_RULES,
    BucketRule,
    Config,
    save_config,
)
from cleanup_tools import queue as queue_module
from cleanup_tools.queue import QueueEntry, build_plan_snapshot


def _make_fake_adapter(home: Path) -> MacOSAdapter:
    """A MacOSAdapter whose resolve_home() points at ``home``.

    config_module.load_config()'s default path is derived from
    adapter.resolve_home(), so redirecting just resolve_home() is enough to
    keep every config lookup sort.run() makes off the real filesystem.
    """

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
def adapter(home: Path) -> MacOSAdapter:
    return _make_fake_adapter(home)


# ---------------------------------------------------------------------------
# 1. Dry-run (go=False): filesystem unchanged, plan describes what WOULD move.
# ---------------------------------------------------------------------------


def test_dry_run_leaves_files_unchanged_but_plan_describes_moves(adapter, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    photo = target / "photo.jpg"
    doc = target / "notes.txt"
    photo.write_text("photo-bytes")
    doc.write_text("doc-bytes")

    args = SimpleNamespace(dir=[str(target)], go=False)
    result = sort.run(adapter, args)

    assert result["go"] is False
    assert result["roots"] == [target]

    # Nothing on disk moved: originals still present, no _sorted/ created.
    assert photo.exists() and photo.read_text() == "photo-bytes"
    assert doc.exists() and doc.read_text() == "doc-bytes"
    assert not (target / "_sorted").exists()

    # But the plan describes exactly what --go would do.
    plan_by_name = {entry["src"].name: entry for entry in result["plan"]}
    assert set(plan_by_name) == {"photo.jpg", "notes.txt"}
    assert plan_by_name["photo.jpg"]["bucket"] == "photos"
    assert plan_by_name["photo.jpg"]["dest"] == target / "_sorted" / "photos" / "photo.jpg"
    assert plan_by_name["notes.txt"]["bucket"] == "docs"
    assert plan_by_name["notes.txt"]["dest"] == target / "_sorted" / "docs" / "notes.txt"


# ---------------------------------------------------------------------------
# 2. --go (go=True): every default-rule category actually moves into
#    _sorted/<bucket>/. The screenshot case proves the filename-pattern
#    override still wins over the plain photos rule for the same extension.
# ---------------------------------------------------------------------------

BUCKET_CASES = [
    ("photo.jpg", "photos"),
    # Same extension as the photos rule, but the "screenshot*" filename
    # pattern rule precedes it and must win.
    ("screenshot.png", "screenshots"),
    ("app.dmg", "installers"),
    ("clip.mp4", "videos"),
    ("doc.pdf", "pdfs"),
    ("bundle.zip", "archives"),
    ("sheet.csv", "data"),
    ("file.txt", "docs"),
    ("mystery.xyz", "other"),
]


@pytest.mark.parametrize("filename,expected_bucket", BUCKET_CASES)
def test_go_moves_file_into_correct_bucket(adapter, tmp_path, filename, expected_bucket):
    target = tmp_path / "target"
    target.mkdir()
    src = target / filename
    src.write_text("content")

    args = SimpleNamespace(dir=[str(target)], go=True)
    result = sort.run(adapter, args)

    dest = target / "_sorted" / expected_bucket / filename

    assert result["go"] is True
    assert result["plan"] == [
        {
            "src": src,
            "dest": dest,
            "bucket": expected_bucket,
            "dest_exists": False,
            "moved": True,
            "error": None,
        }
    ]
    assert not src.exists()
    assert dest.exists()
    assert dest.read_text() == "content"


# ---------------------------------------------------------------------------
# 3. Dotfiles are excluded from the plan and never moved, dry-run or --go.
# ---------------------------------------------------------------------------


def test_dotfiles_excluded_from_plan_and_never_moved(adapter, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    dotfile = target / ".DS_Store"
    dotfile.write_text("ignore-me")
    normal = target / "photo.jpg"
    normal.write_text("photo-bytes")

    # Dry-run: dotfile absent from the plan, filesystem untouched.
    dry_result = sort.run(adapter, SimpleNamespace(dir=[str(target)], go=False))
    assert {entry["src"].name for entry in dry_result["plan"]} == {"photo.jpg"}
    assert dotfile.exists()
    assert dotfile.read_text() == "ignore-me"

    # --go: dotfile still absent from the plan and still never moved; only
    # the normal file actually moves.
    go_result = sort.run(adapter, SimpleNamespace(dir=[str(target)], go=True))
    assert {entry["src"].name for entry in go_result["plan"]} == {"photo.jpg"}
    assert dotfile.exists()
    assert dotfile.read_text() == "ignore-me"
    assert not normal.exists()
    assert (target / "_sorted" / "photos" / "photo.jpg").exists()


# ---------------------------------------------------------------------------
# 4. Empty directory: empty plan, no error, in either mode.
# ---------------------------------------------------------------------------


def test_empty_directory_returns_empty_plan_without_error(adapter, tmp_path):
    target = tmp_path / "empty"
    target.mkdir()

    dry_result = sort.run(adapter, SimpleNamespace(dir=[str(target)], go=False))
    assert dry_result["plan"] == []

    go_result = sort.run(adapter, SimpleNamespace(dir=[str(target)], go=True))
    assert go_result["plan"] == []


# ---------------------------------------------------------------------------
# 4b. Multi-root scanning via configured search_roots (mirrors reclaim.py/
#     corral_screenshots.py's existing precedence: CLI dirs win, else
#     search_roots, else the single default).
# ---------------------------------------------------------------------------


def test_multi_root_scan_covers_every_configured_search_root(adapter, tmp_path):
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    root_b = tmp_path / "root_b"
    root_b.mkdir()
    (root_a / "photo.jpg").write_text("a-bytes")
    (root_b / "doc.pdf").write_text("b-bytes")

    save_config(adapter, Config(bucket_rules=DEFAULT_BUCKET_RULES, search_roots=[str(root_a), str(root_b)]))

    result = sort.run(adapter, SimpleNamespace(dir=None, go=False))

    assert result["roots"] == [root_a, root_b]
    plan_by_name = {entry["src"].name: entry for entry in result["plan"]}
    assert set(plan_by_name) == {"photo.jpg", "doc.pdf"}
    assert plan_by_name["photo.jpg"]["dest"] == root_a / "_sorted" / "photos" / "photo.jpg"
    assert plan_by_name["doc.pdf"]["dest"] == root_b / "_sorted" / "pdfs" / "doc.pdf"


def test_configured_root_missing_is_silently_skipped_others_still_scanned(adapter, tmp_path):
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    missing_root = tmp_path / "does-not-exist"
    (root_a / "photo.jpg").write_text("a-bytes")

    save_config(adapter, Config(bucket_rules=DEFAULT_BUCKET_RULES, search_roots=[str(root_a), str(missing_root)]))

    # Must not raise: an implicit, configured root that doesn't exist yet
    # (e.g. no Documents folder on a fresh machine) is best-effort, unlike
    # an explicitly CLI-supplied missing dir (see the FileNotFoundError test
    # in section 8 below).
    result = sort.run(adapter, SimpleNamespace(dir=None, go=False))

    assert result["roots"] == [root_a, missing_root]
    assert {entry["src"].name for entry in result["plan"]} == {"photo.jpg"}


def test_explicit_cli_dir_overrides_configured_search_roots(adapter, tmp_path):
    configured_root = tmp_path / "configured"
    configured_root.mkdir()
    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    (configured_root / "photo.jpg").write_text("a-bytes")
    (explicit_dir / "doc.pdf").write_text("b-bytes")

    save_config(adapter, Config(bucket_rules=DEFAULT_BUCKET_RULES, search_roots=[str(configured_root)]))

    result = sort.run(adapter, SimpleNamespace(dir=[str(explicit_dir)], go=False))

    assert result["roots"] == [explicit_dir]
    assert {entry["src"].name for entry in result["plan"]} == {"doc.pdf"}


# ---------------------------------------------------------------------------
# 5. Custom bucket rules from a loaded config are respected, alongside (not
#    instead of) the defaults.
# ---------------------------------------------------------------------------


def test_custom_bucket_rule_from_config_is_respected(adapter, tmp_path):
    custom_rule = BucketRule(extensions=frozenset({"log"}), bucket="logs")
    config = Config(bucket_rules=[custom_rule] + DEFAULT_BUCKET_RULES)
    # No explicit path: writes to adapter.resolve_home()/.config/cleanup-tools/
    # config.yaml, the same default location sort.run()'s load_config() call
    # will read from -- both derived from the fake, tmp_path-scoped home.
    save_config(adapter, config)

    target = tmp_path / "target"
    target.mkdir()
    log_file = target / "server.log"
    log_file.write_text("log-bytes")
    pdf_file = target / "report.pdf"
    pdf_file.write_text("pdf-bytes")

    args = SimpleNamespace(dir=[str(target)], go=True)
    result = sort.run(adapter, args)

    plan_by_name = {entry["src"].name: entry["bucket"] for entry in result["plan"]}
    assert plan_by_name["server.log"] == "logs"
    # Defaults still apply for files the custom rule doesn't cover.
    assert plan_by_name["report.pdf"] == "pdfs"

    assert (target / "_sorted" / "logs" / "server.log").exists()
    assert (target / "_sorted" / "pdfs" / "report.pdf").exists()


# ---------------------------------------------------------------------------
# 6. --go continues past a per-entry move failure (e.g. a file that vanished
#    mid-run) instead of crashing the whole batch, and records what
#    happened to every entry -- both the failure and the survivors.
# ---------------------------------------------------------------------------


def test_go_continues_after_one_entry_fails_and_records_outcome_per_entry(
    adapter, tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    good = target / "good.txt"
    good.write_text("good-bytes")
    bad = target / "bad.txt"
    bad.write_text("bad-bytes")

    real_move = adapter.move

    def flaky_move(src, dst):
        # Simulate "bad.txt" having been deleted out from under us between
        # planning and execution (e.g. by another process) -- a real
        # shutil.move on a since-vanished src raises FileNotFoundError.
        if Path(src).name == "bad.txt":
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{src}'")
        return real_move(src, dst)

    monkeypatch.setattr(adapter, "move", flaky_move)

    args = SimpleNamespace(dir=[str(target)], go=True)
    result = sort.run(adapter, args)  # must not raise

    plan_by_name = {entry["src"].name: entry for entry in result["plan"]}

    good_entry = plan_by_name["good.txt"]
    assert good_entry["moved"] is True
    assert good_entry["error"] is None
    assert not good.exists()
    assert (target / "_sorted" / "docs" / "good.txt").exists()

    bad_entry = plan_by_name["bad.txt"]
    assert bad_entry["moved"] is False
    assert bad_entry["error"] is not None
    assert "bad.txt" in bad_entry["error"]
    # The failed move was never actually attempted on a real file, so
    # bad.txt is untouched -- no data loss, no crash, no silent skip.
    assert bad.exists()
    assert bad.read_text() == "bad-bytes"


# ---------------------------------------------------------------------------
# 7. --go never overwrites a pre-existing file already sitting at the
#    destination path; it skips that entry and says why instead.
# ---------------------------------------------------------------------------


def test_go_skips_entry_whose_destination_already_exists_without_overwriting(
    adapter, tmp_path
):
    target = tmp_path / "target"
    target.mkdir()
    src = target / "notes.txt"
    src.write_text("new-content")

    dest = target / "_sorted" / "docs" / "notes.txt"
    dest.parent.mkdir(parents=True)
    dest.write_text("precious-old-content")

    args = SimpleNamespace(dir=[str(target)], go=True)
    result = sort.run(adapter, args)

    entry = result["plan"][0]
    assert entry["dest_exists"] is True
    assert entry["moved"] is False
    assert entry["error"] == "destination already exists, skipped to avoid overwrite"

    # No overwrite happened: the pre-seeded destination content survives
    # untouched, and the source was never moved (or deleted) either.
    assert dest.read_text() == "precious-old-content"
    assert src.exists()
    assert src.read_text() == "new-content"


def test_dry_run_flags_dest_exists_without_touching_filesystem(adapter, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    src = target / "notes.txt"
    src.write_text("new-content")

    dest = target / "_sorted" / "docs" / "notes.txt"
    dest.parent.mkdir(parents=True)
    dest.write_text("precious-old-content")

    args = SimpleNamespace(dir=[str(target)], go=False)
    result = sort.run(adapter, args)

    entry = result["plan"][0]
    assert entry["dest"] == dest
    assert entry["dest_exists"] is True
    # Nothing executed in dry-run mode: no per-entry outcome fields yet.
    assert "moved" not in entry
    assert "error" not in entry

    assert dest.read_text() == "precious-old-content"
    assert src.read_text() == "new-content"


# ---------------------------------------------------------------------------
# 8. A nonexistent target directory is a loud error, not an empty plan.
# ---------------------------------------------------------------------------


def test_nonexistent_target_dir_raises_file_not_found_error(adapter, tmp_path):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(FileNotFoundError, match=str(missing)):
        sort.run(adapter, SimpleNamespace(dir=[str(missing)], go=False))

    with pytest.raises(FileNotFoundError, match=str(missing)):
        sort.run(adapter, SimpleNamespace(dir=[str(missing)], go=True))


# ---------------------------------------------------------------------------
# 9. --from-queue: executes only approved, queue-sourced ``move`` entries
#    under the target dir. See sort._run_from_queue's docstring for the
#    staleness/isolation/single-lock-cycle contract these tests pin down.
# ---------------------------------------------------------------------------


def _seed_queue(adapter, entries: list[QueueEntry]) -> Path:
    """Write ``entries`` straight to the (fake-home) default queue path."""
    path = queue_module.default_queue_path(adapter)
    queue_module.save_queue(adapter, entries, path)
    return path


def _reload_queue(adapter) -> list[QueueEntry]:
    return queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_from_queue_executes_only_approved_move_entries(adapter, tmp_path, status):
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("content")
    dest = target / "_sorted" / "docs" / "file.txt"

    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status=status,
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)
    report = result["from_queue"]

    if status == "approved":
        assert report["qualifying_count"] == 1
        assert report["executed"] == [{"id": entry.id, "src": str(src), "dest": str(dest)}]
        assert not src.exists()
        assert dest.exists()
        assert dest.read_text() == "content"
    else:
        assert report["qualifying_count"] == 0
        assert report["executed"] == []
        assert src.exists()
        assert not dest.exists()

    # The persisted status is never mutated by --from-queue itself -- only
    # execution outcome fields are.
    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == status


def test_from_queue_ignores_approved_non_move_action(adapter, tmp_path):
    """An approved queue entry whose action isn't "move" must never execute
    through sort's --from-queue, even though it's approved and under the
    target dir -- confirming the "right action type" half of the filter.
    """
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("content")

    entry = QueueEntry(
        action="delete",
        src=str(src),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)
    report = result["from_queue"]

    assert report["qualifying_count"] == 0
    assert report["executed"] == []
    assert src.exists()
    assert src.read_text() == "content"


def test_from_queue_stale_entry_is_skipped_status_and_execution_fields_untouched(
    adapter, tmp_path
):
    """A stale approved entry (content changed since plan_snapshot) is left
    alone entirely: never moved, status stays "approved", executed_at/
    execution_error stay None, and it's reported in the "stale" list.
    """
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("original-content")
    dest = target / "_sorted" / "docs" / "file.txt"

    snapshot = build_plan_snapshot(src)
    # Content changes after the snapshot was taken -- same file, same name,
    # but the recorded content_hash (and size) no longer match.
    src.write_text("a completely different, longer payload now")

    entry = QueueEntry(
        action="move", src=str(src), dest=str(dest), status="approved", plan_snapshot=snapshot
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)
    report = result["from_queue"]

    assert report["qualifying_count"] == 1
    assert report["executed"] == []
    assert report["failed"] == []
    assert len(report["stale"]) == 1
    assert report["stale"][0]["id"] == entry.id
    assert report["stale"][0]["src"] == str(src)

    # Never touched on disk.
    assert src.exists()
    assert src.read_text() == "a completely different, longer payload now"
    assert not dest.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None
    assert reloaded.execution_error is None


def test_from_queue_stale_entry_deleted_src_is_skipped_and_reported(adapter, tmp_path):
    """The other half of "changed/deleted since plan_snapshot": a src that no
    longer exists at all is also stale, not a hard failure.
    """
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("content")
    dest = target / "_sorted" / "docs" / "file.txt"
    snapshot = build_plan_snapshot(src)
    src.unlink()

    entry = QueueEntry(
        action="move", src=str(src), dest=str(dest), status="approved", plan_snapshot=snapshot
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)
    report = result["from_queue"]

    assert report["executed"] == []
    assert len(report["stale"]) == 1
    assert report["stale"][0]["id"] == entry.id
    assert not dest.exists()

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None
    assert reloaded.execution_error is None


def test_from_queue_successful_execution_sets_executed_at_and_clears_error(adapter, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("content")
    dest = target / "_sorted" / "docs" / "file.txt"

    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)

    assert result["from_queue"]["executed"] == [
        {"id": entry.id, "src": str(src), "dest": str(dest)}
    ]

    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"  # execution never touches status
    assert reloaded.executed_at is not None
    assert reloaded.execution_error is None
    # Sanity: it's a real, parseable timestamp, not a placeholder string.
    datetime.fromisoformat(reloaded.executed_at)


def test_from_queue_dest_already_exists_is_skipped_not_overwritten(adapter, tmp_path):
    """CRITICAL regression: --from-queue must never clobber a pre-existing
    destination file, mirroring the --go path's dest_exists guard. Before
    the fix, _run_from_queue called adapter.move() unconditionally and
    silently overwrote whatever was already sitting at entry.dest.
    """
    target = tmp_path / "target"
    target.mkdir()
    src = target / "file.txt"
    src.write_text("new-content")
    dest = target / "_sorted" / "docs" / "file.txt"
    dest.parent.mkdir(parents=True)
    dest.write_text("pre-existing-content-must-survive")

    entry = QueueEntry(
        action="move",
        src=str(src),
        dest=str(dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(src),
    )
    _seed_queue(adapter, [entry])

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)
    report = result["from_queue"]

    # Neither side of the move is executed nor reported as executed.
    assert report["executed"] == []
    assert report["failed"] == []
    assert len(report["skipped"]) == 1
    assert report["skipped"][0]["id"] == entry.id
    assert report["skipped"][0]["src"] == str(src)
    assert report["skipped"][0]["dest"] == str(dest)
    assert (
        report["skipped"][0]["reason"]
        == "destination already exists, skipped to avoid overwrite"
    )

    # The pre-existing destination file survives untouched, and the source
    # was never moved (and thus still exists with its own original content).
    assert dest.read_text() == "pre-existing-content-must-survive"
    assert src.exists()
    assert src.read_text() == "new-content"

    # Never recorded as an execution attempt: executed_at/execution_error
    # stay untouched (this is a skip, not a raised-exception failure), and
    # status is left alone too.
    reloaded = _reload_queue(adapter)[0]
    assert reloaded.status == "approved"
    assert reloaded.executed_at is None
    assert reloaded.execution_error is None


def test_from_queue_failed_execution_isolated_per_entry(adapter, tmp_path, monkeypatch):
    """One entry's adapter.move() raising OSError sets execution_error to
    the error string (executed_at still gets set), and the batch continues
    on to the next entry rather than aborting -- per-entry isolation,
    mirroring --go's existing per-entry try/except.
    """
    target = tmp_path / "target"
    target.mkdir()
    good_src = target / "good.txt"
    good_src.write_text("good-bytes")
    bad_src = target / "bad.txt"
    bad_src.write_text("bad-bytes")
    good_dest = target / "_sorted" / "docs" / "good.txt"
    bad_dest = target / "_sorted" / "docs" / "bad.txt"

    good_entry = QueueEntry(
        action="move",
        src=str(good_src),
        dest=str(good_dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(good_src),
    )
    bad_entry = QueueEntry(
        action="move",
        src=str(bad_src),
        dest=str(bad_dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(bad_src),
    )
    _seed_queue(adapter, [bad_entry, good_entry])

    real_move = adapter.move

    def flaky_move(src, dst):
        if Path(src).name == "bad.txt":
            raise OSError("permission denied (simulated)")
        return real_move(src, dst)

    monkeypatch.setattr(adapter, "move", flaky_move)

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)  # must not raise
    report = result["from_queue"]

    assert len(report["failed"]) == 1
    assert report["failed"][0]["id"] == bad_entry.id
    assert report["failed"][0]["error"] == "permission denied (simulated)"

    # The batch continued: the good entry after the bad one in queue order
    # still executed.
    assert len(report["executed"]) == 1
    assert report["executed"][0]["id"] == good_entry.id

    by_id = {e.id: e for e in _reload_queue(adapter)}

    bad_reloaded = by_id[bad_entry.id]
    assert bad_reloaded.status == "approved"
    assert bad_reloaded.executed_at is not None  # set on failure too
    assert bad_reloaded.execution_error == "permission denied (simulated)"
    assert bad_src.exists()  # never actually moved
    assert bad_src.read_text() == "bad-bytes"

    good_reloaded = by_id[good_entry.id]
    assert good_reloaded.status == "approved"
    assert good_reloaded.executed_at is not None
    assert good_reloaded.execution_error is None
    assert not good_src.exists()
    assert good_dest.exists()


def test_from_queue_non_oserror_mid_batch_does_not_lose_prior_entrys_result(
    adapter, tmp_path, monkeypatch
):
    """CRITICAL regression: a non-OSError exception raised while executing
    one entry must not prevent save_queue from persisting the outcome
    already recorded on an *earlier* entry in the same batch.

    Before the fix, _run_from_queue only caught ``except OSError``, so a
    ValueError (or any other non-OSError) raised mid-batch would propagate
    out of the whole function -- past save_queue -- losing the first
    entry's already-set executed_at/execution_error entirely.
    """
    target = tmp_path / "target"
    target.mkdir()
    first_src = target / "first.txt"
    first_src.write_text("first-bytes")
    second_src = target / "second.txt"
    second_src.write_text("second-bytes")
    first_dest = target / "_sorted" / "docs" / "first.txt"
    second_dest = target / "_sorted" / "docs" / "second.txt"

    first_entry = QueueEntry(
        action="move",
        src=str(first_src),
        dest=str(first_dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(first_src),
    )
    second_entry = QueueEntry(
        action="move",
        src=str(second_src),
        dest=str(second_dest),
        status="approved",
        plan_snapshot=build_plan_snapshot(second_src),
    )
    _seed_queue(adapter, [first_entry, second_entry])

    real_move = adapter.move

    def flaky_move(src, dst):
        if Path(src).name == "second.txt":
            raise ValueError("unexpected bug (simulated), not an OSError")
        return real_move(src, dst)

    monkeypatch.setattr(adapter, "move", flaky_move)

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)  # must not raise
    report = result["from_queue"]

    assert len(report["executed"]) == 1
    assert report["executed"][0]["id"] == first_entry.id

    assert len(report["failed"]) == 1
    assert report["failed"][0]["id"] == second_entry.id
    assert report["failed"][0]["error"] == "unexpected bug (simulated), not an OSError"

    # The queue file was still saved -- the first entry's success is not
    # lost just because the second entry blew up with something other
    # than OSError.
    by_id = {e.id: e for e in _reload_queue(adapter)}

    first_reloaded = by_id[first_entry.id]
    assert first_reloaded.status == "approved"
    assert first_reloaded.executed_at is not None
    assert first_reloaded.execution_error is None
    assert not first_src.exists()
    assert first_dest.exists()

    second_reloaded = by_id[second_entry.id]
    assert second_reloaded.status == "approved"
    assert second_reloaded.executed_at is not None
    assert second_reloaded.execution_error == "unexpected bug (simulated), not an OSError"
    assert second_src.exists()  # never actually moved


def test_from_queue_saves_queue_exactly_once_per_invocation(adapter, tmp_path, monkeypatch):
    """The whole load -> mutate -> save cycle runs inside a single
    with_queue_lock block: N qualifying entries must still only produce one
    lock acquisition and one save_queue call, not N of each.
    """
    target = tmp_path / "target"
    target.mkdir()

    entries = []
    for i in range(4):
        src = target / f"file{i}.txt"
        src.write_text(f"content-{i}")
        dest = target / "_sorted" / "docs" / f"file{i}.txt"
        entries.append(
            QueueEntry(
                action="move",
                src=str(src),
                dest=str(dest),
                status="approved",
                plan_snapshot=build_plan_snapshot(src),
            )
        )
    _seed_queue(adapter, entries)

    save_calls: list[list[QueueEntry]] = []
    real_save = queue_module.save_queue

    def spy_save(adapter_, entries_, path=None):
        save_calls.append(list(entries_))
        return real_save(adapter_, entries_, path)

    monkeypatch.setattr(sort.queue_module, "save_queue", spy_save)

    lock_calls: list[Path] = []
    real_lock = queue_module.with_queue_lock

    @contextlib.contextmanager
    def spy_lock(adapter_, path):
        lock_calls.append(path)
        with real_lock(adapter_, path):
            yield

    monkeypatch.setattr(sort.queue_module, "with_queue_lock", spy_lock)

    args = SimpleNamespace(dir=[str(target)], go=False, from_queue=True)
    result = sort.run(adapter, args)

    assert result["from_queue"]["qualifying_count"] == 4
    assert len(result["from_queue"]["executed"]) == 4
    assert len(save_calls) == 1
    assert len(lock_calls) == 1
