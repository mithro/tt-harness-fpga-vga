# SPDX-License-Identifier: Apache-2.0
"""Command line entry point. Each stage module exposes add_parser(sub) and is listed in STAGES."""

import argparse
import sys
from types import ModuleType

from tthfv import build, compare, demos, fetch, manifest, reference, results, runner, tools
from tthfv.board import command as board
from tthfv.errors import UserError

# Stage modules, one per pipeline stage. Each has add_parser(sub), which adds its subparser and
# calls set_defaults(func=...) with a callable taking the parsed Namespace and returning an int.
STAGES: list[ModuleType] = [tools, manifest, fetch, build, reference, runner, results, demos, compare, board]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tthfv",
        description="Build FPGA bitstreams of Tiny Tapeout VGA designs and produce reference renders.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    for stage in STAGES:
        stage.add_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UserError as e:
        print(f"tthfv: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
