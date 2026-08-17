"""Command-line entry point for cleanup-tools.

Wires up a single top-level argparse parser with one subparser per command
(``survey``, ``sort``, ``reclaim``, ``find-wallets``, ``dedupe``,
``propose-ai``, and ``approve``).
``main()`` builds one :class:`~cleanup_tools.adapters.base.OSAdapter` for the
whole invocation, dispatches to the matching command module's
``run(adapter, args)`` function -- passing the parsed argparse namespace so
commands with options (e.g. ``sort``'s ``dir``/``--go``, ``reclaim``'s
``dir``/``--go``/``--docker``) can read them, while options-less commands
(e.g. ``survey``) simply ignore ``args`` -- and prints the result as
pretty-printed JSON.

``approve`` and ``propose-ai`` are the two exceptions to that plain
``COMMANDS`` dispatch, each for its own reason: ``approve`` starts the
localhost approvals UI (see :mod:`cleanup_tools.ui.app`) and blocks for as
long as the server runs; ``propose-ai`` needs an
:class:`~cleanup_tools.ai.base.AIProvider` assembled via
:func:`cleanup_tools.ai.get_provider` first (which can fail with a routine
``CredentialsError`` if no API key is configured, printed as a clean error
rather than a traceback) and its ``QueueEntry`` results need
``dataclasses.asdict()`` before they fit the JSON-printing convention every
other subcommand follows.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import adapters
from .commands import corral_screenshots, dedupe, find_wallets, reclaim, sort, survey


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser and its command subparsers."""
    parser = argparse.ArgumentParser(
        prog="cleanup",
        description="Cross-platform disk/downloads cleanup CLI tooling.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "survey",
        help="Read-only snapshot of disk usage and clutter (no changes made).",
    )

    sort_parser = subparsers.add_parser(
        "sort",
        help="Sort loose files into _sorted/<bucket>/ (dry-run unless --go).",
    )
    sort_parser.add_argument(
        "dir",
        nargs="*",
        default=None,
        help=(
            "Root directories to sort (default: configured search_roots, "
            "else the platform Downloads dir)."
        ),
    )
    sort_parser.add_argument(
        "--go",
        action="store_true",
        default=False,
        help="Actually move files (default: dry-run, plan only).",
    )
    sort_parser.add_argument(
        "--from-queue",
        action="store_true",
        default=False,
        help=(
            "Execute already-approved 'move' entries from the approval queue "
            "whose src falls under the target directory, instead of "
            "computing a fresh plan."
        ),
    )

    reclaim_parser = subparsers.add_parser(
        "reclaim",
        help=(
            "Reclaim disk space from build caches, OS junk, and (with "
            "--docker) Docker (dry-run unless --go)."
        ),
    )
    reclaim_parser.add_argument(
        "dir",
        nargs="*",
        default=None,
        help=(
            "Root directories to scan (default: configured search_roots, "
            "else Documents and Desktop)."
        ),
    )
    reclaim_parser.add_argument(
        "--go",
        action="store_true",
        default=False,
        help="Actually delete/prune (default: dry-run, plan only).",
    )
    reclaim_parser.add_argument(
        "--docker",
        action="store_true",
        default=False,
        help=(
            "Also allow the Docker prune category to actually run -- only "
            "takes effect combined with --go."
        ),
    )
    reclaim_parser.add_argument(
        "--from-queue",
        action="store_true",
        default=False,
        help=(
            "Execute already-approved 'delete' entries from the approval "
            "queue whose src falls under a resolved root, instead of "
            "computing a fresh plan."
        ),
    )

    corral_screenshots_parser = subparsers.add_parser(
        "corral-screenshots",
        help=(
            "Move loose screenshot files into ~/Pictures/Screenshots, and "
            "(with --set-default-location) stop new ones landing on the "
            "Desktop (dry-run unless --go)."
        ),
    )
    corral_screenshots_parser.add_argument(
        "dir",
        nargs="*",
        default=None,
        help=(
            "Root directories to scan (default: configured search_roots, "
            "else Desktop, Downloads, and Documents)."
        ),
    )
    corral_screenshots_parser.add_argument(
        "--go",
        action="store_true",
        default=False,
        help="Actually move files (default: dry-run, plan only).",
    )
    corral_screenshots_parser.add_argument(
        "--from-queue",
        action="store_true",
        default=False,
        help=(
            "Execute already-approved 'move' entries from the approval "
            "queue whose src falls under a resolved root, instead of "
            "computing a fresh plan."
        ),
    )
    corral_screenshots_parser.add_argument(
        "--set-default-location",
        action="store_true",
        default=False,
        help=(
            "Also set the macOS default screenshot save location to "
            "~/Pictures/Screenshots. Independent of --go/--from-queue -- "
            "never triggered by either of them alone."
        ),
    )

    find_wallets_parser = subparsers.add_parser(
        "find-wallets",
        help=(
            "Read-only scan for crypto-wallet artifacts by filename and "
            "content signature (never writes/moves/deletes anything)."
        ),
    )
    find_wallets_parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Root directory to scan (default: the user's home directory).",
    )

    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help=(
            "Read-only scan for duplicate files by size then full-content "
            "hash (never writes/moves/deletes anything)."
        ),
    )
    dedupe_parser.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Directory to scan (default: the platform Downloads dir).",
    )

    propose_ai_parser = subparsers.add_parser(
        "propose-ai",
        help=(
            "Ask the configured AI provider to bucket ambiguous ('other') "
            "files and stage the successful proposals into the approval "
            "queue, same as `approve`'s UI would."
        ),
    )
    propose_ai_parser.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Directory to scan (default: the platform Downloads dir, same as sort).",
    )
    propose_ai_parser.add_argument(
        "--cap",
        type=int,
        default=None,
        help="Max number of AI proposals to request in one run (default: 20).",
    )

    approve_parser = subparsers.add_parser(
        "approve",
        help=(
            "Start the local approvals UI (binds to 127.0.0.1 only) and "
            "open it in your default browser."
        ),
    )
    approve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind the approvals UI on (default: 5151).",
    )
    approve_parser.add_argument(
        "--no-browser",
        action="store_true",
        default=False,
        help="Don't automatically open the default web browser.",
    )

    return parser


