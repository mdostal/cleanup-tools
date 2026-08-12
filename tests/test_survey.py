"""Tests for cleanup_tools.commands.survey.run().

Builds a small, fully deterministic fake home directory under tmp_path and
runs survey.run() against a MacOSAdapter subclass whose resolve_home() is
redirected there (mirroring the TmpHomeAdapter pattern in test_config.py).
Because OSAdapter.resolve_standard_dir() derives every standard dir from
resolve_home(), overriding just resolve_home() is enough to keep every
survey section off the real filesystem -- no automated test here ever reads
the real ~/Documents, ~/Downloads, etc.

Fixture layout (sizes chosen so every section's expected totals are easy to
hand-compute):

    home/
      Documents/
        top_file.txt                          50 bytes
        screenshot_doc.png                     25 bytes   (screenshot)
        node_modules/pkg/file.js               80 bytes   (node_modules #1)
        work/
          repo-a/
            file1.txt                         200 bytes
            subdir/file2.txt                  300 bytes   (repo-a = 500)
          repo-b/
            file1.txt                         150 bytes   (repo-b = 150)
      Downloads/
        report.pdf                            300 bytes
        data.csv                              100 bytes
        data2.csv                              50 bytes
        noext_file                             20 bytes   (no extension)
        noext_file2                            30 bytes   (no extension)
        screenshot1.png                        10 bytes   (screenshot)
        screenshot2.png                        15 bytes   (screenshot)
      Desktop/
        note.txt                               40 bytes
        screenshot_desktop.png                  5 bytes   (screenshot)
        node_modules/                                     (node_modules #2)
          pkg2/
            file.js                           120 bytes
            node_modules/nested/file.js         60 bytes  (pruned: nested
                                                            match, must not
                                                            appear as its own
                                                            entry, and must
                                                            not be counted a
                                                            second time)
      Pictures/
        photo.jpg                              60 bytes
        screenshot_pic1.png                     5 bytes   (screenshot)
        nested/screenshot_pic2.png              5 bytes   (screenshot, depth 1)
      Movies/
        movie.mp4                             400 bytes
      Library/
        cache.dat                              90 bytes

Expected *raw content* totals (i.e. the sum of each file's exact byte
count). These are used as a lower bound only: directory sizes are now
computed via ``OSAdapter.dir_size_bytes()``, which shells out to ``du -sk``
and therefore reports real on-disk block usage rather than exact content
bytes. ``du`` rounds each file up to the filesystem's allocation block size
(commonly 4KiB), so for a fixture this small (tens to hundreds of bytes per
file) the reported size is dominated by per-file block overhead, not content
size -- e.g. Downloads (7 tiny files) can legitimately report a larger
``du`` size than Documents (6 tiny files) even though Documents has more
raw content bytes. Tests below assert ``actual >= raw`` (true on every
filesystem, since du never rounds down) and a generous upper bound to catch
gross regressions, rather than exact equality.

    Documents = 50 + 25 + 80 + 500 + 150            = 805   (6 files)
    Downloads = 300 + 100 + 50 + 20 + 30 + 10 + 15   = 525   (7 files)
    Desktop   = 40 + 5 + 120 + 60                    = 225   (4 files)
    Pictures  = 60 + 5 + 5                           =  70   (3 files)
    Movies    = 400                                          (1 file)
    Library   =  90                                          (1 file)

Expected node_modules (raw content, same lower-bound caveat as above):
    dir_count   = 2 (the nested "node_modules" inside Desktop's is pruned)
    total_bytes = 80 (Documents #1) + (120 + 60) (Desktop #2, nested bytes
                  already included in its recursive size) = 260   (3 files)

Expected downloads_by_extension:
    {"pdf": 1, "csv": 2, "": 2, "png": 2}

Expected screenshot_count (desktop + downloads + documents + pictures):
    1 (screenshot_desktop) + 2 (screenshot1/2) + 1 (screenshot_doc)
    + 2 (screenshot_pic1/2, one nested one level deep) = 6
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.commands import survey


def _write(path: Path, size: int) -> None:
    """Create ``path`` (and parents) containing exactly ``size`` bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# du -sk rounds each file's usage up to the filesystem's allocation block
