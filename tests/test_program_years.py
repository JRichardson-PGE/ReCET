"""ProgramCost has no ClaimYearQuarter (0.1.105): a program cost row's year, the year its costs are discounted from, is its PrgYear; a file that
still has the column is read and the column ignored; CEDARS' rules about it are left out of the shipped rule sets; the editor checks PrgYear."""

import pandas as pd
import pytest

from recet import editor, filing_rules as fr, templates
from recet.cet_versions import RELEASES
from recet.cet_versions.inputs import input_program
from recet.pipeline import cost_tests, statewide_allocation


def _write(tmp_path, rows) -> str:
    path = tmp_path / "ProgramCost.csv"
    pd.DataFrame(rows).to_csv(path, sep="|", index=False)
    return path


def _row(**kw) -> dict:
    return {**templates.EXAMPLE_PROGRAMCOST, **kw}


def test_the_template_has_no_program_cost_quarter():
    assert "ClaimYearQuarter" not in templates.PROGRAMCOST_COLUMNS and "ClaimYearQuarter" not in templates.EXAMPLE_PROGRAMCOST
    assert "PrgYear" in templates.PROGRAMCOST_COLUMNS and "PrgYear" in editor.CALC_COLUMNS["ProgramCost"]


def test_a_program_cost_row_is_discounted_from_its_prgyear(tmp_path):
    pc = cost_tests.load_program_costs(_write(tmp_path, [_row(PrgYear="2029")]))
    assert pc["Used_Year"].tolist() == [2029]
    # a file that still has the column: it is ignored, PrgYear decides
    pc = cost_tests.load_program_costs(_write(tmp_path, [_row(PrgYear="2029", ClaimYearQuarter="2031Q2")]))
    assert pc["Used_Year"].tolist() == [2029]
    # so the program totals discount from 2029 either way
    a = cost_tests.program_cost_sums(cost_tests.load_program_costs(_write(tmp_path, [_row(PrgYear="2029")])), 2028)
    b = cost_tests.program_cost_sums(pc, 2028)
    pd.testing.assert_frame_equal(a, b)


@pytest.mark.parametrize("year", ["", "2028Q4", "twenty"])
def test_a_program_cost_row_needs_a_year(tmp_path, year):
    with pytest.raises(ValueError, match="PrgYear must be a year"):
        cost_tests.load_program_costs(_write(tmp_path, [_row(PrgYear=year)]))


def test_statewide_allocation_reads_prgyear():
    df = pd.DataFrame([_row(PrgYear="2029", ClaimYearQuarter="2031Q2")])
    assert statewide_allocation._claim_year(df, "program_cost").tolist() == [2029]


@pytest.mark.parametrize("name", ["claims", "budget_filing"])
def test_cedars_rules_about_the_program_cost_quarter_are_left_out(name):
    rs = fr.rule_set(name)
    about = [r for r in rs.rules if fr._reads_not_taken(r)]
    assert about == []
    assert not [r for r in rs.rules if r.table == "ProgramCost" and "claimyearquarter" in str(r.spec).lower()]
    assert any(r.table == "Measure" and r.field == "ClaimYearQuarter" for r in rs.rules)  # a measure's quarter is still checked


def test_the_editor_checks_prgyear():
    m = pd.DataFrame([templates.EXAMPLE_MEASURE]).astype(str)
    pc = editor.conform(pd.DataFrame([_row(), _row(PrgYear="28")]).astype(str), editor.PROGRAMCOST_COLUMNS)
    issues = {(i.table, i.row, i.column, i.severity) for i in editor.check_tables(editor.conform(m, editor.MEASURE_COLUMNS), pc)}
    assert ("ProgramCost", 1, "PrgYear", "error") in issues
    pc = editor.conform(pd.DataFrame([_row(), _row()]).astype(str), editor.PROGRAMCOST_COLUMNS)
    issues = {(i.table, i.row, i.column, i.severity) for i in editor.check_tables(editor.conform(m, editor.MEASURE_COLUMNS), pc)}
    assert ("ProgramCost", 0, "PrgYear", "warning") in issues


def test_a_cet_replica_reads_the_quarter_as_cet_did_else_prgyear():
    release = next(iter(RELEASES.values()))
    with_q = input_program(pd.DataFrame([_row(PrgYear="2029", ClaimYearQuarter="2030Q1")]).astype("string"), release)
    without = input_program(pd.DataFrame([_row(PrgYear="2029")]).astype("string"), release)
    assert with_q["Year"].tolist() == [2030] and without["Year"].tolist() == [2029]
