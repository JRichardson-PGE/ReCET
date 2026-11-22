"""A ProgramCost row whose PA has no run parameters at the run's ACC (a REN or CCA on the 2022 ACC on) is discounted at SCE's rates, as CEDARS passes
non-IOU program costs to CET (CET_UI job 123406); a PA with its own rows keeps them."""

import pandas as pd
import pytest

from recet.pipeline import cost_tests, discount_settings


def _program(pa: str, year: int) -> pd.DataFrame:
    row = {f: 0.0 for f in cost_tests._COST_FIELDS}
    row.update(PrgID="P1", PA=pa, Used_Year=year, AdminCostsOverheadAndGA=1000.0)
    return pd.DataFrame([row])


def test_a_ren_on_the_2024_acc_has_no_run_parameters_of_its_own():
    assert not discount_settings.has_settings("BAY", 2024)
    assert discount_settings.has_settings("BAY", 2020)  # older ACCs carry the CCAs and RENs
    assert discount_settings.has_settings("SCE", 2024) and discount_settings.has_settings("PG&E", 2024)


@pytest.mark.parametrize("pa", ["BAY", "MCE"])
def test_a_ren_or_cca_program_cost_is_discounted_at_sces_rate(pa):
    first = 2027
    got = cost_tests.program_cost_sums(_program(pa, first + 2), first, 2024).iloc[0]
    sce = cost_tests.program_cost_sums(_program("SCE", first + 2), first, 2024).iloc[0]
    assert got["SumCostsNPV"] == pytest.approx(sce["SumCostsNPV"]) and got["SumCostsNPV_SCT"] == pytest.approx(sce["SumCostsNPV_SCT"])
    assert got["SumCostsNPV"] == pytest.approx(1000.0 / (1 + discount_settings.get_discount_rate_annual("SCE", 2024)) ** 2)


def test_first_year_costs_are_not_discounted_whatever_the_pa():
    got = cost_tests.program_cost_sums(_program("BAY", 2027), 2027, 2024).iloc[0]
    assert got["SumCostsNPV"] == pytest.approx(1000.0)


def test_a_pa_with_its_own_rows_keeps_them():
    pge = cost_tests.program_cost_sums(_program("PGE", 2029), 2027, 2024).iloc[0]
    assert pge["SumCostsNPV"] == pytest.approx(1000.0 / (1 + discount_settings.get_discount_rate_annual("PGE", 2024)) ** 2)
    old = cost_tests.program_cost_sums(_program("BAY", 2022), 2020, 2020).iloc[0]  # BAY's own 2020 ACC row
    assert old["SumCostsNPV"] == pytest.approx(1000.0 / (1 + discount_settings.get_discount_rate_annual("BAY", 2020)) ** 2)
