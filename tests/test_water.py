"""Water energy: CalcCE's WaterEnergyBen / WaterEnergyCost, checked against hand-computed values on a one-measure job with a synthetic ACC (Gen $100/MWh flat,
T&D $15/MWh flat, so total = $115/MWh: water must use the $100 Gen part, the electric benefit the whole $115)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import versions
from recet.jobs import RunConfig, run_job
from recet.pipeline import cost_tests, discount_settings
from conftest import (expected_flat_value, expected_two_baseline_value, make_electric_acc, make_flat_profile_csv, make_measure_csv)

GEN, TD = 0.100, 0.015          # $/kWh


def _cfg(tmp_path, **kw):
    measure_kw = kw.pop("measure", {})
    base = dict(measures=str(make_measure_csv(tmp_path / f"M_{kw.get('run_name', 'w')}.csv", **measure_kw)), out_dir=str(tmp_path / "out"), run_name="w",
                acc_version="2026", acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet", energy=60, ghg=40, transmission=10, distribution=5)),
                profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False)
    base.update(kw)
    return RunConfig(**base)


def _run(tmp_path, **kw):
    return run_job(_cfg(tmp_path, **kw), log=lambda s: None).frame.iloc[0]


RATE = discount_settings.get_discount_rate_qtr("PGE", versions.BUILTIN["2026"].acc)
RATE_SCT = discount_settings.get_discount_rate_qtr_sct("PGE", versions.BUILTIN["2026"].acc)


def test_water_benefit_is_priced_on_the_gen_term_only(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500})
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, GEN + TD, RATE), rel=1e-12)        # electric: Gen + T&D
    assert f["WaterEnergyBen"] == pytest.approx(expected_flat_value(500, GEN, RATE), rel=1e-12)       # water: Gen only
    assert f["WaterEnergyCost"] == 0 and f["WaterPricingStatus"] == "priced"
    # societal columns use the societal discount rate on the SCT rows (identical values here)
    assert f["WaterEnergyBen_SB"] == pytest.approx(expected_flat_value(500, GEN, RATE_SCT), rel=1e-12)
    assert f["WaterEnergyBen_SH"] == pytest.approx(f["WaterEnergyBen_SB"], rel=1e-12)


def test_water_enters_tsb_and_the_ratios_but_not_the_costs(tmp_path):
    costs = {"UnitMeaCost1stBaseline": 300, "UnitEndUserRebate": 100}      # so PAC, TRC and RIM costs are non-zero and their ratios defined
    with_water = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500, **costs}, run_name="a")
    without = _run(tmp_path, measure=costs, run_name="b")
    w = with_water["WaterEnergyBen"]
    assert with_water["TotalSystemBenefit"] == pytest.approx(without["TotalSystemBenefit"] + w, rel=1e-12)
    assert with_water["TRCCost"] == pytest.approx(without["TRCCost"], rel=1e-12) and with_water["PACCost"] == pytest.approx(without["PACCost"], rel=1e-12)
    assert with_water["TRCRatio"] > without["TRCRatio"] and with_water["PACRatio"] > without["PACRatio"] and with_water["RIMRatio"] > without["RIMRatio"]
    ben = with_water["ElecBen"] + with_water["GasBen"] + w + with_water["OtherBen"]
    assert with_water["PACRatio"] == pytest.approx(ben / with_water["PACCost"], rel=1e-12)
    assert with_water["TRCRatio"] == pytest.approx((ben + with_water["TaxCredits"]) / with_water["TRCCost"], rel=1e-12)
    assert with_water["SCBRatio"] > without["SCBRatio"]


def test_negative_water_is_a_cost_that_reduces_tsb_and_raises_trc_pac(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": -500}, run_name="neg")
    base = _run(tmp_path, run_name="base")
    cost = expected_flat_value(500, GEN, RATE)
    assert f["WaterEnergyCost"] == pytest.approx(cost, rel=1e-12) and f["WaterEnergyBen"] == 0
    assert f["TotalSystemBenefit"] == pytest.approx(base["TotalSystemBenefit"] - cost, rel=1e-12)
    assert f["TRCCost"] == pytest.approx(base["TRCCost"] + cost, rel=1e-12)
    assert f["PACCost"] == pytest.approx(base["PACCost"] + cost, rel=1e-12)
    assert f["RIMCost"] == pytest.approx(base["RIMCost"] + cost, rel=1e-12)
    assert f["WaterEnergyCost_SB"] == pytest.approx(expected_flat_value(500, GEN, RATE_SCT), rel=1e-12)
    assert f["SCBCost"] == pytest.approx(base["SCBCost"] + f["WaterEnergyCost_SB"], rel=1e-12)


def test_split_is_decided_by_the_first_baseline_sign(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 0, "UnitkWhIOUWater2ndBaseline": 300}, run_name="only2nd")
    assert f["WaterEnergyBen"] == 0 and f["WaterEnergyCost"] == 0          # CalcCE tests kWhWater1 only


def test_second_baseline_follows_the_rul(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500, "UnitkWhIOUWater2ndBaseline": 100, "RUL_Yrs": 0.5}, run_name="rul")
    assert f["WaterEnergyBen"] == pytest.approx(expected_two_baseline_value(500, 100, 0.5, GEN, RATE), rel=1e-12)


def test_market_effects_apply_to_water(tmp_path):
    a = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500, "NTGRkWh": 0.8, "NTGRkW": 0.8}, run_name="m0")
    b = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500, "NTGRkWh": 0.8, "NTGRkW": 0.8}, market_effects=0.05, run_name="m5")
    assert b["WaterEnergyBen"] == pytest.approx(a["WaterEnergyBen"] * 0.85 / 0.8, rel=1e-12)
    assert a["WaterEnergyBenGross"] == pytest.approx(a["WaterEnergyBen"] / 0.8, rel=1e-12)             # gross removes the NTG
    assert b["WaterEnergyBenGross"] == pytest.approx(a["WaterEnergyBenGross"], rel=1e-12)              # ... and the market effect, so it does not change


def test_water_is_unpriced_when_the_measure_has_no_profile(tmp_path, monkeypatch):
    # The built-in library has no match for the measure (and the test must not need the reference data folder to find that out).
    from recet.pipeline import real_impact_profiles
    monkeypatch.setattr(real_impact_profiles, "match_profile_id", lambda *a, **k: (None, False))
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500}, profiles_8760_file=None, run_name="noprof")
    assert f["WaterEnergyBen"] == 0 and f["WaterPricingStatus"].startswith("UNPRICED")


def test_a_measure_with_no_water_says_so(tmp_path):
    f = _run(tmp_path, run_name="none")
    assert f["WaterPricingStatus"] == "no water energy" and f["WaterEnergyBen"] == 0 and f["WaterEnergyCost"] == 0


def test_water_only_measure_is_priced(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500, "UnitkWh1stBaseline": 0}, run_name="wonly")
    assert f["ElecBen"] == 0 and f["WaterEnergyBen"] == pytest.approx(expected_flat_value(500, GEN, RATE), rel=1e-12)


def test_program_cost_weights_count_water_benefit():
    """Two measures in one program, one with only an electric benefit and one with only a water benefit: the program's costs are shared in proportion to
    their positive benefits INCLUDING water (CalcCE's BenPos / BensSum)."""
    z = 0.0
    d = pd.DataFrame({"measure_id": ["e", "w"], "prg_id": ["P", "P"], "NTGRkWh": [1.0, 1.0], "ntgr_therm": [1.0, 1.0],
                      "ElecBen": [300.0, z], "GasBen": [z, z], "WaterEnergyBen": [z, 100.0], "OtherBen": [z, z],
                      "ElecSupplyCost": [z, z], "GasSupplyCost": [z, z], "WaterEnergyCost": [z, z], "OtherCost": [z, z],
                      "OtherBenGross": [z, z], "OtherCostGross": [z, z], "TaxCredits": [z, z], "TaxCreditsGross": [z, z]})
    for s in ("_SB", "_SH"):
        for c in ("ElecBen", "GasBen", "ElecSupplyCost", "GasSupplyCost"):
            d[c + s] = d[c]
        d["WaterEnergyBen" + s] = d["WaterEnergyBen"]
        d["WaterEnergyCost" + s] = 0.0
    for c in ("RebatesandIncentsPV", "NetParticipantCostPV", "NetParticipantCostAdjustedPV", "GrossParticipantCostPV", "RebatesandIncentsPV_sct",
              "NetParticipantCostPV_sct", "NetParticipantCostAdjustedPV_sct", "GrossParticipantCostPV_sct", "LifecycleBillReducElec", "LifecycleBillReducGas", "LifecycleBillIncrElec", "LifecycleBillIncrGas"):
        d[c] = 0.0
    prog = pd.DataFrame({"PrgID": ["P"], "SumCosts": [1000.0], "SumCostsNPV": [1000.0], "SumCostsNPV_SCT": [1000.0]})
    out = cost_tests.compute_cost_tests(d, prog).set_index("measure_id")
    assert out.loc["e", "WeightedBenefits"] == pytest.approx(0.75) and out.loc["w", "WeightedBenefits"] == pytest.approx(0.25)
    assert out.loc["w", "WeightedProgramCost"] == pytest.approx(250.0)
    assert out.loc["w", "TRCCost"] == pytest.approx(250.0) and out.loc["w", "TotalSystemBenefit"] == pytest.approx(100.0)


def test_summary_and_columns_carry_water(tmp_path):
    r = run_job(_cfg(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500}, run_name="cols"), log=lambda s: None)
    assert {"WaterEnergyBen", "WaterEnergyCost", "WaterEnergyBen_SB", "WaterEnergyCost_SH", "WaterEnergyBenGross", "WaterPricingStatus",
            "AC_kWh_Water_1st_Baseline"} <= set(r.frame.columns)
    summ = pd.read_csv(r.files["portfolio_summary"])
    assert summ.loc[0, "WaterEnergyBen"] == pytest.approx(r.frame.iloc[0]["WaterEnergyBen"])
    assert "no_water_profile" not in (r.run_dir / "preflight.txt").read_text()


def test_no_societal_run_blanks_the_water_societal_columns(tmp_path):
    f = _run(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500}, include_societal=False, run_name="nosoc")
    assert pd.isna(f["WaterEnergyBen_SB"]) and pd.isna(f["WaterEnergyCost_SH"]) and not np.isnan(f["WaterEnergyBen"])


def test_preflight_warns_about_water_measures_without_a_profile(tmp_path):
    r = run_job(_cfg(tmp_path, measure={"UnitkWhIOUWater1stBaseline": 500}, profiles_8760_file=None, run_name="pfw", preflight_only=True), log=lambda s: None)
    text = (r.run_dir / "preflight.txt").read_text()
    assert "water-energy measures: 1" in text
