"""Data-pack acceptance (BRD v0.5 BR-11.8, BR-06.21): the manifest names the pack and each file's type, ACC version and draft flag; a draft ACC prices
analysis runs, labelled draft, and is refused for a filing; the tie-out report checks the ACC's structure, its control totals against the source
workbook's, and reference measures priced by the engine and by tests/reference_calc.py."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from recet import datapack, paths, tieout
from recet.jobs import RunConfig, run_job
from test_chunked import _job


def acc_frame(years=range(2026, 2034), pairs=(("PG&E", "CZ13"), ("SCE", "CZ9"), ("SDG&E", "CZ7"))) -> pd.DataFrame:
    h = np.arange(1, 8761)
    frames = []
    for k, (iou, cz) in enumerate(pairs):
        for t, test in enumerate(("TRC", "SCT Base", "SCT High")):
            for y in years:
                energy = 40 + 10 * np.sin(2 * np.pi * h / 24 + k) + (y - 2026) + 3 * t
                frames.append(pd.DataFrame({"iou": iou, "climate_zone": cz, "cost_test": test, "year": y, "hour_of_year": h,
                                            "datetime": pd.Timestamp("2018-01-01") + pd.to_timedelta(h - 1, unit="h"), "energy": energy,
                                            "ghg_adder": 5.0 + t, "transmission": 1.0 + np.cos(2 * np.pi * h / 8760), "distribution": 2.0,
                                            "total_avoided_cost": energy + 5.0 + t + 1.0 + np.cos(2 * np.pi * h / 8760) + 2.0,
                                            "emission_rate_tonnes_per_kwh": 3e-4}))
    return pd.concat(frames, ignore_index=True)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A current reference folder with a 2024 ACC, built into a pack (manifest format 2) with the 2024 ACC as a draft."""
    src = tmp_path / "src"
    (src / "acc" / "2024").mkdir(parents=True)
    acc_frame().to_parquet(src / "acc" / "2024" / "electric_hourly.parquet", index=False)
    monkeypatch.setattr(paths, "REF_FILES", {k: v for k, v in paths.REF_FILES.items() if k == "acc_electric_2024"} | {
        k: paths.RefFile(v.key, v.rel, v.what, required=False) for k, v in paths.REF_FILES.items() if k != "acc_electric_2024"})
    paths.set_reference_dir(src)
    return datapack.build_reference_folder(tmp_path / "pack", pack_version="2026-10-30a", draft_accs=["2024"], log=lambda s: None)


def test_the_manifest_names_the_pack_and_its_files(folder):
    m = json.loads((folder / "manifest.json").read_text())
    assert m["format"] == 2 and m["pack"] == {"type": "reference", "version": "2026-10-30a"}
    f = m["files"]["acc/2024/electric_hourly.parquet"]
    assert f["type"] == "acc_electric_2024" and f["acc_version"] == "2024" and f["draft"] is True and len(f["sha256"]) == 64
    info = datapack.file_info(folder / "acc" / "2024" / "electric_hourly.parquet", folder)
    assert info["rel"] == "acc/2024/electric_hourly.parquet" and info["pack"]["version"] == "2026-10-30a"
    datapack.set_draft(folder, "2024", draft=False, log=lambda s: None)
    assert datapack.file_info(folder / "acc" / "2024" / "electric_hourly.parquet", folder)["draft"] is False
    with pytest.raises(ValueError, match="no ACC 2021"):
        datapack.set_draft(folder, "2021", log=lambda s: None)
    assert datapack.verify(folder, log=lambda s: None)


