"""The System climate zone (pipeline/system_zones.py): a System measure is priced in the zone with the lowest total avoided cost over the ACC."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet.jobs import RunConfig, run_job
from recet.pipeline import system_zones as sz
from conftest import expected_flat_value, make_measure_csv
from test_run import _rate


def _acc(path, pge_prices: dict[str, float], years=(2027, 2028, 2029)):
    """A synthetic electric ACC: PG&E in the given zones at a flat $/MWh each (all energy), plus SCE CZ9 / SDG&E CZ7 so every IOU is present."""
    frames = []
    for iou, cz, price in [("PG&E", f"CZ{z}", p) for z, p in pge_prices.items()] + [("SCE", "CZ9", 80.0), ("SDG&E", "CZ7", 80.0)]:
        for test in ("TRC", "SCT Base", "SCT High"):
            for y in years:
                frames.append(pd.DataFrame({"iou": iou, "climate_zone": cz, "cost_test": test, "year": y, "hour_of_year": np.arange(1, 8761),
                                            "energy": price, "ghg_adder": 0.0, "generation_capacity": 0.0, "transmission": 0.0, "distribution": 0.0,
                                            "avoided_as_procurement": 0.0, "losses": 0.0, "methane_leakage": 0.0, "air_quality_adder": 0.0,
                                            "ghg_cap_and_invest": 0.0, "total_avoided_cost": price, "emission_rate_tonnes_per_kwh": 0.0}))
    pd.concat(frames, ignore_index=True).to_parquet(path, index=False)
    return path


def _profiles(path, zones: dict[str, np.ndarray]):
    pd.concat([pd.DataFrame({"end_use_name": "TEST_SHAPE", "pa": "PGE", "target_sector": "Residential", "climate_zone": z, "hour_of_year": np.arange(1, 8761),
                             "impact": s}) for z, s in zones.items()]).to_csv(path, index=False)
    return path


def _run(tmp_path, acc, profiles, zone="System"):
    cfg = RunConfig(measures=str(make_measure_csv(tmp_path / "Measure.csv", E3ClimateZone=zone)), out_dir=str(tmp_path / "out"), run_name="t", acc_version="2026",
                    acc_electric_file=str(acc), profiles_8760_file=str(profiles), emissions=False)
    return run_job(cfg, log=lambda s: None).frame.iloc[0]


FLAT = np.full(8760, 1 / 8760)


def test_pick_takes_the_lowest_and_breaks_ties_in_zone_order():
    assert sz.pick({"13": 2.0, "1": 3.0, "3A": 2.5}) == "13"
    assert sz.pick({"13": 2.0, "3B": 2.0, "3A": 2.0}) == "3A"          # natural order: 3A before 3B before 13
    assert sz.pick({"1": -1.0, "2": 0.5}) == "1"                       # lowest, not smallest in magnitude
    assert sz.pick({}) is None


def test_zone_totals_use_each_zones_own_shape_and_prices():
    price = {"1": np.full(8760, 0.1), "13": np.r_[np.full(4380, 0.0), np.full(4380, 0.2)]}
    front = np.r_[np.full(4380, 1 / 4380), np.zeros(4380)]             # all savings in the first half of the year
    shapes = {"1": FLAT, "13": front, "System": FLAT}
    tot = sz.zone_totals("PGE", shapes, 2018, sums=price)              # 2018: no calendar shift
    assert set(tot) == {"1", "13"}                                     # the profile's own System rows are never an option
    assert tot["1"] == pytest.approx(0.1) and tot["13"] == pytest.approx(0.0)
    assert sz.pick(tot) == "13"                                        # the shape, not just the price, decides


def test_system_measure_is_priced_in_the_cheapest_zone(tmp_path):
    acc = _acc(tmp_path / "acc.parquet", {"1": 50.0, "13": 100.0})
    prof = _profiles(tmp_path / "p.csv", {"1": FLAT, "13": FLAT})
    f = _run(tmp_path, acc, prof)
    assert f["SystemClimateZone"] == "1" and f["SystemZoneSource"] == "calculated (user profile)"
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, 0.050, _rate()), rel=1e-12)
    g = _run(tmp_path, acc, prof, zone="1")                            # the same as stating zone 1 outright
    assert g["ElecBen"] == pytest.approx(f["ElecBen"], rel=1e-15) and pd.isna(g["SystemClimateZone"])


def test_only_zones_the_profile_has_are_options(tmp_path):
    acc = _acc(tmp_path / "acc.parquet", {"1": 50.0, "13": 100.0})
    f = _run(tmp_path, acc, _profiles(tmp_path / "p.csv", {"13": FLAT}))   # zone 1 is cheaper but the profile has no zone-1 shape
    assert f["SystemClimateZone"] == "13"
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, 0.100, _rate()), rel=1e-12)


def test_profile_given_only_for_all_zones_lets_the_prices_decide(tmp_path):
    acc = _acc(tmp_path / "acc.parquet", {"1": 90.0, "11": 40.0, "13": 70.0})
    f = _run(tmp_path, acc, _profiles(tmp_path / "p.csv", {"ALL": FLAT}))
    assert f["SystemClimateZone"] == "11"
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, 0.040, _rate()), rel=1e-12)


def test_a_files_own_system_rows_are_not_the_system_measures_shape(tmp_path):
    acc = _acc(tmp_path / "acc.parquet", {"1": 50.0, "13": 100.0})
    back = np.r_[np.zeros(4380), np.full(4380, 1 / 4380)]
    f = _run(tmp_path, acc, _profiles(tmp_path / "p.csv", {"1": FLAT, "13": FLAT, "System": back}))
    assert f["SystemClimateZone"] == "1"
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, 0.050, _rate()), rel=1e-12)   # FLAT in zone 1, not the System rows' shape
