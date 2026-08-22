"""Brand-state symbols must remain stable across terminal renderers."""

from rich.cells import cell_len

import symbols


def test_relay_spinner_has_a_stable_two_cell_footprint():
    assert symbols.SPINNER_RELAY == ("L·", "L›", "L»", "L›")
    assert {cell_len(frame) for frame in symbols.SPINNER_RELAY} == {2}


def test_legacy_spinner_names_follow_relay_identity():
    assert symbols.SPINNER_BRAILLE is symbols.SPINNER_RELAY
    assert symbols.SPINNER_GEO is symbols.SPINNER_RELAY
