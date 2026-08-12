"""Command-line entry point for cleanup-tools.

Wires up a single top-level argparse parser with one subparser per command
(``survey`` for now; ``sort``/``reclaim`` land in later stories). ``main()``
builds one :class:`~cleanup_tools.adapters.base.OSAdapter` for the whole
invocation, dispatches to the matching command module's ``run(adapter)``
function, and prints the result as pretty-printed JSON.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import adapters
from .commands import survey


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

    # Future stories add "sort" and "reclaim" subparsers here.

    return parser


COMMANDS = {
    "survey": survey.run,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    adapter = adapters.get_adapter()
    command_fn = COMMANDS[args.command]
    result = command_fn(adapter)

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
