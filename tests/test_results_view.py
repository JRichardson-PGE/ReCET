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
        dict(CEInputID="M1", MeasDescription="a", PrgID="P1", CreditedPA="PGE", Prg_PA="PGE", IOU_AC_Territory="PGE", Prg_PrimarySector="Residential",
             Prg_ProgramSegment="resource_acquisition", Prg_ProgramName="One", ElecBen=100.0, GasBen=0.0, TRCCost=50.0),
        dict(CEInputID="M2", MeasDescription="b", PrgID="P1", CreditedPA="PGE", Prg_PA="PGE", IOU_AC_Territory="PGE", Prg_PrimarySector="Residential",
             Prg_ProgramSegment="resource_acquisition", Prg_ProgramName="One", ElecBen=10.0, GasBen=0.0, TRCCost=100.0),
        dict(CEInputID="M3", MeasDescription="c", PrgID="P2", CreditedPA="", Prg_PA="BAY", IOU_AC_Territory="PGE", Prg_PrimarySector="Commercial",
             Prg_ProgramSegment="", Prg_ProgramName="Two", ElecBen=30.0, GasBen=10.0, TRCCost=20.0),
        dict(CEInputID="M4", MeasDescription="d", PrgID="P3", CreditedPA="", Prg_PA="", IOU_AC_Territory="SCE", Prg_PrimarySector="Commercial",
             Prg_ProgramSegment="market_support", Prg_ProgramName="Three", ElecBen=5.0, GasBen=5.0, TRCCost=10.0),
    ]
    f = pd.DataFrame(rows)
    for c in ("WaterEnergyBen", "OtherBen", "TaxCredits", "LifecycleBillIncrElec", "LifecycleBillIncrGas"):
        f[c] = 0.0
    f["PACCost"] = f["TRCCost"] / 2
    f["RIMCost"] = f["TRCCost"] * 3
    f["TotalExpenditures"] = [40.0, 60.0, 15.0, 5.0]
    f["TotalSystemBenefit"] = f["ElecBen"] + f["GasBen"]
    f["FirstYearNetKWh"] = [1000.0, 2000.0, 500.0, 100.0]
    f["LifecycleNetElecCO2"] = [1.0, 2.0, 3.0, 4.0]
    f["LifecycleNetGasCO2"] = [0.5, 0.0, 0.5, 0.0]
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


