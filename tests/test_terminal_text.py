# SPDX-License-Identifier: Apache-2.0
"""Third-party text (tool output, titles) is cleaned before it reaches a terminal or the Markdown."""

import re

import pytest
from test_build import make_design
from test_results import design, put, work  # noqa: F401 - fixtures

from tthfv import build, reference, results
from tthfv.text import strip_control

CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
EVIL = "bad\x1b[31mred\x07\r\nnext\x7f\ttab"


def test_strip_control_removes_everything_below_space_but_tab_and_del():
    assert strip_control(EVIL) == "bad[31mrednexttab".replace("tab", "\ttab")
    assert strip_control("plain") == "plain"


def test_every_markdown_cell_is_escaped_and_cleaned(work):  # noqa: F811
    d = design("tt01/tt_um_a", tiles="1x1|\x1b[31m", title="t\x1bi|tle")
    md = results.render_markdown(results.collect([d], work))
    assert not CONTROL.search(md.replace("\n", ""))
    row = next(r for r in md.splitlines() if r.startswith("| tt01/tt_um_a"))
    assert len(row.replace(r"\|", "").split("|")) == 11
    assert r"1x1\|" in row


def test_stale_reasons_in_the_markdown_are_cleaned(work):  # noqa: F811
    put(work, "build", "tt01/tt_um_a", "build.json", {"status": "ok", "commit": "x\x1b[1m"})
    md = results.render_markdown(results.collect([design("tt01/tt_um_a")], work))
    assert "stale" in md and "\x1b" not in md


def test_build_line_strips_control_characters():
    d = make_design()
    r = build.BuildResult(id=d.id, status="synth-fail", reason=EVIL)
    line = build._line(d, r)
    assert not CONTROL.search(line) and "red" in line


def test_reference_command_prints_clean_reasons(monkeypatch, capsys):
    from tthfv.cli import main

    d = make_design()
    monkeypatch.setattr(reference.manifest, "load", lambda **kw: [d])
    monkeypatch.setattr(
        reference, "make_reference", lambda *a, **k: reference.ReferenceResult("sim-fail", None, 0, EVIL)
    )
    assert main(["reference", d.id]) == 1
    out = capsys.readouterr().out
    assert not CONTROL.search(out.rstrip("\n")) and "red" in out


def test_fetch_command_prints_clean_reasons(monkeypatch, capsys):
    from tthfv import fetch
    from tthfv.cli import main

    d = make_design()
    monkeypatch.setattr(fetch.manifest, "load", lambda **kw: [d])
    monkeypatch.setattr(fetch, "fetch", lambda *a, **k: fetch.FetchResult("fetch-fail", fetch.Path(), (), (), fetch.Path(), EVIL))
    assert main(["fetch", d.id]) == 1
    assert not CONTROL.search(capsys.readouterr().out.rstrip("\n"))


@pytest.mark.parametrize("bad", ["\x1b]0;title\x07", "\x00"])
def test_other_sequences(bad):
    assert strip_control(f"a{bad}b").count("\x1b") == 0
