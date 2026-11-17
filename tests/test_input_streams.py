"""Input streams priced from the cost-test set (pipeline/input_streams.py, COST_TESTS.md step 4): EE-2026's OtherBen / OtherCost / TaxCredits equal the
engine's bit for bit, the annual_over_life basis equals a year-by-year loop, and a set with a non-energy benefit (an annual health NEB from a Measure column
CET does not have) and a jurisdiction-specific test that counts it runs end to end: loader, template, column check, measure-level table, summary, parts."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import cost_test_sets as cts, outputs, templates
from recet.jobs import run_job
from recet.pipeline import data_loading, discount_settings, input_streams
from recet.pipeline.engine import price_other_ben_cost, rul_in_use
from test_chunked import _cfg as _job_cfg, _job
from test_cost_test_sets import _data, _write

QUIET = dict(log=lambda s: None)
NEB = "UnitNEBHealth"


def _measures(n=60, seed=3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    eul = rng.choice([0.0, 0.5, 1.0, 3.0, 7.25, 12.0, 20.0], n)
    rul = np.where(rng.random(n) < 0.4, np.minimum(eul, rng.choice([0.0, 0.5, 2.0, 4.0, 30.0], n)), 0.0)
    return pd.DataFrame({
        "CEInputID": [f"m{i}" for i in range(n)], "IOU_AC_Territory": rng.choice(["PGE", "SCE", "SDGE", "SCG"], n),
        "NumUnits": rng.uniform(0, 50, n), "NTGRkWh": rng.uniform(0.3, 1.0, n), "NTGRTherm": rng.uniform(0.3, 1.0, n),
        "EUL_Yrs": eul, "RUL_Yrs": rul, "Used_Year": rng.choice([2026, 2027, 2028], n), "Used_Quarter": rng.integers(1, 5, n),
        **{c: rng.uniform(-5, 40, n) for c in ("UnitGasInfraBens", "UnitRefrigBens", "UnitMiscBens", "UnitRefrigCosts", "UnitMiscCosts", "UnitTaxCredits")},
        NEB: rng.uniform(0, 30, n), "UnitNEBHealth2nd": rng.uniform(0, 10, n)})


def _neb_data(**stream) -> dict:
    d = _data()
    d.update(id="EE-2026-neb", label="EE 2026 with a health NEB and a JST")
    d["streams"]["HealthNEB"] = {"source": "input", "kind": "benefit", "columns": [NEB], "basis": "annual_over_life", "ntg": "kwh",
                                 "market_effects": "benefits", "pricings": ["primary", "sct_base"], "naming": "suffix", "gross": "divide_ntg",
                                 "unit": "$ (PV)", "definition": "A health benefit per unit per year over the measure's life (synthetic)."} | stream
    d["tests"]["JST"] = {"label": "Jurisdiction-specific test", "pricing": "primary", "benefits": ["ElecBen", "GasBen", "HealthNEB"],
                         "costs": ["RebatesandIncentsPV"], "program_costs": {"share": "TRC"}, "variants": ["NoAdmin"]}
    return d


def _neb_set(tmp_path, **stream):
    return _write(tmp_path, _neb_data(**stream), "neb.yaml")


@pytest.mark.parametrize("meb, mec", [(0.0, 0.0), (0.05, 0.05), (0.05, 0.0)])
def test_ee_input_streams_are_the_engines_bit_for_bit(meb, mec):
    m = _measures()
    got = input_streams.price(m, cts.load("EE-2026"), 2026, 2026, meb, mec)
    want = price_other_ben_cost(m, meb, mec)
    assert set(got.columns) == set(want.columns)
    for c in want.columns:
        assert (got[c].to_numpy() == want[c].to_numpy()).all(), c


def _loop(m: pd.Series, a1: float, a2: float, ra: float, rq: float, base_year: int) -> float:
    """Year by year: whole years of the life at a1 (a2 after the RUL), year t discounted by (1 + ra)^-t, brought back to the First Year."""
    eul, rul = float(m["EUL_Yrs"]), float(rul_in_use(m["RUL_Yrs"], m["EUL_Yrs"]))
    total = sum((a1 if (rul == 0 or t < rul) else a2) / (1 + ra) ** t for t in range(int(eul)))
    return total / (1 + rq) ** ((m["Used_Year"] - base_year) * 4 + m["Used_Quarter"] - 1)


def test_annual_over_life_equals_a_year_by_year_loop(tmp_path):
    m = _measures(80, seed=11)
    m["EUL_Yrs"] = np.floor(m["EUL_Yrs"])                     # the loop counts whole years; fractional lives follow A(n) (next test)
    m["RUL_Yrs"] = np.floor(m["RUL_Yrs"])
    s = cts.read_file(_write(tmp_path, _neb_data(columns_2nd=["UnitNEBHealth2nd"])))
    got = input_streams.price(m, s, 2026, 2026, 0.05, 0.0)
    for (_, r), v, v_sb, g in zip(m.iterrows(), got["HealthNEB"], got["HealthNEB_SB"], got["HealthNEBGross"]):
        st = discount_settings.get_settings(r["IOU_AC_Territory"], 2026)
        life = _loop(r, r[NEB], r["UnitNEBHealth2nd"], st["DiscountRateAnnual"], st["DiscountRateQtr"], 2026)
        life_sb = _loop(r, r[NEB], r["UnitNEBHealth2nd"], st["DiscountRateAnnual_SCT"], st["DiscountRateQtr_SCT"], 2026)
        assert v == pytest.approx(r["NumUnits"] * (r["NTGRkWh"] + 0.05) * life, rel=1e-12, abs=1e-9)
        assert v_sb == pytest.approx(r["NumUnits"] * (r["NTGRkWh"] + 0.05) * life_sb, rel=1e-12, abs=1e-9)
        assert g == pytest.approx(r["NumUnits"] * life, rel=1e-12, abs=1e-9)


def test_a_fractional_life_and_a_zero_rate():
    a = input_streams._annual_sum(np.array([0.0, 0.5, 1.0, 2.0, 2.5, -1.0]), np.array([0.1, 0.1, 0.1, 0.1, 0.0, 0.1]))
    np.testing.assert_allclose(a, [0.0, 1 + (1 - 1.1 ** 0.5) / 0.1, 1.0, 1 + 1 / 1.1, 2.5, 0.0], rtol=1e-15)


def test_the_set_validates_its_input_streams():
    bad = _neb_data(basis="one_time", columns_2nd=["X"])
    assert any("columns_2nd: only for basis annual_over_life" in e for e in cts.validate(bad))
    bad = _neb_data(naming="none")                            # primary and sct_base discount at different rates
    assert any("needs its own column per pricing" in e for e in cts.validate(bad))
    bad = _neb_data(columns=["not a name"])
    assert any("is not a column name" in e for e in cts.validate(bad))
    assert cts.validate(_neb_data()) == []


def _neb_job(tmp_path, values=None):
    """test_chunked's 40-measure job with a UnitNEBHealth column (values per row; default 2 + i % 5, every tenth blank)."""
    m, pc = _job(tmp_path)
    lines = m.read_text(encoding="utf-8").splitlines()
    vals = values or [("" if i % 10 == 3 else str(2 + i % 5)) for i in range(len(lines) - 1)]
    m.write_text("\n".join([lines[0] + f"|{NEB}"] + [f"{line}|{v}" for line, v in zip(lines[1:], vals)]) + "\n", encoding="utf-8")
    return m, pc