def _big(n: int = 120) -> pd.DataFrame:
    """The four measures repeated, with new ids: enough rows for several read batches."""
    f = pd.concat([_frame()] * (n // 4), ignore_index=True)
    f["CEInputID"] = [f"M{i}" for i in range(len(f))]
    return f


@pytest.mark.parametrize("suffix", ["parquet", "csv"])
def test_a_results_file_gives_the_tables_its_frame_gives(tmp_path, monkeypatch, suffix):
    """RunResults.from_file reads the file in batches of only the needed columns; every level, PA and view equals the in-memory frame's."""
    monkeypatch.setattr(rv, "BATCH_ROWS", 7)
    f = _big()
    path = tmp_path / f"measure_level_x.{suffix}"
    f.to_parquet(path, index=False) if suffix == "parquet" else f.to_csv(path, index=False)
    res, ref = rv.RunResults.from_file(path), rv.RunResults.from_frame(outputs.read_table(path))
    keys = ["TotalExpenditures", "TRC_ratio", "PAC_ratio", "TotalCO2Lifecycle", "ElecBen", "FirstYearNetKWh"]
    assert res.pas() == ref.pas() == [rv.ALL_PAS, "BAY", "PGE", "SCE"] and res.n == len(f)
    assert list(res.catalog()) == list(rv.catalog(f))
    for pa in res.pas():
        for level, _ in rv.LEVELS:
            pd.testing.assert_frame_equal(res.table(level, pa, keys), ref.table(level, pa, keys), check_dtype=False, rtol=1e-12)
    for level in ("program", "sector", "segment", "portfolio"):     # and the frame-based function the desktop tab used before
        pd.testing.assert_frame_equal(res.table(level, rv.ALL_PAS, keys), rv.table(f, level, rv.ALL_PAS, keys), check_dtype=False, rtol=1e-12)


def test_group_sums_are_kept_beside_the_results_and_rebuilt_when_stale(tmp_path):
    import os, time
    path = tmp_path / "measure_level_x.parquet"
    _big().to_parquet(path, index=False)
    rv.RunResults.from_file(path)
    kept = tmp_path / outputs.RESULTS_DIR / f"group_sums{rv.CUBE_VERSION}_measure_level_x.parquet.parquet"
    assert kept.exists() and len(pd.read_parquet(kept)) < 20                # a few group rows, not 120 measures
    f = _big()
    f["TotalExpenditures"] = 1.0
    f.to_parquet(path, index=False)
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert rv.RunResults.from_file(path).table("portfolio", rv.ALL_PAS, ["TotalExpenditures"]).iloc[0]["TotalExpenditures"] == 120.0
    csv_run = tmp_path / outputs.RESULTS_DIR / "measure_level_y.parquet"   # a CSV run's kept parquet: its group sums go in the same folder
    _big().to_parquet(csv_run, index=False)
    rv.RunResults.from_file(csv_run)
    assert (tmp_path / outputs.RESULTS_DIR / f"group_sums{rv.CUBE_VERSION}_measure_level_y.parquet.parquet").exists()


def test_no_measure_level_for_a_run_too_large_to_show(tmp_path, monkeypatch):
    path = tmp_path / "measure_level_x.parquet"
    _big().to_parquet(path, index=False)
    res = rv.RunResults.from_file(path)
    monkeypatch.setattr(rv, "MAX_RUN_MEASURE_ROWS", 100)
    assert not res.measure_level_available() and not res.measure_level_allowed("BAY")      # even a PA with 30 measures
    with pytest.raises(ValueError):
        res.table("measure", "BAY", ["ElecBen"])
    assert res.table("program", "BAY", ["ElecBen"])["Measures"].sum() == 30                 # the other levels are unaffected
    monkeypatch.setattr(rv, "MAX_RUN_MEASURE_ROWS", 1000)
    monkeypatch.setattr(rv, "MAX_MEASURE_ROWS", 40)
    assert res.measure_level_available() and not res.measure_level_allowed(rv.ALL_PAS) and res.measure_level_allowed("BAY")
    t = res.table("measure", "BAY", ["ElecBen", "TRC_ratio"])
    assert len(t) == 30 and set(t["PA"]) == {"BAY"} and t["TRC_ratio"].tolist() == pytest.approx([2.0] * 30)


def test_kept_group_sums_are_never_taken_for_the_results_file(tmp_path):
    """results_source looks for measure_level_* files, also in RESULTS_DIR: the kept sums must not be one of them, for a parquet or a CSV run."""
    run = tmp_path / "parquet_run"
    run.mkdir()
    _big().to_parquet(run / "measure_level_x.parquet", index=False)
    rv.RunResults.from_file(outputs.results_source(run))
    assert outputs.results_source(run) == run / "measure_level_x.parquet"
    csv_run = tmp_path / "csv_run"
    (csv_run / outputs.RESULTS_DIR).mkdir(parents=True)
    _big().to_csv(csv_run / "measure_level_x.csv", index=False)
    kept = csv_run / outputs.RESULTS_DIR / "measure_level_x.csv.parquet"
    _big().to_parquet(kept, index=False)
    rv.RunResults.from_file(outputs.results_source(csv_run))
    assert outputs.results_source(csv_run) == kept and len(list((csv_run / outputs.RESULTS_DIR).glob("group_sums*"))) == 1


def test_a_run_keeps_its_group_sums(tmp_path):
    """keep_group_sums from the frame in memory gives the same sums the file gives, and the Results page then uses them."""
    run = tmp_path / "run"
    run.mkdir()
    f = _big()
    f.to_parquet(run / "measure_level_x.parquet", index=False)
    rv.keep_group_sums(run, f)
    kept = run / outputs.RESULTS_DIR / f"group_sums{rv.CUBE_VERSION}_measure_level_x.parquet.parquet"
    pd.testing.assert_frame_equal(pd.read_parquet(kept), rv.build_group_sums(run / "measure_level_x.parquet"), check_dtype=False)


def test_program_years_level_and_filter():
    """BRD BR-04.1: a program-year level, and every level totalled over the years picked (the summary file has a program year group too)."""
    f = _frame()
    f["Used_Year"] = [2028, 2029, 2029, 2030]
    res = rv.RunResults.from_frame(f)
    assert res.years() == ["2028", "2029", "2030"]
    by_year = res.table("year", rv.ALL_PAS, ["TotalExpenditures", "TRC_ratio"]).set_index("Program year")
    assert by_year["TotalExpenditures"].to_dict() == {"2028": 40.0, "2029": 75.0, "2030": 5.0}
    assert by_year.loc["2029", "TRC_ratio"] == pytest.approx(50 / 120)
    two = res.table("portfolio", rv.ALL_PAS, ["TotalExpenditures", "TRC_ratio"], years=["2029", "2030"]).iloc[0]
    assert two["Measures"] == 3 and two["TotalExpenditures"] == 80.0 and two["Program years"] == "2029, 2030"
    assert two["TRC_ratio"] == pytest.approx((10 + 40 + 10) / (100 + 20 + 10))
    prog = res.table("program", "PGE", ["TotalExpenditures"], years=["2029"]).set_index("PrgID")
    assert prog["TotalExpenditures"].to_dict() == {"P1": 60.0}                  # P2 (2029) is BAY's
    assert res.table("measure", rv.ALL_PAS, ["TotalExpenditures"], years=["2028"])["CEInputID"].tolist() == ["M1"]
    assert res.count(rv.ALL_PAS, ["2030"]) == 1 and rv.parse_years(["2029,2028", "x", "2028"]) == ["2028", "2029"]
    summ = outputs.summarize(f)
    years = summ[summ["group"] == "program year"].set_index("value")
    assert years["measures"].to_dict() == {"2028": 1, "2029": 2, "2030": 1} and years.loc["2029", "TRC_ratio"] == pytest.approx(50 / 120)
    parts = outputs.SummaryParts()
    parts.add(f.iloc[:2])
    parts.add(f.iloc[2:])
    pd.testing.assert_frame_equal(parts.result(), summ, check_dtype=False)
