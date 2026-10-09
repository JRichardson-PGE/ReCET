"""Preflight checks that mirror CET's own QC rules (cet_spec.sql): blanks that would silently blank a result, and out-of-range values."""
from __future__ import annotations

import pytest

from recet import preflight
from conftest import make_measure_csv


def _codes(path, severity):
    return {i.code for i in preflight.check_inputs(path).issues if i.severity == severity}


@pytest.mark.parametrize("column", ["NTGRkWh", "NumUnits", "EUL_Yrs", "UnitkWh1stBaseline"])
def test_a_blank_the_arithmetic_cannot_do_without_is_a_blocking_error(tmp_path, column):
    """A blank NTGRkWh used to give ElecBen = 0 and a blank (NaN) TSB and TRC cost with nothing flagged."""
    p = make_measure_csv(tmp_path / "m.csv", **{column: ""})
    assert "blank_required_value" in _codes(p, "error")


def test_a_second_baseline_is_needed_on_early_replacement_measures(tmp_path):
    p = make_measure_csv(tmp_path / "m.csv", RUL_Yrs="1", EUL_Yrs="3", UnitkWh2ndBaseline="")
    assert "blank_second_baseline" in _codes(p, "error")
    ok = make_measure_csv(tmp_path / "m2.csv", RUL_Yrs="0", EUL_Yrs="3", UnitkWh2ndBaseline="")     # single baseline: the 2nd baseline is never used
    assert "blank_second_baseline" not in _codes(ok, "error")


@pytest.mark.parametrize("column,value", [("NTGRkWh", "1.6"), ("NTGRkWh", "0"), ("EUL_Yrs", "41"), ("RUL_Yrs", "31"), ("InstallationRatekWh", "1.2"),
                                          ("RealizationRatekWh", "2.5"), ("MeasInflation", "1.5"), ("UnitMeaCost1stBaseline", "-5")])
def test_values_outside_cets_ranges_are_warnings_not_errors(tmp_path, column, value):
    p = make_measure_csv(tmp_path / "m.csv", **{column: value})
    rep = preflight.check_inputs(p)
    assert "outside_cet_range" in {i.code for i in rep.issues if i.severity == "warning"}
    assert rep.ok                                                  # the tool still prices it


def test_an_rul_that_is_not_below_the_eul_is_flagged(tmp_path):
    assert "rul_not_below_eul" in _codes(make_measure_csv(tmp_path / "m.csv", RUL_Yrs="5", EUL_Yrs="4"), "warning")


def test_a_clean_row_raises_none_of_them(tmp_path):
    codes = _codes(make_measure_csv(tmp_path / "m.csv"), "warning") | _codes(tmp_path / "m.csv", "error")
    assert not codes & {"blank_required_value", "blank_second_baseline", "outside_cet_range", "rul_not_below_eul"}