def test_a_neb_set_runs_end_to_end(tmp_path):
    m, pc = _neb_job(tmp_path)
    r = run_job(_job_cfg(tmp_path, m, pc, cost_test_set=str(_neb_set(tmp_path)), output_format="csv", chunk_size=0), **QUIET)
    t = outputs.read_table(r.files["measure_level"])
    loaded = data_loading.load_measures_from_csv(m, extra_columns=[NEB])
    assert loaded[NEB].tolist() == [0.0 if i % 10 == 3 else 2.0 + i % 5 for i in range(40)]          # blank = 0
    want = input_streams.price(loaded, r.cost_tests.set, 2026, int(loaded["Used_Year"].min()))
    np.testing.assert_allclose(t["HealthNEB"], want["HealthNEB"], rtol=1e-12)
    assert (t["HealthNEB"] > 0).sum() > 20 and NEB in t.columns                                     # echoed input, priced stream
    np.testing.assert_allclose(t["JSTBen"], t["ElecBen"] + t["GasBen"] + t["HealthNEB"], rtol=1e-12)
    summ = outputs.read_table(r.files["portfolio_summary"]).iloc[0]
    assert summ["JST_ratio"] == pytest.approx(t["JSTBen"].sum() / t["JSTCost"].sum(), rel=1e-12)
    assert summ["TRC_ratio"] == pytest.approx(t["TRCBen"].sum() / t["TRCCost"].sum(), rel=1e-12)   # TRC does not count the NEB
    np.testing.assert_allclose(t["TRCBen"], t["ElecBen"] + t["GasBen"] + t["WaterEnergyBen"] + t["OtherBen"] + t["TaxCredits"], rtol=1e-12)
    fd = outputs.read_table(r.files["field_definitions"]).set_index("column")
    assert fd.loc["HealthNEB", "definition"].startswith("A health benefit per unit per year") and fd.loc["HealthNEB", "cost_tests"] == "benefit in JST"
    assert "cost-test stream HealthNEB" in fd.loc[NEB, "definition"]
    ee = run_job(_job_cfg(tmp_path, m, pc, run_name="ee", output_format="csv", chunk_size=0), **QUIET)
    e = outputs.read_table(ee.files["measure_level"])
    np.testing.assert_allclose(t["TRCCost"], e["TRCCost"], rtol=1e-12)                              # the TRC share is the EE run's
    assert "HealthNEB" not in e.columns


