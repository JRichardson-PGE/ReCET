"""recet regress (the regression gate's tool): the sliced reference folder holds only what a run read and reproduces the full folder's results; check
finds a changed result; compare reports two runs' differences."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from recet import regress
from test_calcs import LIB_MEASURES, make_stub_library, write_measures


def full_reference(folder):
    """A reference folder like the real one: a 2024 ACC with three (utility, zone) pairs and values that vary by hour and year, and the stub library."""
    make_stub_library(folder)
    h = np.arange(1, 8761)
    frames = []
    for k, (iou, cz) in enumerate((("PG&E", "CZ13"), ("SCE", "CZ9"), ("SDG&E", "CZ7"))):
        for t, test in enumerate(("TRC", "SCT Base", "SCT High")):
            for y in range(2026, 2034):
                energy = 40 + 10 * np.sin(2 * np.pi * h / 24 + k) + y - 2026 + t
                td = 3 + np.cos(2 * np.pi * h / 8760)
                frames.append(pd.DataFrame({"iou": iou, "climate_zone": cz, "cost_test": test, "year": y, "hour_of_year": h, "energy": energy,
                                            "ghg_adder": 5.0, "transmission": td / 2, "distribution": td / 2, "total_avoided_cost": energy + 5.0 + td,
                                            "emission_rate_tonnes_per_kwh": 3e-4 + 1e-5 * np.sin(h / 50.0)}))
    f = folder / "acc" / "2024" / "electric_hourly.parquet"
    f.parent.mkdir(parents=True)
    pd.concat(frames, ignore_index=True).to_parquet(f, index=False)
    return folder


@pytest.fixture
def built(tmp_path):
    src = full_reference(tmp_path / "full")
    job = tmp_path / "job"
    job.mkdir()
    write_measures(job / "Measure.csv", {k: LIB_MEASURES[k] for k in ("LIB1", "LIB3", "NONE1")})
    manifest = regress.build([regress.Job("lib", job / "Measure.csv", None, {"acc_version": "2024"})], src, tmp_path / "fixtures", log=lambda s: None)
    return tmp_path / "fixtures", manifest


def test_the_slice_holds_only_what_was_read(built):
    fixtures, manifest = built
    acc = pd.read_parquet(fixtures / "ref" / "acc" / "2024" / "electric_hourly.parquet")
    assert set(acc["iou"]) == {"PG&E"} and len(acc) == 3 * 8 * 8760                 # the one pair the job reads: every cost test and year kept
    for c in ("energy", "ghg_adder", "transmission", "distribution"):                # columns this job's pricing never reads
        assert acc[c].isna().all(), c
    assert acc["total_avoided_cost"].notna().all()
    trc = acc["cost_test"] == "TRC"
    assert acc.loc[trc, "emission_rate_tonnes_per_kwh"].notna().all() and acc.loc[~trc, "emission_rate_tonnes_per_kwh"].isna().all()
    lib = pq.read_table(fixtures / "ref" / "impact_profiles" / "hourly.parquet").to_pandas()
    assert set(lib["impact_profile_id"]) == {1, 2} and set(lib.loc[lib.impact_profile_id == 1, "climate_zone"]) == {"13"}   # profile 3 never read
    assert manifest["jobs"]["lib"]["measures"] == 3 and "measure_level" in manifest["jobs"]["lib"]["tables"]
    assert json.loads((fixtures / "manifest.json").read_text())["ref"]["acc/2024/electric_hourly.parquet"]["bytes"] > 0


def test_check_passes_and_finds_a_changed_result(built, tmp_path):
    fixtures, _ = built
    assert regress.check(fixtures, tmp_path / "c1", log=lambda s: None) == {"lib": {}}
    f = fixtures / "jobs" / "lib" / "expected" / "measure_level.parquet"
    df = pd.read_parquet(f)
    i = int(df["ElecBen"].abs().idxmax())
    assert df.loc[i, "ElecBen"] > 1
    df.loc[i, "ElecBen"] = df.loc[i, "ElecBen"] * (1 + 1e-6)
    df.to_parquet(f, index=False)
    diff = regress.check(fixtures, tmp_path / "c2", log=lambda s: None)["lib"]
    assert list(diff) == ["measure_level"] and diff["measure_level"][0].startswith(f"ElecBen: 1 value(s) differ, worst row {i}")
    regress.check(fixtures, tmp_path / "c3", update=True, log=lambda s: None)
    assert regress.check(fixtures, tmp_path / "c4", log=lambda s: None) == {"lib": {}}


def test_check_refuses_a_changed_reference_file(built, tmp_path):
    fixtures, _ = built
    with open(fixtures / "ref" / "acc" / "2024" / "electric_hourly.parquet", "ab") as fh:
        fh.write(b"x")
    with pytest.raises(RuntimeError, match="missing or changed"):
        regress.check(fixtures, tmp_path / "c", log=lambda s: None)


def test_differences():
    a = pd.DataFrame({"x": [1.0, 2.0, np.nan], "s": ["a", "b", None]})
    assert regress.differences(a, a.copy(), 1e-9, 0) == []
    b = a.assign(x=[1.0, 2.0 + 1e-6, np.nan], s=["a", "c", None])
    d = regress.differences(a, b, 1e-9, 0)
    assert d[0].startswith("x: 1 value(s) differ, worst row 1") and d[1].startswith("s: 1 value(s) differ, first row 1")
    assert regress.differences(a, b, 1e-6, 0)[0].startswith("s:")                  # within the tolerance
    assert "rows" in regress.differences(a.iloc[:2], a, 1e-9, 0)[0]
    assert "missing ['s']" in regress.differences(a[["x"]], a, 1e-9, 0)[0]


def test_cli(built, tmp_path, capsys):
    fixtures, _ = built
    assert regress.main(["check", str(fixtures), "--out", str(tmp_path / "c")]) == 0
    run = next((tmp_path / "c").iterdir())
    assert regress.main(["compare", str(run), str(run)]) == 0
    assert "agree in every table" in capsys.readouterr().out
