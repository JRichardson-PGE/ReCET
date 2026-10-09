"""The Results tab's tables (recet/results_view.py): PA filter, the five levels, recomputed ratios, the column catalog and saved views. No window needed."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from recet import outputs, results_view as rv
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def _frame() -> pd.DataFrame:
    """Four measures, two programs, two PAs. Program P1 holds one cheap and one expensive measure so an averaged ratio would differ from the recomputed one."""
    rows = [
        dict(CEInputID="M1", MeasDescription="a", PrgID="P1", CreditedIOU="PGE", Prg_PA="PGE", IOU_AC_Territory="PGE", Prg_PrimarySector="Residential",
             Prg_ProgramSegment="resource_acquisition", Prg_ProgramName="One", ElecBen=100.0, GasBen=0.0, TRCCost=50.0),
        dict(CEInputID="M2", MeasDescription="b", PrgID="P1", CreditedIOU="PGE", Prg_PA="PGE", IOU_AC_Territory="PGE", Prg_PrimarySector="Residential",
             Prg_ProgramSegment="resource_acquisition", Prg_ProgramName="One", ElecBen=10.0, GasBen=0.0, TRCCost=100.0),
        dict(CEInputID="M3", MeasDescription="c", PrgID="P2", CreditedIOU="", Prg_PA="BAY", IOU_AC_Territory="PGE", Prg_PrimarySector="Commercial",
             Prg_ProgramSegment="", Prg_ProgramName="Two", ElecBen=30.0, GasBen=10.0, TRCCost=20.0),
        dict(CEInputID="M4", MeasDescription="d", PrgID="P3", CreditedIOU="", Prg_PA="", IOU_AC_Territory="SCE", Prg_PrimarySector="Commercial",
             Prg_ProgramSegment="market_support", Prg_ProgramName="Three", ElecBen=5.0, GasBen=5.0, TRCCost=10.0),
    ]
    f = pd.DataFrame(rows)
    for c in ("WaterEnergyBen", "OtherBen", "TaxCredits", "BillIncrElec", "BillIncrGas"):
        f[c] = 0.0
    f["PACCost"] = f["TRCCost"] / 2
    f["RIMCost"] = f["TRCCost"] * 3
    f["TotalExpenditures"] = [40.0, 60.0, 15.0, 5.0]
    f["TotalSystemBenefit"] = f["ElecBen"] + f["GasBen"]
    f["FirstYearNetKWh"] = [1000.0, 2000.0, 500.0, 100.0]
    f["NetElecCO2Lifecycle"] = [1.0, 2.0, 3.0, 4.0]
    f["NetGasCO2Lifecycle"] = [0.5, 0.0, 0.5, 0.0]
    f["TRCRatio"] = (f["ElecBen"] + f["GasBen"]) / f["TRCCost"]   # a measure-level ratio column: never summed
    f["NumUnits"] = 7.0                                    # an input: never offered
    return f


def test_pa_is_the_program_administrator_with_fallbacks():
    assert rv.pa_of(_frame()).tolist() == ["PGE", "PGE", "BAY", "SCE"]
    assert rv.available_pas(_frame()) == [rv.ALL_PAS, "BAY", "PGE", "SCE"]


def test_ratios_are_recomputed_from_sums_not_averaged():
    f = _frame()
    prog = rv.table(f, "program", rv.ALL_PAS, ["TRC_ratio"]).set_index("PrgID")
    assert prog.loc["P1", "TRC_ratio"] == pytest.approx(110 / 150)                       # an average of 2.0 and 0.1 would be 1.05
    port = rv.table(f, "portfolio", rv.ALL_PAS, ["TRC_ratio", "PAC_ratio", "RIM_ratio"]).iloc[0]
    want = outputs.group_ratios(f)
    for k in ("TRC_ratio", "PAC_ratio", "RIM_ratio"):
        assert port[k] == pytest.approx(want[k])
    assert port["TRC_ratio"] == pytest.approx(outputs.summarize(f).iloc[0]["TRC_ratio"])    # same as the portfolio summary file


def test_measure_level_ratios_equal_each_measures_own():
    t = rv.table(_frame(), "measure", rv.ALL_PAS, ["TRC_ratio", "TotalExpenditures"])
    assert t["TRC_ratio"].tolist() == pytest.approx(_frame()["TRCRatio"].tolist())
    assert list(t.columns[:4]) == ["CEInputID", "Description", "PrgID", "PA"]


def test_levels_add_up_and_filter_by_pa():
    f = _frame()
    for level in ("program", "sector", "segment"):
        t = rv.table(f, level, rv.ALL_PAS, ["TotalExpenditures", "FirstYearNetKWh"])
        assert t["TotalExpenditures"].sum() == pytest.approx(120.0) and t["Measures"].sum() == 4
    seg = rv.table(f, "segment", rv.ALL_PAS, ["TotalExpenditures"]).set_index("Program segment")
    assert seg.loc["(blank)", "TotalExpenditures"] == 15.0
    pge = rv.table(f, "portfolio", "PGE", ["TotalExpenditures", "TotalCO2Lifecycle"]).iloc[0]
    assert pge["Measures"] == 2 and pge["TotalExpenditures"] == 100.0 and pge["TotalCO2Lifecycle"] == pytest.approx(3.5)


def test_catalog_offers_outputs_and_ratios_but_not_inputs_or_measure_ratios():
    cat = rv.catalog(_frame())
    assert list(cat)[:5] == ["TotalExpenditures", "TotalSystemBenefit", "TRC_ratio", "PAC_ratio", "RIM_ratio"]
    assert "NumUnits" not in cat and "TRCRatio" not in cat and "Prg_PA" not in cat
    assert cat["ElecBen"].kind == "sum" and cat["TRC_ratio"].kind == "ratio" and cat["TotalCO2Lifecycle"].kind == "derived"
    t = rv.table(_frame(), "portfolio", rv.ALL_PAS, ["NotAColumn", "ElecBen"])
    assert "NotAColumn" not in t.columns and t.iloc[0]["ElecBen"] == 145.0


def test_measure_level_is_turned_off_for_big_runs(monkeypatch):
    monkeypatch.setattr(rv, "MAX_MEASURE_ROWS", 3)
    assert not rv.measure_level_allowed(_frame(), rv.ALL_PAS)
    assert rv.measure_level_allowed(_frame(), "PGE")                 # the filter brings it under the limit


def test_views_are_saved_and_the_default_cannot_be_changed():
    views, current = rv.load_views()
    assert views == {rv.DEFAULT_VIEW_NAME: rv.DEFAULT_VIEW} and current == rv.DEFAULT_VIEW_NAME
    views["Costs"] = ["TRCCost", "PACCost"]
    views[rv.DEFAULT_VIEW_NAME] = ["ElecBen"]
    rv.save_views(views, "Costs")
    views, current = rv.load_views()
    assert views["Costs"] == ["TRCCost", "PACCost"] and current == "Costs" and views[rv.DEFAULT_VIEW_NAME] == rv.DEFAULT_VIEW


def test_formatting():
    cat = rv.catalog(_frame())
    assert rv.format_value(cat["TotalExpenditures"], 1234567.4) == "$1,234,567"
    assert rv.format_value(cat["TotalExpenditures"], -12.6) == "-$13"
    assert rv.format_value(cat["TRC_ratio"], 1.234) == "1.23"
    assert rv.format_value(cat["TRC_ratio"], math.nan) == ""
    assert rv.format_value(cat["TotalCO2Lifecycle"], 1234.56) == "1,234.6"


def test_every_level_works_on_a_real_run(tmp_path):
    from recet.jobs import RunConfig, run_job
    cfg = RunConfig(measures=str(make_measure_csv(tmp_path / "Measure.csv")), out_dir=str(tmp_path / "out"), run_name="r", acc_version="2026",
                    acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet")), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")))
    frame = run_job(cfg, log=lambda s: None).frame
    cat = rv.catalog(frame)
    assert all(k in cat for k in rv.DEFAULT_VIEW)                   # every default column exists in a real run
    for level, _ in rv.LEVELS:
        t = rv.table(frame, level, rv.ALL_PAS, rv.DEFAULT_VIEW)
        assert len(t) >= 1 and "TotalSystemBenefit" in t.columns
    port = rv.table(frame, "portfolio", rv.ALL_PAS, ["TotalSystemBenefit", "FirstYearNetKWh"]).iloc[0]
    assert port["TotalSystemBenefit"] == pytest.approx(frame["TotalSystemBenefit"].sum())
    assert port["FirstYearNetKWh"] == pytest.approx(1000.0)            # 1 unit x 1,000 kWh x NTG 1, one year