# size (commonly 4KiB, but not guaranteed), so a du-backed directory size is
# always >= the raw content bytes, and typically somewhat more. This margin
# is deliberately generous (well above any real-world block size) so the
# check only catches gross regressions (e.g. a size of 0, or double-counting)
# rather than asserting exact equality with content bytes.
_DU_PER_FILE_MARGIN = 16 * 1024


def _assert_size_close(actual: int, raw_bytes: int, file_count: int) -> None:
    """Assert ``actual`` (a du-backed size) is consistent with ``raw_bytes``
    of real content spread across ``file_count`` files.

    ``du`` never reports less than the actual content size, so ``actual``
    must be at least ``raw_bytes``. It also shouldn't exceed content size by
    more than a generous per-file block-rounding margin.
    """
    assert actual >= raw_bytes, (
        f"expected du-reported size >= raw content bytes ({raw_bytes}), got {actual}"
    )
    upper_bound = raw_bytes + file_count * _DU_PER_FILE_MARGIN
    assert actual < upper_bound, (
        f"du-reported size {actual} unexpectedly far above raw content bytes "
        f"({raw_bytes}) + block-rounding margin (upper bound {upper_bound})"
    )


def _make_fake_adapter(home: Path) -> MacOSAdapter:
    """A MacOSAdapter whose resolve_home() points at ``home`` instead of the
    real user home. resolve_standard_dir() is inherited unchanged from the
    base class, which derives every standard dir from resolve_home() -- so
    redirecting just resolve_home() is sufficient to keep the whole survey
    off the real filesystem.
    """

    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

    return FakeHomeAdapter()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"

    # Documents: top-level file, a screenshot, one node_modules, and work/
    _write(home_dir / "Documents" / "top_file.txt", 50)
    _write(home_dir / "Documents" / "screenshot_doc.png", 25)
    _write(home_dir / "Documents" / "node_modules" / "pkg" / "file.js", 80)
    _write(home_dir / "Documents" / "work" / "repo-a" / "file1.txt", 200)
    _write(home_dir / "Documents" / "work" / "repo-a" / "subdir" / "file2.txt", 300)
    _write(home_dir / "Documents" / "work" / "repo-b" / "file1.txt", 150)

    # Downloads: mixed extensions, two extensionless files, two screenshots
    _write(home_dir / "Downloads" / "report.pdf", 300)
    _write(home_dir / "Downloads" / "data.csv", 100)
    _write(home_dir / "Downloads" / "data2.csv", 50)
    _write(home_dir / "Downloads" / "noext_file", 20)
    _write(home_dir / "Downloads" / "noext_file2", 30)
    _write(home_dir / "Downloads" / "screenshot1.png", 10)
    _write(home_dir / "Downloads" / "screenshot2.png", 15)

    # Desktop: a plain file, a screenshot, and the second node_modules --
    # this one has a nested "node_modules" inside it to exercise pruning.
    _write(home_dir / "Desktop" / "note.txt", 40)
    _write(home_dir / "Desktop" / "screenshot_desktop.png", 5)
    _write(home_dir / "Desktop" / "node_modules" / "pkg2" / "file.js", 120)
    _write(
        home_dir / "Desktop" / "node_modules" / "pkg2" / "node_modules" / "nested" / "file.js",
        60,
    )

    # Pictures: a plain photo and two screenshots (one nested one level deep)
    _write(home_dir / "Pictures" / "photo.jpg", 60)
    _write(home_dir / "Pictures" / "screenshot_pic1.png", 5)
    _write(home_dir / "Pictures" / "nested" / "screenshot_pic2.png", 5)

    # Movies / Library: only used by home_top_level.
    _write(home_dir / "Movies" / "movie.mp4", 400)
    _write(home_dir / "Library" / "cache.dat", 90)

    return home_dir


@pytest.fixture
def adapter(home: Path) -> MacOSAdapter:
    return _make_fake_adapter(home)


@pytest.fixture
def result(adapter: MacOSAdapter) -> dict:
    return survey.run(adapter)


# ---------------------------------------------------------------------------
# 1. All 6 keys are present.
# ---------------------------------------------------------------------------


def test_run_returns_all_six_expected_keys(result):
    assert set(result.keys()) == {
        "disk",
        "home_top_level",
        "documents_work_biggest_repos",
        "node_modules",
        "downloads_by_extension",
        "screenshot_count",
    }


# ---------------------------------------------------------------------------
# 2. home_top_level sums match the fixture tree.
# ---------------------------------------------------------------------------


