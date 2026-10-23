"""Separate electric shapes for the 2nd-baseline years (AC_Elec_Impact_Profile_2nd_Baseline) and for the water energy (AC_Elec_Impact_Profile_Water); blank = the measure's own."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet.jobs import RunConfig, run_job
from recet.pipeline import discount_settings
from recet import versions
from conftest import QUARTER_HOURS, make_electric_acc, make_measure_csv

PEAK_HOUR = 4500                       # in Q3 of the 2018 calendar (Q1 + Q2 = 4,344 hours)


def _acc(path):
    """$100/MWh every hour, $300/MWh in PEAK_HOUR; no T&D, so the generation-only water price is the same."""
    make_electric_acc(path)
    df = pd.read_parquet(path)
    sel = df["hour_of_year"] == PEAK_HOUR
    df.loc[sel, "energy"], df.loc[sel, "ghg_adder"], df.loc[sel, "total_avoided_cost"] = 300.0, 0.0, 300.0
    df.to_parquet(path, index=False)
    return path


def _profiles(path):
    flat = np.full(8760, 1 / 8760)
    peak = np.zeros(8760)
    peak[PEAK_HOUR - 1] = 1.0
    frames = [pd.DataFrame({"end_use_name": name, "pa": "PGE", "target_sector": "Residential", "climate_zone": "ALL", "hour_of_year": np.arange(1, 8761),
                            "impact": v, "represents_year": 2018}) for name, v in (("FLAT", flat), ("PEAK", peak))]
    pd.concat(frames).to_csv(path, index=False)
    return path


def _run(tmp_path, name, **measure):
    m = make_measure_csv(tmp_path / f"{name}.csv", AC_Elec_Impact_Profile="FLAT", **measure)
    return run_job(RunConfig(measures=str(m), out_dir=str(tmp_path / "out"), run_name=name, acc_version="2026", acc_electric_file=str(_acc(tmp_path / "acc.parquet")),
                             profiles_8760_file=str(_profiles(tmp_path / "p.csv")), emissions=False), log=lambda s: None).frame.iloc[0]


def _rate():
    return discount_settings.get_discount_rate_qtr("PGE", versions.BUILTIN["2026"].acc)


def test_second_baseline_years_are_priced_on_their_own_shape(tmp_path):
    early = dict(RUL_Yrs="0.5", UnitkWh1stBaseline="1000", UnitkWh2ndBaseline="400")      # Q1-Q2 on the 1st baseline, Q3-Q4 on the 2nd
    f = _run(tmp_path, "split", AC_Elec_Impact_Profile_2nd_Baseline="PEAK", **early)
    r = 1 + _rate()
    first = sum(1000 * 0.100 * QUARTER_HOURS[q] / 8760 / r ** q for q in (0, 1))         # flat shape at $100/MWh
    second = 400 * 0.300 / r ** 2                                                          # all 2nd-baseline kWh in the $300 hour of Q3
    assert f["ElecBen"] == pytest.approx(first + second, rel=1e-12)
    assert f["ElecPricingStatus"] == "priced: hourly profile" and f["poc_shape_key_2nd"] == "PEAK"
    same = _run(tmp_path, "one_shape", **early)
    q3 = 400 * ((QUARTER_HOURS[2] - 1) * 0.100 + 0.300) / 8760 / r ** 2                   # the flat shape also sees the $300 hour
    assert same["ElecBen"] == pytest.approx(first + q3 + 400 * 0.100 * QUARTER_HOURS[3] / 8760 / r ** 3, rel=1e-12)


def test_water_energy_is_priced_on_its_own_shape(tmp_path):
    f = _run(tmp_path, "water", UnitkWh1stBaseline="0", UnitkWhIOUWater1stBaseline="50", AC_Elec_Impact_Profile_Water="PEAK")
    assert f["WaterEnergyBen"] == pytest.approx(50 * 0.300 / (1 + _rate()) ** 2, rel=1e-12) and f["poc_shape_key_water"] == "PEAK"


def test_a_missing_second_baseline_shape_leaves_the_measure_partly_priced(tmp_path, monkeypatch):
    from recet.pipeline import real_impact_profiles
    monkeypatch.setattr(real_impact_profiles, "match_profile_id", lambda *a, **k: (None, False))     # no library here: an unknown name matches nothing
    f = _run(tmp_path, "partly", AC_Elec_Impact_Profile_2nd_Baseline="NO_SUCH_SHAPE", RUL_Yrs="0.5", UnitkWh1stBaseline="1000", UnitkWh2ndBaseline="400")
    assert f["ElecPricingStatus"].startswith("PARTLY PRICED")
