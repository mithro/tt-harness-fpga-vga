# SPDX-License-Identifier: Apache-2.0
import pytest

from tthfv.stimulus import fixed_inputs


def test_fixed_ui_in_from_override():
    assert fixed_inputs({"ui_in": 5}, None) == (5, 0)


def test_override_wins_over_script():
    assert fixed_inputs({"ui_in": 5}, "0 3 128\n") == (5, 0)


def test_first_line_of_inputs_script():
    assert fixed_inputs({}, "0 3 128\n500000 1 0\n") == (3, 128)


def test_hex_form_as_in_real_files():
    script = "# clock ui_in uio_in\n0 0x02 0x80\n94500000 0x08 0x00\n"
    assert fixed_inputs({"inputs": "x.inputs"}, script) == (2, 128)


def test_default_is_zero():
    assert fixed_inputs({}, None) == (0, 0)
    assert fixed_inputs({"gamepad": "x.gamepad"}, None) == (0, 0)
    assert fixed_inputs({}, "# only a comment\n") == (0, 0)


def test_malformed_line_is_loud():
    with pytest.raises(ValueError):
        fixed_inputs({}, "0 1\n")
