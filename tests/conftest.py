"""Shared fixtures: tiny synthetic inputs so the tests run without the 11 GB reference data."""
from __future__ import annotations

import os
import tempfile

# Keep the developer's real ~/.recet config out of the tests from the first import on.
os.environ["RECET_HOME"] = tempfile.mkdtemp(prefix="recet_test_home_")

import numpy as np
import pandas as pd
import pytest

from recet import templates

QUARTER_HOURS = [2160, 2184, 2208, 2208]


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never read or write the developer's real ~/.recet config or reference folder."""
    monkeypatch.setenv("RECET_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RECET_DATA", raising=False)
    from recet import paths
    from recet.pipeline import discount_settings, statewide_allocation
    paths.set_reference_dir(None)
    yield
    paths.set_reference_dir(None)
    discount_settings.set_user_parameters(None)          # a run's supplied run parameters / SW factors must not leak into the next test
    statewide_allocation.set_user_factors(None)
    from recet import programs
    programs.set_user_programs(None)


def make_measure_csv(path, **overrides):
    """A one-measure CET Measure.csv: PGE, zone 13, claim 2028Q1, 1-year life, 1,000 kWh, NTG 1, no costs."""
    row = {c: "" for c in templates._CET_MEASURE_HEADER}
    row.update({"CEInputID": "T1", "PrgID": "PRG1", "ClaimYearQuarter": "2028Q1", "Sector": "Res", "E3ClimateZone": "13", "E3GasSector": "Residential",
                "E3MeaElecEndUseShape": "TEST_SHAPE", "E3TargetSector": "Residential", "MeasDescription": "test measure", "IOU_AC_Territory": "PGE",
                "EUL_Yrs": "1", "RUL_Yrs": "0", "NumUnits": "1", "UnitkWh1stBaseline": "1000", "UnitkW1stBaseline": "0", "UnitTherm1stBaseline": "0",
                "UnitkW2ndBaseline": "0", "UnitkWh2ndBaseline": "0", "UnitTherm2ndBaseline": "0",
                "NTGRkW": "1", "NTGRkWh": "1", "NTGRTherm": "1", "NTGRCost": "1", "RealizationRatekW": "1", "RealizationRatekWh": "1", "RealizationRateTherm": "1",
                "InstallationRatekW": "1", "InstallationRatekWh": "1", "InstallationRateTherm": "1", "MeasInflation": "0"})
    row.update({k: str(v) for k, v in overrides.items()})
    header = list(row)
    path.write_text("|".join(header) + "\n" + "|".join(row[c] for c in header) + "\n", encoding="utf-8")
    return path


def make_electric_acc(path, energy=60.0, ghg=40.0, years=(2027, 2028, 2029), transmission=0.0, distribution=0.0):
    """A synthetic electric ACC ($/MWh): PG&E CZ13 (and SCE CZ9, SDG&E CZ7 so every IOU is present), all three cost tests, constant in time."""
    frames = []
    for iou, cz in (("PG&E", "CZ13"), ("SCE", "CZ9"), ("SDG&E", "CZ7")):
        for test in ("TRC", "SCT Base", "SCT High"):
            for y in years:
                frames.append(pd.DataFrame({"iou": iou, "climate_zone": cz, "cost_test": test, "year": y, "hour_of_year": np.arange(1, 8761),
                                            "energy": energy, "ghg_adder": ghg, "generation_capacity": 0.0, "transmission": transmission, "distribution": distribution,
                                            "avoided_as_procurement": 0.0, "losses": 0.0, "methane_leakage": 0.0, "air_quality_adder": 0.0,
                                            "ghg_cap_and_invest": 0.0, "total_avoided_cost": energy + ghg + transmission + distribution, "emission_rate_tonnes_per_kwh": 0.0}))
    pd.concat(frames, ignore_index=True).to_parquet(path, index=False)
    return path


def make_flat_profile_csv(path, end_use="TEST_SHAPE", pa="PGE", sector="Residential", zone="ALL", total=1.0):
    pd.DataFrame({"end_use_name": end_use, "pa": pa, "target_sector": sector, "climate_zone": zone,
                  "hour_of_year": np.arange(1, 8761), "impact": total / 8760}).to_csv(path, index=False)
    return path


def expected_flat_value(kwh, dollars_per_kwh, rate_qtr):
    """Discounted value of a flat-shape, one-year, Q1-claim measure priced at a constant $/kWh (First Year = claim year)."""
    return sum(kwh * dollars_per_kwh * h / 8760 / (1 + rate_qtr) ** q for q, h in enumerate(QUARTER_HOURS))


def expected_two_baseline_value(kwh1, kwh2, rul_years, dollars_per_kwh, rate_qtr):
    """Same, for a one-year measure whose 1st-baseline kWh applies until RUL and 2nd-baseline kWh after (whole quarters only)."""
    rul_q = rul_years * 4
    return sum((kwh1 if q + 1 <= rul_q else kwh2) * dollars_per_kwh * h / 8760 / (1 + rate_qtr) ** q for q, h in enumerate(QUARTER_HOURS))