COMMANDS = {
    "survey": survey.run,
    "sort": sort.run,
    "reclaim": reclaim.run,
    "corral-screenshots": corral_screenshots.run,
    "find-wallets": find_wallets.run,
    "dedupe": dedupe.run,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    adapter = adapters.get_adapter()

    if args.command == "approve":
        # The approvals UI is a long-running server, not a one-shot
        # JSON-producing command like every other subcommand -- it is
        # dispatched here directly rather than through COMMANDS/json.dumps.
        from .ui.app import DEFAULT_PORT, run_server

        run_server(
            adapter,
            port=args.port if args.port is not None else DEFAULT_PORT,
            open_browser=not args.no_browser,
        )
        return 0

    if args.command == "propose-ai":
        # Also dispatched directly rather than through COMMANDS: it needs an
        # AIProvider assembled via get_provider() first (which can fail with
        # a routine, expected CredentialsError if no API key is configured
        # anywhere -- printed as a clean one-line error, not a traceback),
        # and its QueueEntry results need dataclasses.asdict() before they're
        # JSON-serializable in the same shape every other subcommand prints.
        from . import config as config_module
        from .ai import CredentialsError, get_provider
        from .ai.wiring import DEFAULT_CAP, propose_for_other_bucket

        try:
            provider = get_provider()
        except CredentialsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        config = config_module.load_config(adapter)
        target_dir = Path(args.dir) if args.dir else None
        cap = args.cap if args.cap is not None else DEFAULT_CAP
        result = propose_for_other_bucket(adapter, config, provider, cap, target_dir=target_dir)
        output = {
            "proposed": [dataclasses.asdict(e) for e in result["proposed"]],
            "failures": result["failures"],
            "calls_made": result["calls_made"],
        }
        print(json.dumps(output, indent=2, default=str))
        return 0

    command_fn = COMMANDS[args.command]
    result = command_fn(adapter, args)

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
