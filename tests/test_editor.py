"""The input editor's model (recet/editor.py): loading, per-cell checks, dropdown values and saving. No window and no reference data needed."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import editor, fieldnames, preflight, templates, workbook
from conftest import make_flat_profile_csv, make_measure_csv


def _job():
    return editor.new_job()


def _issues(m, pc=None, choices=None):
    return {(i.table, i.row, i.column, i.severity) for i in editor.check_tables(m, pc if pc is not None else _job().programcost, choices)}


def test_new_job_is_the_template_example_and_has_no_problems():
    job = _job()
    assert list(job.measures.columns) == templates.MEASURE_COLUMNS
    assert list(job.programcost.columns) == templates.PROGRAMCOST_COLUMNS
    choices = editor.ChoiceSource()
    choices.load_profiles()                       # no reference data here: only the gas library (shipped in the package) is known
    assert editor.check_tables(job.measures, job.programcost, choices) == []


@pytest.mark.parametrize("column, value, severity", [
    ("CEInputID", "", "error"),
    ("ClaimYearQuarter", "2028-3", "error"),
    ("IOU_AC_Territory", "PG&E", "error"),
    ("NumUnits", "ten", "error"),
    ("EUL_Yrs", "", "error"),
    ("RUL_Yrs", "", "error"),                    # blank (CET requires a value); the 2nd-baseline profile checks still run
    ("RUL_Yrs", "x", "error"),
    ("NTGRkWh", "1.6", "warning"),
    ("InstallationRatekWh", "1.2", "warning"),
    ("Sector", "Com", "warning"),                # retired in CET's list after 2018; the claim is 2028
    ("MeasImpactType", "Deemed", "warning"),     # not in CET's list at all
    ("Residential_Flag", "yes", "warning"),
    ("MeasDescription", "a|b", "error"),
])
def test_each_rule_flags_the_cell(column, value, severity):
    m = _job().measures
    m.loc[0, column] = value
    assert ("Measure", 0, column, severity) in _issues(m)


def test_second_baseline_and_rul_rules():
    m = _job().measures
    m.loc[0, ["RUL_Yrs", "UnitkWh2ndBaseline"]] = ["5", ""]
    assert ("Measure", 0, "UnitkWh2ndBaseline", "error") in _issues(m)
    m.loc[0, ["RUL_Yrs", "UnitkWh2ndBaseline", "EUL_Yrs"]] = ["15", "10", "15"]
    assert ("Measure", 0, "RUL_Yrs", "warning") in _issues(m)


def test_duplicate_ids_flag_both_rows():
    m = pd.concat([_job().measures] * 2, ignore_index=True)
    got = _issues(m)
    assert ("Measure", 0, "CEInputID", "error") in got and ("Measure", 1, "CEInputID", "error") in got


def test_program_ids_must_line_up_both_ways():
    job = _job()
    m, pc = job.measures, job.programcost
    m.loc[0, "PrgID"] = "OTHER"
    got = _issues(m, pc)
    assert ("Measure", 0, "PrgID", "error") in got and ("ProgramCost", 0, "PrgID", "error") in got
    # without program costs, a measure's PrgID is not checked against anything
    assert ("Measure", 0, "PrgID", "error") not in _issues(m, editor.conform(None, editor.PROGRAMCOST_COLUMNS))


def test_gas_rules_apply_only_with_gas_savings():
    m = _job().measures                          # the example has UnitTherm1stBaseline -0.5
    m.loc[0, "AC_Gas_Target_Sector"] = "Retail"
    assert ("Measure", 0, "AC_Gas_Target_Sector", "error") in _issues(m)
    m.loc[0, "UnitTherm1stBaseline"] = "0"
    assert ("Measure", 0, "AC_Gas_Target_Sector", "error") not in _issues(m)


def test_profile_checks_use_the_supplied_and_library_profiles(tmp_path):
    m = _job().measures
    choices = editor.ChoiceSource()
    choices.load_profiles()
    m.loc[0, "AC_Gas_Impact_Profile"] = "No Such Profile"
    assert ("Measure", 0, "AC_Gas_Impact_Profile", "warning") in _issues(m, choices=choices)
    # a supplied 8,760 profile is offered in the end-use dropdown for its IOU
    prof = make_flat_profile_csv(tmp_path / "p.csv", end_use="MY_SHAPE", pa="PGE", sector="Commercial")
    choices.load_profiles(profiles_8760_file=str(prof))
    assert "MY_SHAPE" in choices.end_use_choices("PGE", "COMMERCIAL")


def test_blank_rows_are_ignored_by_checks_and_dropped_on_save(tmp_path):
    job = _job()
    m = pd.concat([job.measures, pd.DataFrame([[""] * len(job.measures.columns)], columns=job.measures.columns)], ignore_index=True)
    assert all(i.row == 0 for i in editor.check_tables(m, job.programcost))
    files = editor.write_cet_files(m, job.programcost, tmp_path)
    assert len(pd.read_csv(files["measures"], sep="|", dtype=str)) == 1


def test_errors_agree_with_the_preflight(tmp_path):
    """A blocking cell error here is a blocking preflight error on the saved files, and a clean table passes the preflight's format checks."""
    job = _job()
    m = job.measures.copy()
    m.loc[0, "NumUnits"] = ""
    files = editor.write_cet_files(m, job.programcost, tmp_path / "bad")
    assert not preflight.check_inputs(files["measures"], files["programcost"]).ok
    files = editor.write_cet_files(job.measures, job.programcost, tmp_path / "good")
    rep = preflight.check_inputs(files["measures"], files["programcost"])
    assert rep.ok, rep.text()


