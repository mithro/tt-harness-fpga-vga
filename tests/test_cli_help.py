# SPDX-License-Identifier: Apache-2.0
"""`--help` of every command and sub-command works (an unescaped % in a help string once crashed it)."""

import argparse

import pytest

from tthfv import cli


def command_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = [prefix]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found += command_paths(sub, (*prefix, name))
    return found


PATHS = command_paths(cli.build_parser())


def test_the_walk_found_the_nested_commands():
    assert ("run",) in PATHS and ("manifest", "sync") in PATHS and ("tools", "install") in PATHS
    assert len(PATHS) >= 12


@pytest.mark.parametrize("path", PATHS, ids=[" ".join(p) or "tthfv" for p in PATHS])
def test_help_exits_zero_and_prints_usage(path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([*path, "--help"])
    assert e.value.code == 0
    assert "usage:" in capsys.readouterr().out
