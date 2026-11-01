"""A true-up priced against its parent's profile (BRD BR-05.6, CALCULATIONS C21): AC_Elec_Impact_Profile_Parent (and _Parent_2nd_Baseline) price the
record a second time on the parent's profile, and the incremental columns are the difference, bit for bit what pricing the job twice and subtracting gives."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import run_poc
from recet.jobs import RunConfig, run_job
from recet.pipeline import cost_tests, discount_settings
from recet import versions
import reference_calc as ref
from conftest import QUARTER_HOURS, make_electric_acc, make_measure_csv

@pytest.fixture(autouse=True)
def _no_library(monkeypatch):
    from recet.pipeline import real_impact_profiles
    monkeypatch.setattr(real_impact_profiles, "match_profile_id", lambda *a, **k: (None, False))     # no library here: an unknown name matches nothing


PEAK_HOUR = 4500                       # in Q3 of the 2018 calendar
PARENT_COLUMNS = ["AC_Elec_Impact_Profile_Parent", "AC_Elec_Impact_Profile_Parent_2nd_Baseline"]


def _acc(path):
    """$100/MWh every hour, $300/MWh in PEAK_HOUR, $20/MWh of transmission (so the water energy's generation-only price differs)."""
    make_electric_acc(path, transmission=20.0)
    df = pd.read_parquet(path)
    sel = df["hour_of_year"] == PEAK_HOUR
    df.loc[sel, "energy"], df.loc[sel, "ghg_adder"] = 280.0, 0.0
    df["total_avoided_cost"] = df["energy"] + df["ghg_adder"] + df["transmission"]
    df.to_parquet(path, index=False)
    return path


def _profiles(path):
    flat = np.full(8760, 1 / 8760)
    peak = np.zeros(8760)
    peak[PEAK_HOUR - 1] = 1.0
    evening = np.zeros(8760)
    evening[np.arange(18, 8760, 24)] = 1 / 365
    measured = np.full(8760, 900.0 / 8760)          # a metered true-up: 900 kWh per unit per year, its own kWh (BR-06.6)
    measured[PEAK_HOUR - 1] += 100.0
    frames = [pd.DataFrame({"end_use_name": name, "pa": "PGE", "target_sector": "Residential", "climate_zone": "ALL", "hour_of_year": np.arange(1, 8761),
                            "impact": v, "profile_basis": basis, "represents_year": year})
              for name, v, basis, year in (("FLAT", flat, "share", 2018), ("PEAK", peak, "share", 2018), ("EVENING", evening, "share", 2020),
                                           ("MEASURED", measured, "kwh_per_unit", 2018))]
    pd.concat(frames).to_csv(path, index=False)
    return path


# T1 a share profile against a share parent, with water energy on the (blank) water profile; T2 no parent; T3 an early replacement whose parent has its
# own 2nd-baseline profile; T4 a measured kWh-per-unit profile against a share parent; T5 a parent drawn on another calendar, over two years, fuel sub;
# T6 a parent the library does not have.
ROWS = [
    dict(CEInputID="T1", AC_Elec_Impact_Profile="PEAK", AC_Elec_Impact_Profile_Parent="FLAT", UnitkWhIOUWater1stBaseline="50", NumUnits="3"),
    dict(CEInputID="T2", AC_Elec_Impact_Profile="PEAK"),
    dict(CEInputID="T3", AC_Elec_Impact_Profile="FLAT", AC_Elec_Impact_Profile_2nd_Baseline="PEAK", RUL_Yrs="0.5", EUL_Yrs="2",
         UnitkWh2ndBaseline="400", AC_Elec_Impact_Profile_Parent="PEAK", AC_Elec_Impact_Profile_Parent_2nd_Baseline="EVENING"),
    dict(CEInputID="T4", AC_Elec_Impact_Profile="MEASURED", UnitkWh1stBaseline="0", AC_Elec_Impact_Profile_Parent="FLAT", NTGRkWh="0.85"),
    dict(CEInputID="T5", AC_Elec_Impact_Profile="FLAT", AC_Elec_Impact_Profile_Parent="EVENING", EUL_Yrs="2.25", ClaimYearQuarter="2028Q2",
         UnitkWh1stBaseline="-500", FuelID="FuelSub-ToElec", UnitMiscBens="7", UnitTaxCredits="3"),
    dict(CEInputID="T6", AC_Elec_Impact_Profile="FLAT", AC_Elec_Impact_Profile_Parent="NO_SUCH_SHAPE"),
]


def _write(path, rows, parents=True):
    """A Measure.csv of `rows` (each over make_measure_csv's one-measure defaults); parents=False leaves the parent-profile columns out."""
    base = pd.read_csv(make_measure_csv(path), sep="|", dtype=str, keep_default_na=False).iloc[0].to_dict()
    blank = ["AC_Elec_Impact_Profile_2nd_Baseline", "UnitkWhIOUWater1stBaseline", "FuelID", "UnitTaxCredits"] + (PARENT_COLUMNS if parents else [])
    frame = pd.DataFrame([{**base, **{c: "" for c in blank}, **{k: v for k, v in r.items() if parents or k not in PARENT_COLUMNS}} for r in rows])
    frame.to_csv(path, sep="|", index=False)
    return path


def _on_parent(rows):
    """The same records filed with the parent's profiles instead of their own (and no parent columns): the second job of "price both and subtract"."""
    out = []
    for r in rows:
        r = dict(r)
        parent, parent_2nd = r.pop("AC_Elec_Impact_Profile_Parent", ""), r.pop("AC_Elec_Impact_Profile_Parent_2nd_Baseline", "")
        if parent:
            r["AC_Elec_Impact_Profile"], r["AC_Elec_Impact_Profile_2nd_Baseline"] = parent, parent_2nd
        out.append(r)
    return out


def _run(tmp_path, name, path, **cfg):
    acc = tmp_path / "acc.parquet"
    if not acc.exists():
        _acc(acc)
        _profiles(tmp_path / "p.csv")
    return run_job(RunConfig(measures=str(path), out_dir=str(tmp_path / "out"), run_name=name, acc_version="2026", acc_electric_file=str(acc),
                             profiles_8760_file=str(tmp_path / "p.csv"), emissions=False, **cfg), log=lambda s: None)


def _table(r):
    return pd.read_parquet(next((r.run_dir / "_results").glob("measure_level_*.parquet"))) if r.frame is None else r.frame


def _sources():
    return run_poc.INCREMENTAL_STREAMS + ["TotalSystemBenefit", "TRCBen", "PACBen", "RIMBen", "SCBBen", "SCHBen", "PCTBen"]


def test_incremental_columns_are_the_job_priced_twice_and_subtracted_bit_for_bit(tmp_path):
    names = [r["CEInputID"] for r in ROWS]
    with_parent = _table(_run(tmp_path, "true_up", _write(tmp_path / "a.csv", ROWS))).set_index("CEInputID").loc[names]
    filed = _table(_run(tmp_path, "filed", _write(tmp_path / "c.csv", ROWS, parents=False))).set_index("CEInputID").loc[names]
    on_parent = _table(_run(tmp_path, "on_parent", _write(tmp_path / "b.csv", _on_parent(ROWS), parents=False))).set_index("CEInputID").loc[names]

    # nothing else changes: every column of the job without the parent columns is the same, bit for bit
    for c in filed.columns:
        a, b = with_parent[c], filed[c]
        assert ((a == b) | (a.isna() & b.isna())).all(), c
    assert run_poc.incremental_columns(cost_tests.default_set()) == [run_poc.incremental_name(c) for c in _sources()]
    has = with_parent["AC_Elec_Impact_Profile_Parent"].fillna("") != ""
    for c in _sources():
        inc = with_parent[run_poc.incremental_name(c)]
        want = filed[c] - on_parent[c]
        assert (inc[has].to_numpy() == want[has].to_numpy()).all(), c          # ==, not approx: the same arithmetic
        assert inc[~has].isna().all(), c
    assert (with_parent.loc[has, run_poc.PARENT_STATUS] == on_parent.loc[has, "ElecPricingStatus"]).all()
    assert with_parent.loc["T2", run_poc.PARENT_STATUS] == "" and with_parent.loc["T6", run_poc.PARENT_STATUS].startswith("UNPRICED")
    assert with_parent.loc["T1", "ElecBenIncremental"] > 0 and with_parent.loc["T4", "ElecBenIncremental"] != 0
    assert with_parent.loc["T1", "WaterEnergyBenIncremental"] != 0          # the water energy follows the profile when its own column is blank


def test_a_job_in_parts_gives_the_same_incremental_columns(tmp_path):
    whole = _table(_run(tmp_path, "whole", _write(tmp_path / "a.csv", ROWS))).set_index("CEInputID")
    parts = _table(_run(tmp_path, "parts", tmp_path / "a.csv", chunk_size=2)).set_index("CEInputID").loc[whole.index]
    for c in run_poc.incremental_columns() + [run_poc.PARENT_STATUS]:
        a, b = whole[c], parts[c]
        assert ((a == b) | (a.isna() & b.isna())).all(), c


def test_without_the_sct_the_societal_incremental_columns_are_blank(tmp_path):
    t = _table(_run(tmp_path, "trc", _write(tmp_path / "a.csv", ROWS), include_societal=False)).set_index("CEInputID")
    assert t["ElecBenIncremental_SB"].isna().all() and t["SCHBenIncremental"].isna().all()
    assert t.loc["T1", "ElecBenIncremental"] > 0


def test_a_job_without_the_parent_columns_writes_no_incremental_columns(tmp_path):
    t = _table(_run(tmp_path, "plain", make_measure_csv(tmp_path / "m.csv", AC_Elec_Impact_Profile="FLAT")))
    assert not [c for c in t.columns if "Incremental" in c or c == run_poc.PARENT_STATUS]


def test_incremental_electric_benefit_matches_the_reference_loop(tmp_path):
    """reference_calc.incremental_elec_value (C21), independent of the engine, on T1 (share profiles) and T3 (an early replacement, a parent on 2020)."""
    t = _table(_run(tmp_path, "true_up", _write(tmp_path / "a.csv", ROWS))).set_index("CEInputID")
    acc_df = pd.read_parquet(tmp_path / "acc.parquet")
    acc_df = acc_df[(acc_df["iou"] == "PG&E") & (acc_df["cost_test"] == "TRC")]
    acc = {y: g.sort_values("hour_of_year")["total_avoided_cost"].to_numpy() / 1000.0 for y, g in acc_df.groupby("year")}
    p = pd.read_csv(tmp_path / "p.csv")
    shape = {n: g.sort_values("hour_of_year")["impact"].to_numpy() for n, g in p.groupby("end_use_name")}
    rate = discount_settings.get_discount_rate_qtr("PGE", versions.BUILTIN["2026"].acc)
    d1 = dict(Used_Year=2028, Used_Quarter=1, eul=1.0, rul=0.0, ntg_kwh=1.0)
    want = ref.incremental_elec_value(d1, (shape["PEAK"], 2018), (shape["PEAK"], 2018), (shape["FLAT"], 2018), (shape["FLAT"], 2018),
                                      acc, rate, 2028, 3000.0, 0.0, 3000.0, 0.0)
    assert t.loc["T1", "ElecBenIncremental"] == pytest.approx(want, rel=1e-12)
    d3 = dict(Used_Year=2028, Used_Quarter=1, eul=2.0, rul=0.5, ntg_kwh=1.0)
    want3 = ref.incremental_elec_value(d3, (shape["FLAT"], 2018), (shape["PEAK"], 2018), (shape["PEAK"], 2018), (shape["EVENING"], 2020),
                                       acc, rate, 2028, 1000.0, 400.0, 1000.0, 400.0)
    assert t.loc["T3", "ElecBenIncremental"] == pytest.approx(want3, rel=1e-12)
    # the single-shape case by hand: 3,000 kWh all in the $300 hour of Q3 against the flat shape at $120/MWh with that hour at $300
    r = 1 + rate
    flat = sum(3000 * (QUARTER_HOURS[q] * 0.120 + (0.180 if q == 2 else 0.0)) / 8760 / r ** q for q in range(4))
    assert t.loc["T1", "ElecBenIncremental"] == pytest.approx(3000 * 0.300 / r ** 2 - flat, rel=1e-12)


def test_preflight_counts_true_ups_and_warns_on_a_parent_it_cannot_price(tmp_path):
    import json
    rows = ROWS + [dict(CEInputID="T7", AC_Elec_Impact_Profile="FLAT", AC_Elec_Impact_Profile_Parent_2nd_Baseline="PEAK")]
    r = _run(tmp_path, "check", _write(tmp_path / "a.csv", rows))
    report = json.loads((r.run_dir / "preflight.json").read_text(encoding="utf-8"))
    found = {i["code"]: i for i in report["issues"]}
    assert found["no_parent_profile"]["severity"] == "warning" and found["no_parent_profile"]["count"] == 1
    assert found["no_parent_profile"]["cells"] == [["Measure", 5, "AC_Elec_Impact_Profile_Parent"]]
    assert found["parent_2nd_without_parent"]["cells"] == [["Measure", 6, "AC_Elec_Impact_Profile_Parent_2nd_Baseline"]]
    assert report["stats"]["true-ups priced against a parent profile"] == "5"
    t = _table(r).set_index("CEInputID")
    assert np.isnan(t.loc["T7", "ElecBenIncremental"]) and t.loc["T7", run_poc.PARENT_STATUS] == ""