def test_cet_files_round_trip(tmp_path):
    job = _job()
    files = editor.write_cet_files(job.measures, job.programcost, tmp_path)
    back = editor.load_tables(str(files["measures"]), str(files["programcost"]))
    pd.testing.assert_frame_equal(back.measures, job.measures)
    pd.testing.assert_frame_equal(back.programcost, job.programcost)


def test_workbook_round_trip_keeps_the_other_sheets(tmp_path):
    base = templates.write_workbook_template(tmp_path / "base.xlsx", "2024")
    job = editor.load_tables(workbook=str(base))
    assert job.base_workbook == str(base)
    m = job.measures.copy()
    m.loc[0, "NumUnits"] = "250"
    out = editor.write_workbook(m, job.programcost, tmp_path / "edited.xlsx", job.base_workbook)
    assert set(workbook.sheet_names(out)) == set(workbook.sheet_names(base))
    back = editor.load_tables(workbook=str(out))
    pd.testing.assert_frame_equal(back.measures, m)
    pd.testing.assert_frame_equal(back.programcost, job.programcost)


def test_a_file_chosen_separately_wins_over_its_sheet(tmp_path):
    base = templates.write_workbook_template(tmp_path / "base.xlsx", "2024")
    other = make_measure_csv(tmp_path / "Measure.csv")
    job = editor.load_tables(measures=str(other), workbook=str(base))
    assert job.measures["CEInputID"].tolist() == ["T1"]
    assert job.programcost["PrgID"].tolist() == ["EXAMPLE_PRG"]       # still the workbook's ProgramCost sheet


def test_prepare_run_inputs_uses_the_workbooks_other_sheets_unless_given(tmp_path):
    base = templates.write_workbook_template(tmp_path / "base.xlsx", "2024")
    job = editor.load_tables(workbook=str(base))
    upd, folder = editor.prepare_run_inputs(job.measures, job.programcost, tmp_path / "out", job.base_workbook, {"sw_factors_file": "mine.csv"})
    assert upd["workbook"] is None and upd["measures"].endswith("Measure.csv") and upd["programcost"].endswith("ProgramCost.csv")
    assert "run_parameters_file" in upd and "sw_factors_file" not in upd
    assert folder.parent.name == "_edited_inputs"
    # no program-cost rows: the run gets no program costs, even though the workbook has a ProgramCost sheet
    upd, _ = editor.prepare_run_inputs(job.measures, editor.conform(None, editor.PROGRAMCOST_COLUMNS), tmp_path / "out2", job.base_workbook)
    assert upd["programcost"] is None


def test_large_jobs_are_refused(tmp_path):
    job = _job()
    big = pd.concat([job.measures] * 3, ignore_index=True)
    big["CEInputID"] = [f"M{i}" for i in range(len(big))]
    files = editor.write_cet_files(big, None, tmp_path)
    with pytest.raises(editor.EditorError, match="up to 2"):
        editor.load_tables(str(files["measures"]), max_rows=2)


def test_value_list_years():
    assert "Com" not in editor.active_codes("sector", 2028) and "Commercial" in editor.active_codes("sector", 2028)
    assert "Com" in editor.active_codes("sector", 2015)
    assert editor.code_status("sector", "commercial", 2028) is None            # case does not matter
    assert "retired" in editor.code_status("sector", "Com", 2028)


def test_value_list_admin_retirements_are_worded_for_whoever_runs_the_lists():
    """An administrator's retirement names no product; a code retired before any year (a platform's value-list version that leaves it
    out) reads as not in the list in use, never as "retired after 0" (CEDARS platform UAT 2, U07)."""
    try:
        editor.set_value_list_changes({"sector": {"Commercial": {"kind": "retired", "end_year": 2026},
                                                  "Industrial": {"kind": "retired", "end_year": 0}}})
        assert editor.code_status("sector", "Commercial", 2028) == "retired by an administrator after 2026"
        assert editor.code_status("sector", "Commercial", 2026) is None
        assert editor.code_status("sector", "industrial", 2028) == "not in the value list in use"
        assert editor.code_status("sector", "Industrial", 2015) == "not in the value list in use"
        assert "Industrial" not in editor.active_codes("sector", 2028)
    finally:
        editor.set_value_list_changes({})