def _cfg(tmp_path, folder, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    m, pc = _job(tmp_path, n=4)
    from conftest import make_flat_profile_csv
    return RunConfig(measures=str(m), programcost=str(pc), out_dir=str(tmp_path / "out"), acc_version="2024", reference_dir=str(folder),
                     profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False, **kw)


def test_a_draft_acc_prices_analysis_runs_labelled_draft(tmp_path, folder):
    res = run_job(_cfg(tmp_path, folder), log=lambda s: None)
    assert res.frame["Used_ACCVersion"].str.endswith("(draft)").all()
    assert any("DRAFT ACC" in n for n in res.notes)
    settings = json.loads((res.run_dir / "run_settings.json").read_text())
    assert settings["acc"]["draft"] is True and settings["acc"]["label"].endswith("(draft)")


@pytest.mark.parametrize("option", [{"filing_validation": "claims"}, {"acc_purpose": "claims"}])
def test_a_draft_acc_is_refused_for_a_filing(tmp_path, folder, option):
    with pytest.raises(ValueError, match="DRAFT ACC"):
        run_job(_cfg(tmp_path, folder, **option), log=lambda s: None)


def test_a_supplied_acc_marked_draft(tmp_path, folder):
    datapack.set_draft(folder, "2024", draft=False, log=lambda s: None)
    f = tmp_path / "proposed.parquet"
    acc_frame().to_parquet(f, index=False)
    assert not run_job(_cfg(tmp_path / "a", folder, acc_electric_file=str(f)), log=lambda s: None).frame["Used_ACCVersion"].str.contains("draft").any()
    res = run_job(_cfg(tmp_path / "b", folder, acc_electric_file=str(f), acc_draft=True), log=lambda s: None)
    assert res.frame["Used_ACCVersion"].str.endswith("(draft)").all()
    with pytest.raises(ValueError, match="supplied file marked draft"):
        run_job(_cfg(tmp_path / "c", folder, acc_electric_file=str(f), acc_draft=True, filing_validation="claims"), log=lambda s: None)


def test_the_tie_out_passes_and_names_the_pack(tmp_path, folder):
    out = tmp_path / "report"
    pack_totals = tieout.control_totals(acc_frame())
    pack_totals.to_csv(tmp_path / "workbook_totals.csv", index=False)               # what the extraction computes from the workbook
    r = tieout.run(folder, "2024", tmp_path / "workbook_totals.csv", out, log=lambda s: None)
    assert r["status"] == "PASS", [p["problems"] for p in r["parts"]]
    assert [p["status"] for p in r["parts"]] == ["PASS", "PASS", "PASS"]
    assert r["pack"]["version"] == "2026-10-30a" and r["draft"] is True
    ref = r["parts"][2]["values"]
    assert {v["utility"] for v in ref} == {"PG&E", "SCE", "SDG&E"} and len(ref) == 3 * 2 * 3                 # 3 utilities x 2 measures x 3 tests
    assert all(v["engine"] > 0 for v in ref)
    assert (out / "tieout_acc2024.md").read_text().startswith("# Tie-out: the 2024 ACC (DRAFT)")
    assert (out / "control_totals_acc2024.csv").exists()


def test_the_tie_out_finds_problems(tmp_path, folder):
    wb = tieout.control_totals(acc_frame())
    wb.loc[5, "total_avoided_cost"] *= 1 + 1e-6
    wb.iloc[:-1].to_csv(tmp_path / "wb.csv", index=False)                            # one block missing too
    r = tieout.run(folder, "2024", tmp_path / "wb.csv", tmp_path / "r", log=lambda s: None)
    problems = r["parts"][1]["problems"]
    assert r["status"] == "FAIL" and any("only one of the two" in p for p in problems) and any(p.startswith("total_avoided_cost: 1 total(s) differ") for p in problems)
    bad = acc_frame()
    bad.loc[0, "energy"] += 1.0                                                      # components no longer add up
    s = tieout.structure(bad.drop(index=bad.index[-1]), "2024")                       # and one hour missing
    assert s["status"] == "FAIL" and any("hours 1-8,760" in p for p in s["problems"]) and any("add up" in p for p in s["problems"])
    assert tieout.run(folder, "2024", None, tmp_path / "r2", log=lambda s: None)["parts"][1]["status"] == "NOT RUN"
