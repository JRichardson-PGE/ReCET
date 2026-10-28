"""8,760 profiles given in kWh per unit per hour (BRD BR-06.6): the measure's kWh comes from the profile, and a profile that nets to zero (load shifting only)
is priced in one record."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from recet import overrides
from recet.jobs import RunConfig, run_job
from conftest import expected_flat_value, make_electric_acc, make_measure_csv


def _profile(path, values, basis="kwh_per_unit", end_use="TEST_SHAPE", represents_year=2018):
    pd.DataFrame({"end_use_name": end_use, "pa": "PGE", "target_sector": "Residential", "climate_zone": "ALL", "hour_of_year": np.arange(1, 8761),
                  "impact": values, "profile_basis": basis, "represents_year": represents_year}).to_csv(path, index=False)
    return path


def _peaky_acc(path):
    """$100/MWh every hour, except hour 1 at $300 and hour 2 at $100 ... and hour 3 at $50, every year (no calendar shift: the profile is on 2018 too)."""
    make_electric_acc(path)
    df = pd.read_parquet(path)
    for hour, total in ((1, 300.0), (2, 100.0), (3, 50.0)):
        sel = df["hour_of_year"] == hour
        df.loc[sel, "energy"], df.loc[sel, "ghg_adder"], df.loc[sel, "total_avoided_cost"] = total, 0.0, total
    df.to_parquet(path, index=False)
    return path


def _run(tmp_path, measure_kw, profile, name, acc=None):
    m = make_measure_csv(tmp_path / f"{name}.csv", **measure_kw)
    return run_job(RunConfig(measures=str(m), out_dir=str(tmp_path / "out"), run_name=name, acc_version="2026",
                             acc_electric_file=str(acc or make_electric_acc(tmp_path / "acc.parquet")), profiles_8760_file=str(profile),
                             emissions=False), log=lambda s: None)


def test_load_shifting_only_measure_is_priced_in_one_record(tmp_path):
    values = np.zeros(8760)
    values[0], values[2] = 1.0, -1.0                       # 1 kWh per unit saved in hour 1 ($300/MWh), 1 kWh added in hour 3 ($50/MWh): nets to zero
    r = _run(tmp_path, {"NumUnits": "10", "UnitkWh1stBaseline": "0"}, _profile(tmp_path / "p.csv", values), "shift", _peaky_acc(tmp_path / "acc.parquet"))
    f = r.frame.iloc[0]
    assert "kwh_per_unit_profile" in (r.run_dir / "preflight.txt").read_text(encoding="utf-8")
    found = {i["code"]: i for i in json.loads((r.run_dir / "preflight.json").read_text(encoding="utf-8"))["issues"]}
    assert found["kwh_per_unit_profile"]["cells"] == [["Measure", 0, "AC_Elec_Impact_Profile"]]          # it names its records, like every record finding
    assert f["ProfileSource"] == "user profile (kWh per unit)" and f["ElecPricingStatus"] == "priced: hourly profile"
    assert f["ElecBen"] == pytest.approx(10 * (0.300 - 0.050), rel=1e-12)    # hour 1 and hour 3 are both in Q1 of the claim year (no discounting)
    assert f["FirstYearNetKWh"] == 0 and f["LifecycleBillReducElec"] == 0               # no net annual kWh: no first-year kWh, no bill change on annual rates


def test_kwh_per_unit_profile_gives_the_same_value_as_a_share_profile_and_the_kwh_it_implies(tmp_path):
    flat = np.full(8760, 1000.0 / 8760)                     # 1,000 kWh per unit per year, flat
    scaled = _run(tmp_path, {"UnitkWh1stBaseline": "0"}, _profile(tmp_path / "s.csv", flat), "scaled").frame.iloc[0]
    share = _run(tmp_path, {"UnitkWh1stBaseline": "1000"}, _profile(tmp_path / "u.csv", flat / 1000.0, basis="share"), "share").frame.iloc[0]
    assert scaled["ElecBen"] == pytest.approx(share["ElecBen"], rel=1e-12)
    assert scaled["FirstYearNetKWh"] == pytest.approx(share["FirstYearNetKWh"], rel=1e-12) == pytest.approx(1000.0)
    assert scaled["AC_Net_kWh_1st_Baseline"] == pytest.approx(share["AC_Net_kWh_1st_Baseline"], rel=1e-12)    # the profile's own annual kWh, not UnitkWh1stBaseline (0)


def test_loader_rejects_an_all_zero_or_mixed_basis_profile(tmp_path):
    with pytest.raises(overrides.OverrideError, match="every impact is 0"):
        overrides.load_user_profiles(_profile(tmp_path / "z.csv", np.zeros(8760)))
    p = _profile(tmp_path / "m.csv", np.full(8760, 1 / 8760))
    df = pd.read_csv(p)
    df.loc[0, "profile_basis"] = "share"
    df.to_csv(p, index=False)
    with pytest.raises(overrides.OverrideError, match="profile_basis must be the same"):
        overrides.load_user_profiles(p)