def test_new_rows_get_unique_ids_and_the_neighbours_keys():
    like = _job().measures.iloc[0].to_dict()
    row = editor.new_measure_row(like, ["NEW_1"])
    assert row["CEInputID"] == "NEW_2" and row["PrgID"] == like["PrgID"] and row["IOU_AC_Territory"] == "PGE"
    assert row["NTGRkWh"] == "" and row["RealizationRatekWh"] == "1"
    assert editor.unique_id("EXAMPLE_1", ["EXAMPLE_1", "EXAMPLE_2"]) == "EXAMPLE_3"


def test_field_help_covers_every_cet_column():
    help_ = editor.field_help()
    missing = [c for c in editor.MEASURE_COLUMNS + editor.PROGRAMCOST_COLUMNS if c not in help_]
    assert not missing, missing


def test_display_layout_shows_every_column_once_pinned_then_calculation_then_no_effect():
    cols = editor.MEASURE_COLUMNS + ["MyExtra"]
    frozen, scroll, no_effect = editor.display_layout("Measure", cols)
    assert frozen == ["CEInputID", "MeasCode", "MeasDescription"]
    assert sorted(frozen + scroll) == sorted(cols) and len(frozen + scroll) == len(cols)
    assert set(frozen) <= no_effect                                   # the pinned ID, code and description change no result (blue headers)
    first_inert = min(scroll.index(c) for c in no_effect if c in scroll)
    assert all(c not in no_effect for c in scroll[:first_inert]) and all(c in no_effect for c in scroll[first_inert:])
    assert scroll[-1] == "MyExtra"
    assert all(editor.affects_results("Measure", c) == (c not in no_effect) for c in cols)
    # every column the preflight requires for pricing is on the calculation side (bar the labels)
    assert not set(preflight.REQUIRED_MEASURE_COLUMNS) & no_effect - {"Sector", "CEInputID", "MeasDescription"}
    assert {"RateScheduleElec", "RateScheduleGas", "NTG_ID", "Comments"} <= no_effect
    frozen, scroll, no_effect = editor.display_layout("ProgramCost", editor.PROGRAMCOST_COLUMNS)
    assert frozen == ["PrgID"] and no_effect == {"PrgYear", "OnBillFinancing"} and scroll[-2:] == ["PrgYear", "OnBillFinancing"]


def test_files_keep_cets_column_order_whatever_order_they_are_given_in(tmp_path):
    job = _job()
    frozen, scroll, _ = editor.display_layout("Measure", list(job.measures.columns))
    files = editor.write_cet_files(job.measures[frozen + scroll], job.programcost, tmp_path)
    head = files["measures"].read_text(encoding="utf-8").splitlines()[0].split("|")
    assert head == [fieldnames.RECET_TO_CET.get(c, c) for c in templates._CET_MEASURE_HEADER]   # CET's names and order; ReCET-only columns left out while blank
    assert "E3ClimateZone" in head and "AC_Climate_Zone" not in head


def test_a_used_recet_only_column_is_written_after_cets_columns(tmp_path):
    job = _job()
    m = job.measures.copy()
    m.loc[0, "AC_Elec_Impact_Profile_Water"] = "W_SHAPE"
    head = editor.write_cet_files(m, job.programcost, tmp_path)["measures"].read_text(encoding="utf-8").splitlines()[0].split("|")
    assert head == [fieldnames.RECET_TO_CET.get(c, c) for c in templates._CET_MEASURE_HEADER] + ["AC_Elec_Impact_Profile_Water"]   # no CET name: ReCET's


@pytest.mark.parametrize("text, shown", [("0.600000", "0.6"), ("15.0", "15"), ("100", "100"), ("-0.50", "-0.5"), ("4.969198677", "4.969199"),
                                         ("0.0000004", "4e-07"), ("1e-3", "0.001"), ("-0.0000001", "-1e-07"), ("", ""), ("ten", "ten"), ("-0.0", "0")])
def test_numbers_are_shown_without_trailing_zeros_and_at_most_six_decimals(text, shown):
    assert editor.display_number(text) == shown


def test_every_column_has_a_header_tooltip_that_matches_its_colour():
    for table, cols in (("Measure", editor.MEASURE_COLUMNS), ("ProgramCost", editor.PROGRAMCOST_COLUMNS)):
        assert set(editor.EFFECTS[table]) <= set(cols), "a tooltip for a column that does not exist"
        for c in cols:
            tip = editor.column_tooltip(table, c)
            if editor.affects_results(table, c):
                assert "CHANGES RESULTS" in tip and "Affects: " in tip and editor.EFFECTS[table][c][0], c
            else:
                assert "NO EFFECT ON RESULTS" in tip and "Affects" not in tip, c
    assert "gas NOx" in editor.column_tooltip("Measure", "CombustionType")
    assert "Not read" in editor.column_tooltip("Measure", "RateScheduleGas")
