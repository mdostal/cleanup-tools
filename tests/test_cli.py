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

import pytest

from cleanup_tools import cli


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
    monkeypatch.setitem(cli.COMMANDS, "survey", lambda adapter: fake_result)

    exit_code = cli.main(["survey"])

    captured = capsys.readouterr()
    assert exit_code == 0

    parsed = json.loads(captured.out)
    assert parsed == fake_result


def test_main_survey_subcommand_passes_an_adapter_instance_to_run(monkeypatch, capsys):
    received = {}

    def fake_run(adapter):
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
