"""The data-dictionary attributes of the field_definitions table (BRD BR-08.1): cost-test treatment checked against the cost tests' own arithmetic."""
from __future__ import annotations

import numpy as np
import pandas as pd

from recet import field_attributes as fa, outputs
from recet.jobs import run_job
from test_chunked import _cfg, _job

QUIET = dict(log=lambda s: None)


def _members(table: dict, test: str) -> list[str]:
    return [c for c, tests in table.items() if test in tests.split(", ")]


def test_cost_test_treatment_adds_up_to_each_tests_benefits_and_costs(tmp_path):
    m, pc = _job(tmp_path)
    r = run_job(_cfg(tmp_path, m, pc, chunk_size=0), **QUIET)
    d = outputs.read_table(r.files["measure_level"])
    benefit_of, cost_of = fa.benefit_of(), fa.cost_of()
    for test in ("TRC", "PAC", "RIM", "SCB", "SCH"):
        ben = sum(d[c] for c in _members(benefit_of, test))
        cost = sum(d[c] for c in _members(cost_of, test))
        np.testing.assert_allclose(ben, d[f"{test}Ben"], rtol=1e-12, atol=1e-9, err_msg=test)
        np.testing.assert_allclose(cost, d[f"{test}CostNoAdmin"], rtol=1e-12, atol=1e-9, err_msg=test)
    np.testing.assert_allclose(sum(d[c] for c in _members(cost_of, "PCT")), d["PCTCost"], rtol=1e-12, atol=1e-9)   # PCT has no program costs
    assert "TaxCreditsGrossPV" in _members(benefit_of, "PCT")                      # internal: priced for PCTBen, never written


def test_every_column_of_a_run_gets_its_attributes(tmp_path):
    m, pc = _job(tmp_path)
    r = run_job(_cfg(tmp_path, m, pc, chunk_size=0), **QUIET)
    t = outputs.read_table(r.files["field_definitions"]).set_index("column")
    assert list(t.columns[-len(fa.ATTRIBUTE_COLUMNS):]) == fa.ATTRIBUTE_COLUMNS
    assert t["source"].astype(str).str.len().gt(0).all() and t["aggregation"].astype(str).str.len().gt(0).all()
    assert t.loc["TRCRatio", "aggregation"].startswith("recomputed") and t.loc["TRCRatio", "cost_tests"] == "TRC result"
    assert t.loc["ElecBen", "unit"].startswith("Dollars (PV") and t.loc["ElecBen", "version"].endswith("2026 ACC")
    assert t.loc["ElecBen", "cost_tests"] == "benefit in TRC, PAC, RIM" and t.loc["ElecBen", "aggregation"] == "summed over the measures"
    assert t.loc["RebatesandIncentsPV", "cost_tests"] == "benefit in PCT; cost in TRC, PAC, RIM, PCT"
    assert t.loc["NumUnits", "source"] == "input: Measure.csv" and t.loc["NumUnits", "version"] == "as filed (CET input field)"
    assert t.loc["ElecBen_SB", "years_in_use"].startswith("2024 ACC on")
    assert "BillReducElec" in t.loc["LifecycleBillReducElec", "years_in_use"]
    assert t.loc["WtdEMV", "time_basis"] == "nominal dollars, not discounted"
    assert t.loc["FirstYearNetKWh", "time_basis"] == "first year of the measure's life"
    assert t.loc["CreditedPA", "aggregation"].startswith("not combined")
    assert set(t["rounding"]) == {"not rounded (full precision as calculated)"}


def test_unit_is_read_from_a_one_column_or_a_many_column_entry():
    assert fa.unit("Lifecycle net CO2. Unit: Metric Tons.", "LifecycleNetElecCO2") == "Metric Tons"
    assert fa.unit("Savings. Units: AnnualGrossKWh kWh; AnnualGrossKW kW; AnnualGrossThm Therm.", "AnnualGrossKW") == "kW"
    assert fa.unit("No unit here.", "X") == ""
