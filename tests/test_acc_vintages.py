"""The 2021 and 2022 ACCs: versions named by ACC, settings picked by ACC version, the ACC's own hourly calendar, and TRC-only pricing. No reference data needed:
the electric ACC is synthetic; the 2021/2022 gas ACCs ship in the package."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from recet import runtime, versions
from recet.jobs import RunConfig, run_job
from recet.pipeline import discount_settings, gas_avoided_cost, real_avoided_cost_v2, real_impact_profiles
from conftest import expected_flat_value, make_electric_acc, make_flat_profile_csv, make_measure_csv


def test_cet_version_numbers_map_to_acc_versions():
    assert {k: versions.acc_of_cet_version(k) for k in (2022, 2024, 2026, 2027)} == {2022: 2021, 2024: 2022, 2026: 2024, 2027: 2026}
    assert versions.acc_of_cet_version(2013) is None and versions.acc_of_cet_version(2017) is None and versions.acc_of_cet_version("x") is None
    assert [k for k, _ in versions.choices()] == ["2021", "2022", "2024", "2026"]
    assert [label for _, label in versions.choices()] == ["2021 ACC (Avoided Cost Version 2022)", "2022 ACC (Avoided Cost Version 2024)",
                                                          "2024 ACC (Avoided Cost Version 2026)", "2026 ACC (Avoided Cost Version 2028)"]


def test_run_parameters_are_picked_by_acc_version():
    assert {2017, 2018, 2019, 2020, 2021, 2022, 2024, 2026} <= set(discount_settings.known_versions())
    assert 2027 not in discount_settings.known_versions()                        # 2027 was CET's number for the 2026 ACC, not an ACC
    for acc, base in ((2021, 2021), (2022, 2022), (2024, 2024), (2026, 2024)):
        s = discount_settings.get_settings("PGE", acc)
        assert s["settings_base_year"] == base and s["discount_rate_annual"] == pytest.approx(0.0766)


def test_old_saved_config_with_cet_e3_version_is_converted():
    cfg = RunConfig.from_dict({"measures": "M.csv", "acc_version": "2030", "e3_version": 2027})
    assert cfg.settings_from == 2026
    assert RunConfig.from_dict({"measures": "M.csv", "e3_version": 2027, "settings_from": 2022}).settings_from == 2022   # an explicit new field wins


def test_calendar_is_read_from_the_acc_file(tmp_path):
    runtime.apply(versions.resolve_acc("2021", electric_file=make_electric_acc(tmp_path / "a20.parquet", calendar_year=2020)))
    assert real_impact_profiles.acc_calendar_year() == 2020
    assert real_impact_profiles.alignment_offset_days(2025) == 0                    # TOU 8,760s (2025, Wednesday start) need no rotation on 2020
    assert real_impact_profiles.alignment_offset_days(2009) == -1                   # 2009 starts on a Thursday, 2020 on a Wednesday
    runtime.apply(versions.resolve_acc("2024", electric_file=make_electric_acc(tmp_path / "a18.parquet", calendar_year=2018)))
    assert real_impact_profiles.acc_calendar_year() == 2018 and real_impact_profiles.alignment_offset_days(2025) == -2
    runtime.apply(versions.resolve_acc("2024", electric_file=make_electric_acc(tmp_path / "anone.parquet")))
    assert real_impact_profiles.acc_calendar_year() == 2018                         # no hour stamps: the 2018 default


def _day_one_acc(path, calendar_year):
    """An ACC worth $100/MWh in the first 24 hours of every year and nothing else, with hour stamps on `calendar_year`."""
    rows = []
    for iou, cz in (("PG&E", "CZ13"), ("SCE", "CZ9"), ("SDG&E", "CZ7")):
        for y in (2027, 2028, 2029):
            h = np.arange(1, 8761)
            v = np.where(h <= 24, 100.0, 0.0)
            rows.append(pd.DataFrame({"iou": iou, "climate_zone": cz, "cost_test": "TRC", "year": y, "hour_of_year": h, "energy": v,
                                      "total_avoided_cost": v, "transmission": 0.0, "distribution": 0.0, "emission_rate_tonnes_per_kwh": 0.0,
                                      "datetime": pd.Timestamp(f"{calendar_year}-01-01") + pd.to_timedelta(h - 1, unit="h")}))
    pd.concat(rows, ignore_index=True).to_parquet(path, index=False)
    return path


def test_profiles_are_aligned_to_the_acc_calendar(tmp_path):
    """A profile drawn on 2020 whose savings all fall on its own 1 January: on a 2020-calendar ACC it lands on hour 1-24 and earns the whole day-one value; on a
    2018-calendar ACC it is rotated two days (2020 starts on a Wednesday, 2018 on a Monday) and earns nothing."""
    prof = pd.DataFrame({"end_use_name": "TEST_SHAPE", "pa": "PGE", "target_sector": "Residential", "climate_zone": "ALL", "hour_of_year": np.arange(1, 8761),
                         "impact": np.where(np.arange(1, 8761) <= 24, 1 / 24, 0.0), "represents_year": 2020})
    prof.to_csv(tmp_path / "p.csv", index=False)
    common = dict(measures=str(make_measure_csv(tmp_path / "Measure.csv")), out_dir=str(tmp_path / "out"), profiles_8760_file=str(tmp_path / "p.csv"),
                  emissions=False, include_societal=False)
    on_2020 = run_job(RunConfig(run_name="c20", acc_version="2021", acc_electric_file=str(_day_one_acc(tmp_path / "d20.parquet", 2020)), **common),
                      log=lambda s: None).frame.iloc[0]
    on_2018 = run_job(RunConfig(run_name="c18", acc_version="2024", acc_electric_file=str(_day_one_acc(tmp_path / "d18.parquet", 2018)), **common),
                      log=lambda s: None).frame.iloc[0]
    assert on_2020["ElecBen"] == pytest.approx(1000 * 0.100)                       # 1,000 kWh x $0.10/kWh, all in the claim quarter (undiscounted)
    assert on_2018["ElecBen"] == pytest.approx(0.0, abs=1e-9)


def test_trc_only_acc_leaves_the_societal_columns_blank(tmp_path):
    cfg = RunConfig(measures=str(make_measure_csv(tmp_path / "Measure.csv")), out_dir=str(tmp_path / "out"), run_name="t", acc_version="2021",
                    acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet", tests=("TRC",), calendar_year=2020)),
                    profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False)
    r = run_job(cfg, log=lambda s: None)
    f = r.frame.iloc[0]
    rate = discount_settings.get_discount_rate_qtr("PGE", 2021)
    assert f["ElecBen"] == pytest.approx(expected_flat_value(1000, 0.100, rate), rel=1e-12)
    assert f["Used_ACCVersion"] == "2021 ACC (Avoided Cost Version 2022)"
    societal = [c for c in r.frame.columns if c.endswith(("_SB", "_SH")) or c.startswith(("SCB", "SCH"))]
    assert societal and all(math.isnan(float(f[c])) for c in societal if pd.api.types.is_number(f[c]))
    assert any("no Societal Cost Test avoided costs" in n for n in r.notes)


def test_2021_and_2022_gas_acc_have_no_air_quality_adder_and_trc_only(tmp_path):
    for key, years in (("2021", (2020, 2050)), ("2022", (2022, 2052))):
        runtime.apply(versions.resolve_acc(key, electric_file=make_electric_acc(tmp_path / f"e{key}.parquet")))
        assert gas_avoided_cost.available_cost_tests() == ("TRC",)
        assert "air_quality_adder" not in gas_avoided_cost.available_components()
        assert gas_avoided_cost.year_range() == years
        parts = gas_avoided_cost.get_components_for_measure("PGE", "Residential", years[0] + 5)
        assert (parts["air_quality_adder"] == 0).all() and parts["co2"].min() > 0
    assert real_avoided_cost_v2.available_cost_tests() == ("TRC", "SCT Base", "SCT High")
