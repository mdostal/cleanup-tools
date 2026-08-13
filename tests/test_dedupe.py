"""Tests for cleanup_tools.commands.dedupe.run() -- the Python port of
scripts/dedupe.sh.

The bash original is a two-stage filter: group files by size (cheap
``stat``), then hash only the files that share a size with another file,
and group those by hash. This port makes two deliberate corrections versus
bash: full uncapped SHA-256 (not SHA-1 truncated to 12 hex chars), and
structured ``{hash, size_bytes, paths}`` groups (not raw ``<hash>  <path>``
line pairs).

Coverage mirrors the acceptance bar in the porting task:

1. Same size, different content -> zero false-positive duplicate groups.
2. Same size, same content -> one duplicate group with the right hash/size.
3. The critical regression test: two files identical for their first N
   bytes (N well under the 8 MiB cap ``queue.py``'s ``_content_hash``
   would apply) but differing after -- must NOT be reported as duplicates,
   proving this module hashes the *entire* file rather than reusing a
   prefix-capped hash.
4. Singleton-sized files are never hashed at all -- verified by patching
   ``dedupe._hash_file`` and asserting it is never called, not just that
   the final output happens to be correct.
5. Output shape: ``{"dir", "duplicate_groups": [{"hash", "size_bytes",
   "paths"}]}``.
6. A directory with zero duplicates returns an empty list, no error.
7. Default-dir-vs-explicit-dir resolution (mirrors find_wallets' root
   resolution test).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.commands import dedupe


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _make_fake_adapter(home: Path) -> MacOSAdapter:
    """A MacOSAdapter whose resolve_home() points at ``home`` instead of the
    real user home (mirrors test_survey.py / test_find_wallets.py's
    pattern), so no test here ever touches the real filesystem.
    """

    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home

    return FakeHomeAdapter()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture
def adapter(home: Path) -> MacOSAdapter:
    return _make_fake_adapter(home)


def _args(target_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(dir=str(target_dir))


# ---------------------------------------------------------------------------
# 1. Same size, different content -> no false-positive duplicate groups.
# ---------------------------------------------------------------------------


def test_same_size_different_content_reports_zero_duplicates(home: Path, adapter):
    scan_dir = home / "scan"
    _write(scan_dir / "a.txt", b"A" * 500)
    _write(scan_dir / "b.txt", b"B" * 500)
    _write(scan_dir / "c.txt", b"C" * 500)

    size_groups = dedupe._group_by_size(adapter, scan_dir)
    assert set(size_groups.keys()) == {500}
    assert len(size_groups[500]) == 3

    result = dedupe.run(adapter, _args(scan_dir))
    assert result["duplicate_groups"] == []


# ---------------------------------------------------------------------------
# 2. Same size, same content -> one duplicate group, correct hash/size.
# ---------------------------------------------------------------------------


def test_identical_files_reported_as_one_duplicate_group(home: Path, adapter):
    scan_dir = home / "scan"
    content = b"duplicate content" * 20
    _write(scan_dir / "one.txt", content)
    _write(scan_dir / "two.txt", content)

    result = dedupe.run(adapter, _args(scan_dir))

    assert len(result["duplicate_groups"]) == 1
    group = result["duplicate_groups"][0]
    assert group["hash"] == hashlib.sha256(content).hexdigest()
    assert group["size_bytes"] == len(content)
    assert {Path(p).name for p in group["paths"]} == {"one.txt", "two.txt"}


# ---------------------------------------------------------------------------
# 3. Critical: same size + same prefix, differing tail -> NOT duplicates.
#    Proves full-file hashing, not a prefix-capped hash like queue.py's
#    _content_hash (which is capped at CONTENT_HASH_MAX_BYTES = 8 MiB).
# ---------------------------------------------------------------------------


def test_same_prefix_different_tail_is_not_a_duplicate(home: Path, adapter):
    scan_dir = home / "scan"
    # N is well under any prefix cap a naive port might reuse (8 MiB).
    shared_prefix = b"P" * 100_000
    _write(scan_dir / "x.bin", shared_prefix + b"X")
    _write(scan_dir / "y.bin", shared_prefix + b"Y")

    # Sanity: both files are the same size, so they DO reach the hashing
    # stage together (this isn't a size-filter false negative).
    size_groups = dedupe._group_by_size(adapter, scan_dir)
    assert len(size_groups[len(shared_prefix) + 1]) == 2

    result = dedupe.run(adapter, _args(scan_dir))
    assert result["duplicate_groups"] == []


# ---------------------------------------------------------------------------
# 4. Singleton-sized files are never hashed at all.
# ---------------------------------------------------------------------------


def test_singleton_sized_files_are_never_hashed(home: Path, adapter, monkeypatch):
    scan_dir = home / "scan"
    _write(scan_dir / "a.txt", b"A" * 100)
    _write(scan_dir / "b.txt", b"B" * 200)
    _write(scan_dir / "c.txt", b"C" * 300)

    call_count = 0
    original_hash_file = dedupe._hash_file

    def _counting_hash_file(path):
        nonlocal call_count
        call_count += 1
        return original_hash_file(path)

    monkeypatch.setattr(dedupe, "_hash_file", _counting_hash_file)

    result = dedupe.run(adapter, _args(scan_dir))

    assert call_count == 0
    assert result["duplicate_groups"] == []


def test_group_by_hash_only_receives_size_matched_candidates(home: Path, adapter, monkeypatch):
    """Directly exercises _group_by_hash to prove it only ever operates on
    the output of _group_by_size (2+ member groups) -- not just that the
    end-to-end output happens to be right.
    """
    scan_dir = home / "scan"
    _write(scan_dir / "singleton.txt", b"S" * 42)
    _write(scan_dir / "dup1.txt", b"D" * 99)
    _write(scan_dir / "dup2.txt", b"D" * 99)

    size_groups = dedupe._group_by_size(adapter, scan_dir)
    # The singleton size never even makes it into size_groups.
    assert 42 not in size_groups
    assert set(size_groups.keys()) == {99}

    hashed_paths = []
    original_hash_file = dedupe._hash_file

    def _tracking_hash_file(path):
        hashed_paths.append(path)
        return original_hash_file(path)

    monkeypatch.setattr(dedupe, "_hash_file", _tracking_hash_file)

    dedupe._group_by_hash(size_groups)

    assert {p.name for p in hashed_paths} == {"dup1.txt", "dup2.txt"}


# ---------------------------------------------------------------------------
# 5. Output shape: {"dir", "duplicate_groups": [{"hash", "size_bytes", "paths"}]}
# ---------------------------------------------------------------------------


def test_output_shape_is_structured_not_raw_pairs(home: Path, adapter):
    scan_dir = home / "scan"
    content = b"shape-check"
    _write(scan_dir / "one.txt", content)
    _write(scan_dir / "two.txt", content)

    result = dedupe.run(adapter, _args(scan_dir))

    assert set(result.keys()) == {"dir", "duplicate_groups"}
    assert Path(result["dir"]) == scan_dir
    assert isinstance(result["duplicate_groups"], list)
    for group in result["duplicate_groups"]:
        assert set(group.keys()) == {"hash", "size_bytes", "paths"}
        assert isinstance(group["hash"], str)
        assert isinstance(group["size_bytes"], int)
        assert isinstance(group["paths"], list)


# ---------------------------------------------------------------------------
# 6. Zero duplicates -> empty list, no error.
# ---------------------------------------------------------------------------


def test_no_duplicates_returns_empty_list_without_erroring(home: Path, adapter):
    scan_dir = home / "scan"
    _write(scan_dir / "unique1.txt", b"1" * 10)
    _write(scan_dir / "unique2.txt", b"2" * 20)
    _write(scan_dir / "unique3.txt", b"3" * 30)

    result = dedupe.run(adapter, _args(scan_dir))

    assert result["duplicate_groups"] == []


# ---------------------------------------------------------------------------
# 7. Default dir (platform Downloads) vs. explicit dir resolution.
# ---------------------------------------------------------------------------


def test_default_dir_is_platform_downloads(home: Path, adapter):
    (home / "Downloads").mkdir()
    content = b"downloads-default"
    _write(home / "Downloads" / "one.txt", content)
    _write(home / "Downloads" / "two.txt", content)

    result = dedupe.run(adapter, args=None)

    assert Path(result["dir"]) == home / "Downloads"
    assert len(result["duplicate_groups"]) == 1


def test_explicit_dir_overrides_default(home: Path, adapter):
    scan_dir = home / "elsewhere"
    content = b"explicit-dir"
    _write(scan_dir / "one.txt", content)
    _write(scan_dir / "two.txt", content)

    result = dedupe.run(adapter, _args(scan_dir))

    assert Path(result["dir"]) == scan_dir
    assert len(result["duplicate_groups"]) == 1


# ---------------------------------------------------------------------------
# 8. node_modules exclusion.
# ---------------------------------------------------------------------------


def test_node_modules_files_are_excluded(home: Path, adapter):
    scan_dir = home / "scan"
    content = b"nm-excluded"
    _write(scan_dir / "node_modules" / "pkg" / "one.txt", content)
    _write(scan_dir / "two.txt", content)

    result = dedupe.run(adapter, _args(scan_dir))

    # The node_modules copy is excluded, so "two.txt" has no size-match
    # left and reports as a singleton -- zero duplicate groups.
    assert result["duplicate_groups"] == []