def test_home_top_level_sums_match_fixture(result):
    sizes = result["home_top_level"]
    assert set(sizes.keys()) == {
        "Documents",
        "Downloads",
        "Desktop",
        "Pictures",
        "Movies",
        "Library",
    }
    _assert_size_close(sizes["Documents"], raw_bytes=805, file_count=6)
    _assert_size_close(sizes["Downloads"], raw_bytes=525, file_count=7)
    _assert_size_close(sizes["Desktop"], raw_bytes=225, file_count=4)
    _assert_size_close(sizes["Pictures"], raw_bytes=70, file_count=3)
    _assert_size_close(sizes["Movies"], raw_bytes=400, file_count=1)
    _assert_size_close(sizes["Library"], raw_bytes=90, file_count=1)


def test_home_top_level_is_sorted_descending_by_size(result):
    sizes = list(result["home_top_level"].values())
    assert sizes == sorted(sizes, reverse=True)


# ---------------------------------------------------------------------------
# 3. documents_work_biggest_repos is {} when Documents/work doesn't exist.
# ---------------------------------------------------------------------------


def test_documents_work_biggest_repos_matches_fixture(result):
    repos = result["documents_work_biggest_repos"]
    assert set(repos.keys()) == {"repo-a", "repo-b"}
    _assert_size_close(repos["repo-a"], raw_bytes=500, file_count=2)
    _assert_size_close(repos["repo-b"], raw_bytes=150, file_count=1)


def test_documents_work_biggest_repos_empty_when_work_dir_missing(tmp_path):
    home_dir = tmp_path / "home"
    # Documents exists, but Documents/work does not.
    _write(home_dir / "Documents" / "top_file.txt", 10)

    adapter = _make_fake_adapter(home_dir)

    result = survey.run(adapter)

    assert result["documents_work_biggest_repos"] == {}


# ---------------------------------------------------------------------------
# 4. node_modules sums 2 separate dirs, without double-counting the one
#    nested inside another (find_dirs prunes).
# ---------------------------------------------------------------------------


def test_node_modules_counts_two_dirs_and_does_not_double_count_nested(result):
    node_modules = result["node_modules"]
    assert node_modules["dir_count"] == 2
    _assert_size_close(node_modules["total_bytes"], raw_bytes=260, file_count=3)


# ---------------------------------------------------------------------------
# 5. downloads_by_extension tallies correctly, including no-extension files.
# ---------------------------------------------------------------------------


def test_downloads_by_extension_tallies_correctly(result):
    assert result["downloads_by_extension"] == {
        "pdf": 1,
        "csv": 2,
        "": 2,
        "png": 2,
    }


# ---------------------------------------------------------------------------
# 6. screenshot_count counts screenshot-pattern files across all 4 dirs.
# ---------------------------------------------------------------------------


def test_screenshot_count_counts_across_all_four_dirs(result):
    assert result["screenshot_count"] == 6


# ---------------------------------------------------------------------------
# 6b. screenshot_count boundary: OSAdapter.list_dir's max_depth convention
#     is one less than `find -maxdepth N` (max_depth=0 means find -maxdepth 1
#     -- see test_downloads_by_extension, which already gets this right).
#     _screenshot_count wants the equivalent of `find -maxdepth 3`, so it
#     must call list_dir with max_depth=2, NOT max_depth=3. Prove the exact
#     boundary: a screenshot 2 levels deep must be INCLUDED, one 3 levels
#     deep must be EXCLUDED.
# ---------------------------------------------------------------------------


def test_screenshot_count_includes_two_levels_deep_excludes_three_levels_deep(tmp_path):
    home_dir = tmp_path / "home"

    # Desktop/a/b/screenshot_2deep.png is 2 directory levels below Desktop
    # -- within `find -maxdepth 3` / the corrected max_depth=2, so it must
    # be counted.
    _write(home_dir / "Desktop" / "a" / "b" / "screenshot_2deep.png", 5)

    # Desktop/a/b/c/screenshot_3deep.png is 3 directory levels below Desktop
    # -- one level past the boundary above, so it must NOT be counted. With
    # the pre-fix max_depth=3 bug, this file would incorrectly be included.
    _write(home_dir / "Desktop" / "a" / "b" / "c" / "screenshot_3deep.png", 5)

    adapter = _make_fake_adapter(home_dir)
    result = survey.run(adapter)

    assert result["screenshot_count"] == 1