def test_a_neb_set_in_parts_equals_the_whole_job(tmp_path):
    m, pc = _neb_job(tmp_path)
    f = _neb_set(tmp_path)
    whole = run_job(_job_cfg(tmp_path, m, pc, cost_test_set=str(f), run_name="whole", output_format="csv", chunk_size=0), **QUIET)
    parts = run_job(_job_cfg(tmp_path, m, pc, cost_test_set=str(f), run_name="parts", output_format="csv", chunk_size=7), **QUIET)
    a, b = (outputs.read_table(x.files["measure_level"]) for x in (whole, parts))
    for c in ("HealthNEB", "HealthNEB_SB", "HealthNEBGross", "JSTBen", "JSTCost", "JSTRatio"):
        np.testing.assert_allclose(b[c].astype(float), a[c].astype(float), rtol=1e-12, atol=1e-12, err_msg=c)


def test_a_missing_column_is_zero_with_a_note_and_text_stops_the_run(tmp_path):
    m, pc = _job(tmp_path)                                    # no UnitNEBHealth column
    r = run_job(_job_cfg(tmp_path, m, pc, cost_test_set=str(_neb_set(tmp_path)), output_format="csv", chunk_size=0), **QUIET)
    assert any(f"Measure column {NEB}, which the file does not have" in n for n in r.notes)
    assert (outputs.read_table(r.files["measure_level"])["HealthNEB"] == 0).all()
    (tmp_path / "bad").mkdir()
    m2, pc2 = _neb_job(tmp_path / "bad", values=["3"] * 39 + ["lots"])
    with pytest.raises(ValueError, match=f"{NEB}: 1 value.*'lots'"):
        run_job(_job_cfg(tmp_path / "bad", m2, pc2, cost_test_set=str(_neb_set(tmp_path)), output_format="csv", chunk_size=7), **QUIET)


def test_the_template_carries_the_sets_columns(tmp_path):
    f = _neb_set(tmp_path)
    head = templates.write_template("measure", tmp_path / "m.csv", cost_tests=str(f)).read_text(encoding="utf-8").strip().split("|")
    assert head == templates.MEASURE_COLUMNS + [NEB]
    assert templates.write_template("measure", tmp_path / "ee.csv", cost_tests="EE-2026").read_text(encoding="utf-8").strip().split("|") == templates.MEASURE_COLUMNS


def test_statewide_allocation_weights_a_sets_columns(tmp_path):
    from recet.pipeline import statewide_allocation as sw
    f = sw.load_factors().reset_index()
    year, prg = int(f.iloc[1]["Year"]), str(f.iloc[1]["ProgramID"])
    m, _pc = _neb_job(tmp_path)
    lines = m.read_text(encoding="utf-8").splitlines()
    cols = lines[0].split("|")
    row = lines[1].split("|")
    row[cols.index("PrgID")], row[cols.index("ClaimYearQuarter")], row[cols.index(NEB)] = prg, f"{year}Q1", "10"
    m.write_text("\n".join([lines[0], "|".join(row)]) + "\n", encoding="utf-8")
    d = data_loading.load_measures_from_csv(m, allocate_statewide=True, extra_columns=[NEB])
    factors = sw.load_factors().loc[(year, prg)]
    want = {iou: 10 * float(factors[f"{iou}WeightedFactor"]) for iou in sw.IOUS}
    got = dict(zip(d["CEInputID"].str.split("_").str[0], d[NEB]))
    assert len(d) > 1 and all(got[i] == pytest.approx(want[i], rel=1e-12) for i in got)
    assert d[NEB].sum() == pytest.approx(10 * sum(float(factors[f"{i}WeightedFactor"]) for i in got), rel=1e-12)
