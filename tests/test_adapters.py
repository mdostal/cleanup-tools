"""Tests for OSAdapter implementations.

These tests run on macOS, so MacOSAdapter is exercised against a real
(but tmp_path-scoped) filesystem. ArchLinuxAdapter shares its file/dir
implementations with MacOSAdapter via the OSAdapter base class, so a
handful of tests instantiate it directly (read/write round trip, the
find_installed_app() NotImplementedError gate) even though we can't
meaningfully exercise Arch-specific OS behavior on this machine.
platform.system() is monkeypatched to cover get_adapter()'s Linux and
unsupported-OS branches without needing to actually run on those OSes.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from cleanup_tools.adapters import ArchLinuxAdapter, MacOSAdapter, get_adapter


@pytest.fixture
def adapter() -> MacOSAdapter:
    return MacOSAdapter()


# 1. resolve_home()
def test_resolve_home_returns_real_home_dir(adapter):
    from pathlib import Path

    assert adapter.resolve_home() == Path.home()


# 2. resolve_standard_dir()
def test_resolve_standard_dir_returns_sensible_paths_under_home(adapter):
    home = adapter.resolve_home()

    assert adapter.resolve_standard_dir("downloads") == home / "Downloads"
    assert adapter.resolve_standard_dir("desktop") == home / "Desktop"
    assert adapter.resolve_standard_dir("documents") == home / "Documents"
    assert adapter.resolve_standard_dir("pictures") == home / "Pictures"

    for kind in ("downloads", "desktop", "documents", "pictures"):
        resolved = adapter.resolve_standard_dir(kind)
        assert resolved.is_relative_to(home)


def test_resolve_standard_dir_unknown_kind_raises(adapter):
    with pytest.raises(ValueError):
        adapter.resolve_standard_dir("music")


# 3. list_dir() respects max_depth and pattern
def test_list_dir_respects_max_depth(adapter, tmp_path):
    (tmp_path / "top.txt").write_text("top")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "mid.txt").write_text("mid")
    deeper = nested / "deeper"
    deeper.mkdir()
    (deeper / "bottom.txt").write_text("bottom")

    # depth 0: only entries directly inside path (no descending into "nested")
    depth0 = adapter.list_dir(tmp_path, max_depth=0)
    assert {p.name for p in depth0} == {"top.txt"}

    # depth 1: top.txt and mid.txt, but not bottom.txt
    depth1 = adapter.list_dir(tmp_path, max_depth=1)
    assert {p.name for p in depth1} == {"top.txt", "mid.txt"}

    # unlimited depth: everything
    unlimited = adapter.list_dir(tmp_path, max_depth=None)
    assert {p.name for p in unlimited} == {"top.txt", "mid.txt", "bottom.txt"}


def test_list_dir_respects_pattern(adapter, tmp_path):
    (tmp_path / "screenshot-2024.png").write_text("img")
    (tmp_path / "screenshot_final.png").write_text("img")
    (tmp_path / "notes.txt").write_text("text")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "screenshot-nested.png").write_text("img")

    matches = adapter.list_dir(tmp_path, pattern="screenshot*")
    names = {p.name for p in matches}

    assert names == {"screenshot-2024.png", "screenshot_final.png", "screenshot-nested.png"}
    assert "notes.txt" not in names


def test_list_dir_pattern_is_case_insensitive(adapter, tmp_path):
    (tmp_path / "SCREENSHOT-upper.png").write_text("img")

    matches = adapter.list_dir(tmp_path, pattern="screenshot*")
    assert {p.name for p in matches} == {"SCREENSHOT-upper.png"}


# 4. move()
def test_move_creates_destination_parent_dirs_and_moves_file(adapter, tmp_path):
    src = tmp_path / "source.txt"
    src.write_text("hello")

    dst = tmp_path / "sub" / "dir" / "dest.txt"
    assert not dst.parent.exists()

    adapter.move(src, dst)

    assert not src.exists()
    assert dst.exists()
    assert dst.read_text() == "hello"


# 5. delete()
def test_delete_removes_file(adapter, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("data")

    adapter.delete(target)

    assert not target.exists()


def test_delete_removes_dir_recursively(adapter, tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    (target / "inner.txt").write_text("data")

    adapter.delete(target, recursive=True)

    assert not target.exists()


def test_delete_nonexistent_path_is_noop(adapter, tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    assert not missing.exists()

    # must not raise
    adapter.delete(missing)
    adapter.delete(missing, recursive=True)


# 6. disk_usage()
def test_disk_usage_returns_ints_greater_than_zero(adapter, tmp_path):
    usage = adapter.disk_usage(tmp_path)

    assert set(usage.keys()) == {"total", "used", "free"}
    for key in ("total", "used", "free"):
        assert isinstance(usage[key], int)
        assert usage[key] > 0


# 7. is_docker_installed()
def test_is_docker_installed_returns_true_when_which_finds_docker(adapter, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/docker")

    assert adapter.is_docker_installed() is True


def test_is_docker_installed_returns_false_when_which_finds_nothing(adapter, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert adapter.is_docker_installed() is False


# 8. find_installed_app() on MacOSAdapter
def test_find_installed_app_returns_false_for_fabricated_app(adapter, tmp_path):
    installer = tmp_path / "TotallyMadeUpAppThatDoesNotExist-9x7z.pkg"
    installer.write_text("fake installer")

    assert adapter.find_installed_app(installer) is False


def test_find_installed_app_returns_true_when_app_bundle_present(tmp_path):
    fake_applications = tmp_path / "Applications"
    fake_applications.mkdir()
    (fake_applications / "CoolApp.app").mkdir()

    fake_adapter = MacOSAdapter(applications_dir=fake_applications)
    installer = tmp_path / "CoolApp.dmg"

    assert fake_adapter.find_installed_app(installer) is True


def test_find_installed_app_uses_default_applications_dir():
    default_adapter = MacOSAdapter()
    assert default_adapter.applications_dir == Path("/Applications")


# 9. ArchLinuxAdapter.find_installed_app() raises NotImplementedError
def test_arch_linux_find_installed_app_raises_not_implemented(tmp_path):
    arch_adapter = ArchLinuxAdapter()
    installer = tmp_path / "some-package.pkg.tar.zst"

    with pytest.raises(NotImplementedError):
        arch_adapter.find_installed_app(installer)


# 10. get_adapter() returns MacOSAdapter on Darwin
def test_get_adapter_returns_macos_adapter_on_darwin():
    assert platform.system() == "Darwin"
    assert isinstance(get_adapter(), MacOSAdapter)


# 11. get_adapter() returns ArchLinuxAdapter on Linux
def test_get_adapter_returns_arch_linux_adapter_on_linux(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert isinstance(get_adapter(), ArchLinuxAdapter)


# 12. get_adapter() raises a clear error on an unsupported OS
def test_get_adapter_raises_on_unsupported_os(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    with pytest.raises(RuntimeError, match="Unsupported OS"):
        get_adapter()


# 13. read_file() / write_file()
def test_macos_write_file_then_read_file_round_trips_content(adapter, tmp_path):
    target = tmp_path / "notes.txt"

    adapter.write_file(target, "hello from macos")

    assert target.read_text() == "hello from macos"
    assert adapter.read_file(target) == "hello from macos"


def test_arch_linux_write_file_then_read_file_round_trips_content(tmp_path):
    arch_adapter = ArchLinuxAdapter()
    target = tmp_path / "notes.txt"

    arch_adapter.write_file(target, "hello from arch")

    assert target.read_text() == "hello from arch"
    assert arch_adapter.read_file(target) == "hello from arch"


def test_write_file_overwrites_existing_content(adapter, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("old content")

    adapter.write_file(target, "new content")

    assert adapter.read_file(target) == "new content"
