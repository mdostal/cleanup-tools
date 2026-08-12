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
main() would normally pass.
"""

from __future__ import annotations

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

    args = SimpleNamespace(dir=str(target), go=False)
    result = sort.run(adapter, args)

    assert result["go"] is False
    assert result["dir"] == target

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

    args = SimpleNamespace(dir=str(target), go=True)
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
    dry_result = sort.run(adapter, SimpleNamespace(dir=str(target), go=False))
    assert {entry["src"].name for entry in dry_result["plan"]} == {"photo.jpg"}
    assert dotfile.exists()
    assert dotfile.read_text() == "ignore-me"

    # --go: dotfile still absent from the plan and still never moved; only
    # the normal file actually moves.
    go_result = sort.run(adapter, SimpleNamespace(dir=str(target), go=True))
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

    dry_result = sort.run(adapter, SimpleNamespace(dir=str(target), go=False))
    assert dry_result["plan"] == []

    go_result = sort.run(adapter, SimpleNamespace(dir=str(target), go=True))
    assert go_result["plan"] == []


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

    args = SimpleNamespace(dir=str(target), go=True)
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

    args = SimpleNamespace(dir=str(target), go=True)
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

    args = SimpleNamespace(dir=str(target), go=True)
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

    args = SimpleNamespace(dir=str(target), go=False)
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
        sort.run(adapter, SimpleNamespace(dir=str(missing), go=False))

    with pytest.raises(FileNotFoundError, match=str(missing)):
        sort.run(adapter, SimpleNamespace(dir=str(missing), go=True))
