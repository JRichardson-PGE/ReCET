"""The ACC-for-program-year check (BRD BR-06.5, TIER2_PLAN step 2): records priced on an ACC that does not normally apply to their program year are
flagged -- in preflight and run_settings.json -- and still priced."""
from __future__ import annotations

import json

import pytest

from recet import preflight, versions
from recet.jobs import RunConfig, run_job
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def test_which_accs_apply_to_a_program_year():
    # claims: the ACC of the year's final budget filing, one per year
    assert versions.expected_accs(2026, "claims") == {2024}
    assert versions.expected_accs(2028, "claims") == {2026}
    assert versions.expected_accs(2030, "claims") == {2028}
    assert versions.expected_accs(2036, "claims") is None                  # after CLAIMS_LAST_YEAR: not checked
    # any filing: every filing covering the year
    assert versions.expected_accs(2028) == {2024, 2026}
    assert versions.expected_accs(2030) == {2024, 2026, 2028}
    assert versions.expected_accs(2019) is None                            # before the first filing in the table
    # one filing: its ACC within its program years, nothing outside them
    assert versions.expected_accs(2028, "2028-2035-application") == {2024}
    assert versions.expected_accs(2029, "true-up-advice-letter-september-2027") == {2026}
    assert versions.expected_accs(2027, "2028-2035-application") == set()
    with pytest.raises(ValueError):
        versions.expected_accs(2028, "no-such-filing")


def test_every_filing_has_a_key_and_a_purpose_label():
    keys = [k for k, _ in versions.acc_purposes()]
    assert keys[:2] == ["", "claims"] and set(keys[2:]) == set(versions.FILINGS) and len(versions.FILINGS) == len(versions.BUDGET_FILING_ACC)
    assert "2028-2035-application" in versions.FILINGS


def test_the_acc_year_of_a_version_key():
    assert versions.acc_year_of("2026") == 2026 and versions.acc_year_of("2030") == 2030 and versions.acc_year_of("2026-draft") is None


def _two_measures(tmp_path):
    a = make_measure_csv(tmp_path / "a.csv", CEInputID="ON_RUN", ACCVersion="").read_text(encoding="utf-8").splitlines()
    b = make_measure_csv(tmp_path / "b.csv", CEInputID="ON_2024", ACCVersion="2024").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "Measure.csv"
    path.write_text("\n".join([a[0], a[1], b[1]]) + "\n", encoding="utf-8")          # both claimed in 2028
    return path


def _reference(tmp_path):
    ref = tmp_path / "ref"
    for year in (2026, 2024):
        (ref / "acc" / str(year)).mkdir(parents=True)
        make_electric_acc(ref / "acc" / str(year) / "electric_hourly.parquet")
    return ref


def _run(tmp_path, purpose, **kw):
    return run_job(RunConfig(measures=str(_two_measures(tmp_path)), out_dir=str(tmp_path / "out"), run_name=purpose or "any", acc_version="2026",
                             reference_dir=str(_reference(tmp_path)), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False,
                             acc_purpose=purpose, **kw), log=lambda s: None)


def test_claims_flag_the_measure_on_an_acc_that_does_not_apply_and_still_price_it(tmp_path):
    r = _run(tmp_path, "claims")
    f = r.frame.set_index("CEInputID")
    assert "ACCCheck" not in f.columns                                       # the check is in preflight and run_settings, not the measure table
    assert f.loc["ON_2024", "ElecBen"] > 0                                   # flagged, not stopped
    assert r.acc_check["flagged"] == 1 and r.acc_check["measures"] == 2 and r.acc_check["label"] == "Quarterly Claims"
    rows = {(x["acc"], x["program_year"]): x for x in r.acc_check["by_acc_and_year"]}
    assert rows[(2024, 2028)]["status"] == "flagged" and rows[(2024, 2028)]["expected_accs"] == [2026] and rows[(2026, 2028)]["status"] == "ok"
    settings = json.loads((r.run_dir / "run_settings.json").read_text(encoding="utf-8"))
    assert settings["acc_check"]["flagged"] == 1 and settings["config"]["acc_purpose"] == "claims"
    text = (r.run_dir / "preflight.txt").read_text(encoding="utf-8")
    assert "acc_not_for_program_year" in text and "ACC check: Quarterly Claims: 1 of 2 measures" in text


def test_any_filing_accepts_both_accs_of_2028(tmp_path):
    r = _run(tmp_path, "")
    assert r.acc_check["flagged"] == 0


def test_a_named_filing_flags_the_other_acc_and_points_at_the_cells(tmp_path):
    rep = preflight.check_inputs(_two_measures(tmp_path), acc=versions.resolve_acc("2026", tmp_path / "e.parquet", None), acc_purpose="2028-2035-application")
    hit = [i for i in rep.issues if i.code == "acc_not_for_program_year"]
    assert len(hit) == 1 and hit[0].count == 1 and "the 2026 ACC" in hit[0].message and "uses the 2024 ACC" in hit[0].message
    assert hit[0].cells == [("Measure", 0, "ClaimYearQuarter")]             # the run's own ACC: the program year is what is off
    assert hit[0].severity == "warning"                                      # flagged, never a stop
    named = [i for i in rep.issues if i.code == "acc_not_for_program_year" and i.cells and i.cells[0][2] == "ACCVersion"]
    assert not named                                                          # ON_2024 names the 2024 ACC, which this filing uses


def test_the_check_adds_up_over_parts(tmp_path):
    lines = make_measure_csv(tmp_path / "a.csv").read_text(encoding="utf-8").splitlines()
    rows = [lines[1].replace("T1", f"T{i}", 1).replace("2028Q1", "2031Q1" if i % 2 else "2028Q1") for i in range(7)]
    path = tmp_path / "M.csv"
    path.write_text("\n".join([lines[0], *rows]) + "\n", encoding="utf-8")
    acc = versions.resolve_acc("2026", tmp_path / "e.parquet", None)
    whole = preflight.check_inputs(path, acc=acc, acc_purpose="claims")
    parts = preflight.check_inputs(path, acc=acc, acc_purpose="claims", chunk_size=3)
    assert whole.acc_check == parts.acc_check and whole.acc_check["flagged"] == 3        # the three 2031 claims: their final filing uses the 2028 ACC
    assert whole.stats["ACC check"] == parts.stats["ACC check"]
