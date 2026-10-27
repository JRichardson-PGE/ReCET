"""The minimal input set (BRD BR-06.15, TIER2_PLAN step 4): an exploratory run needs only the Measure.csv columns that change a result. A file with only
those columns prices every test without errors; a missing descriptive column is a warning (filled blank), a missing calculation column an error."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import editor, preflight, templates
from recet.jobs import RunConfig, run_job
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def _only(tmp_path, columns):
    """The conftest measure, keeping only `columns`."""
    df = pd.read_csv(make_measure_csv(tmp_path / "full.csv", UnitMeaCost1stBaseline="500", UnitIncentiveToOthers="100"), sep="|", dtype=str,
                     keep_default_na=False)
    path = tmp_path / "Measure.csv"
    df[[c for c in df.columns if c in columns]].to_csv(path, sep="|", index=False)
    return path


def test_the_descriptive_columns_are_the_required_ones_that_change_nothing():
    effects = editor.EFFECTS["Measure"]
    derived = [c for c in preflight.REQUIRED_MEASURE_COLUMNS if c not in preflight.KEY_COLUMNS and effects.get(c, (None, ""))[0] is None]
    assert list(templates.DESCRIPTIVE_MEASURE_COLUMNS) == derived == ["MeasDescription", "Sector"]
    assert set(preflight.CALCULATION_MEASURE_COLUMNS) == set(preflight.REQUIRED_MEASURE_COLUMNS) - set(templates.DESCRIPTIVE_MEASURE_COLUMNS)


def test_a_file_with_only_calculation_columns_prices_every_test(tmp_path):
    path = _only(tmp_path, preflight.CALCULATION_MEASURE_COLUMNS)
    rep = preflight.check_inputs(path)
    assert rep.ok
    assert {i.code for i in rep.issues if i.severity == "warning"} >= {"missing_descriptive_column"}
    ref = tmp_path / "ref"
    (ref / "acc" / "2026").mkdir(parents=True)
    make_electric_acc(ref / "acc" / "2026" / "electric_hourly.parquet")
    r = run_job(RunConfig(measures=str(path), out_dir=str(tmp_path / "out"), run_name="minimal", acc_version="2026", reference_dir=str(ref),
                          profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False), log=lambda s: None)
    row = r.frame.iloc[0]
    for col in ("TotalSystemBenefit", "TRCRatio", "PACRatio", "RIMRatio", "SCBRatio", "SCHRatio"):
        assert pd.notna(row[col]) and row[col] != 0, col
    assert row["MeasDescription"] == "" and row["Sector"] == ""            # defaulted blank, still in the output


def test_the_minimal_file_gives_the_same_results_as_the_full_one(tmp_path):
    ref = tmp_path / "ref"
    (ref / "acc" / "2026").mkdir(parents=True)
    make_electric_acc(ref / "acc" / "2026" / "electric_hourly.parquet")
    prof = str(make_flat_profile_csv(tmp_path / "p.csv"))
    out = {}
    for name, cols in (("full", None), ("minimal", preflight.CALCULATION_MEASURE_COLUMNS)):
        d = tmp_path / name
        d.mkdir()
        path = _only(d, cols) if cols else make_measure_csv(d / "Measure.csv", UnitMeaCost1stBaseline="500", UnitIncentiveToOthers="100")
        out[name] = run_job(RunConfig(measures=str(path), out_dir=str(d / "out"), run_name=name, acc_version="2026", reference_dir=str(ref),
                                      profiles_8760_file=prof, emissions=False), log=lambda s: None).frame.iloc[0]
    for col in ("ElecBen", "TotalSystemBenefit", "TRCCost", "TRCRatio", "PACRatio"):
        assert out["minimal"][col] == pytest.approx(out["full"][col], rel=1e-12), col


def test_a_missing_calculation_column_is_an_error_that_says_so(tmp_path):
    path = _only(tmp_path, [c for c in preflight.CALCULATION_MEASURE_COLUMNS if c != "EUL_Yrs"])
    rep = preflight.check_inputs(path)
    hit = [i for i in rep.issues if i.code == "missing_columns"]
    assert not rep.ok and len(hit) == 1 and "EUL_Yrs" in hit[0].message and "change the results" in hit[0].message


def test_a_minimal_file_runs_in_parts(tmp_path):
    one = pd.read_csv(_only(tmp_path, preflight.CALCULATION_MEASURE_COLUMNS), sep="|", dtype=str, keep_default_na=False)
    many = pd.concat([one.assign(CEInputID=f"M{i}") for i in range(5)], ignore_index=True)
    path = tmp_path / "Many.csv"
    many.to_csv(path, sep="|", index=False)
    ref = tmp_path / "ref"
    (ref / "acc" / "2026").mkdir(parents=True)
    make_electric_acc(ref / "acc" / "2026" / "electric_hourly.parquet")
    from recet import outputs
    r = run_job(RunConfig(measures=str(path), out_dir=str(tmp_path / "out"), run_name="parts", acc_version="2026", reference_dir=str(ref),
                          profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False, chunk_size=2), log=lambda s: None)
    out = outputs.read_table(r.files["measure_level"])
    assert len(out) == 5 and out["TotalSystemBenefit"].gt(0).all()
