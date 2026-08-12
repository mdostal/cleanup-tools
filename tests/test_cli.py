"""Tests for cleanup_tools.cli.

main(argv) accepts an explicit argv list, so tests never depend on the
process's real sys.argv. The "survey" subcommand test stubs out the
survey command function so it doesn't touch the real machine: note that
cli.COMMANDS binds `survey.run` once at import time (COMMANDS = {"survey":
survey.run}), so patching the `run` attribute on the survey module after
the fact would not affect the already-bound dict entry -- the dict itself
must be patched, via monkeypatch.setitem(cli.COMMANDS, ...), for the stub to
actually be used by main().
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cleanup_tools import cli
from cleanup_tools.adapters import MacOSAdapter


# ---------------------------------------------------------------------------
# No subcommand: prints help, returns 1.
# ---------------------------------------------------------------------------


def test_main_no_subcommand_prints_help_and_returns_1(capsys):
    exit_code = cli.main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert "usage" in captured.out.lower()
    assert "cleanup" in captured.out.lower()


# ---------------------------------------------------------------------------
# "survey" subcommand: produces valid JSON on stdout, returns 0.
# ---------------------------------------------------------------------------


def test_main_survey_subcommand_prints_valid_json_and_returns_0(monkeypatch, capsys):
    fake_result = {
        "disk": {"total": 1000, "used": 400, "free": 600},
        "home_top_level": {"Documents": 100, "Downloads": 50},
        "documents_work_biggest_repos": {},
        "node_modules": {"total_bytes": 0, "dir_count": 0},
        "downloads_by_extension": {},
        "screenshot_count": 0,
    }
    monkeypatch.setitem(cli.COMMANDS, "survey", lambda adapter, args: fake_result)

    exit_code = cli.main(["survey"])

    captured = capsys.readouterr()
    assert exit_code == 0

    parsed = json.loads(captured.out)
    assert parsed == fake_result


def test_main_survey_subcommand_passes_an_adapter_instance_to_run(monkeypatch, capsys):
    received = {}

    def fake_run(adapter, args):
        received["adapter"] = adapter
        return {}

    monkeypatch.setitem(cli.COMMANDS, "survey", fake_run)

    exit_code = cli.main(["survey"])

    assert exit_code == 0
    assert received["adapter"] is not None
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Unknown subcommand: argparse itself rejects it (exits non-zero) before
# main() ever dispatches, since argparse validates against the registered
# subparsers.
# ---------------------------------------------------------------------------


def test_main_unknown_subcommand_raises_system_exit(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["not-a-real-command"])

    assert exc_info.value.code != 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# build_parser(): sanity checks independent of main()'s dispatch behavior.
# ---------------------------------------------------------------------------


def test_build_parser_defaults_command_to_none_with_no_args():
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.command is None


def test_build_parser_recognizes_survey_subcommand():
    parser = cli.build_parser()
    args = parser.parse_args(["survey"])
    assert args.command == "survey"


# ---------------------------------------------------------------------------
# "sort" subcommand: parses dir/--go, dispatches through cli.COMMANDS like
# survey does. cli.COMMANDS binds sort.run at import time, so (as with
# survey above) the stub must replace the dict entry via
# monkeypatch.setitem(cli.COMMANDS, "sort", fake_run) rather than patching
# sort.run directly.
# ---------------------------------------------------------------------------


def test_build_parser_recognizes_sort_subcommand_with_dir_and_go():
    parser = cli.build_parser()
    args = parser.parse_args(["sort", "/some/dir", "--go"])
    assert args.command == "sort"
    assert args.dir == "/some/dir"
    assert args.go is True


def test_build_parser_sort_subcommand_defaults_dir_none_and_go_false():
    parser = cli.build_parser()
    args = parser.parse_args(["sort"])
    assert args.command == "sort"
    assert args.dir is None
    assert args.go is False


def test_main_sort_subcommand_prints_valid_json_and_returns_0(monkeypatch, capsys):
    fake_result = {"dir": "/some/dir", "go": True, "plan": []}
    monkeypatch.setitem(cli.COMMANDS, "sort", lambda adapter, args: fake_result)

    exit_code = cli.main(["sort", "/some/dir", "--go"])

    captured = capsys.readouterr()
    assert exit_code == 0

    parsed = json.loads(captured.out)
    assert parsed == fake_result


def test_main_sort_subcommand_passes_args_with_dir_and_go_to_run(monkeypatch, capsys):
    received = {}

    def fake_run(adapter, args):
        received["args"] = args
        return {}

    monkeypatch.setitem(cli.COMMANDS, "sort", fake_run)

    exit_code = cli.main(["sort", "/some/dir", "--go"])

    assert exit_code == 0
    assert received["args"].dir == "/some/dir"
    assert received["args"].go is True
    capsys.readouterr()


# ---------------------------------------------------------------------------
# End-to-end "sort" dry-run through the real cli.main() -> sort.run() path
# (no stubbed command function this time -- unlike every test above, which
# only ever exercises cli.main()'s dispatch against a fake command). This
# is the one test proving the real seam between argument parsing, adapter
# construction, and sort's own dry-run guard: that a genuine `cleanup sort
# <dir>` invocation with no --go leaves the filesystem completely
# untouched. adapters.get_adapter() is monkeypatched only to redirect
# resolve_home() into tmp_path (so config loading never touches the real
# user home); the adapter itself, and sort.run(), are real.
# ---------------------------------------------------------------------------


def test_main_sort_dry_run_end_to_end_leaves_filesystem_untouched(monkeypatch, capsys, tmp_path):
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self) -> Path:
            return home_dir

    fake_adapter = FakeHomeAdapter()
    monkeypatch.setattr(cli.adapters, "get_adapter", lambda: fake_adapter)

    target = tmp_path / "target"
    target.mkdir()
    photo = target / "photo.jpg"
    photo.write_text("photo-bytes")
    doc = target / "notes.txt"
    doc.write_text("doc-bytes")

    exit_code = cli.main(["sort", str(target)])

    captured = capsys.readouterr()
    assert exit_code == 0

    parsed = json.loads(captured.out)
    assert parsed["go"] is False
    assert {entry["src"] for entry in parsed["plan"]} == {str(photo), str(doc)}

    # Real filesystem, genuinely untouched: no --go was passed, and this
    # went through the real sort.run(), not a stub.
    assert photo.exists() and photo.read_text() == "photo-bytes"
    assert doc.exists() and doc.read_text() == "doc-bytes"
    assert not (target / "_sorted").exists()
