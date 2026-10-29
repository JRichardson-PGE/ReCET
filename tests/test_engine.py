"""The calculation engine of a run (D13, BR-06.4): ReCET's own, or a CET replica through the ordinary run path (jobs.run_job, `recet run --engine`, the web
app's New run), its outputs readable by the Results and Compare pages, and what a replica refuses (a filing accepts only ReCET's own engine)."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from recet import results_view as rv
from recet.cet_versions import job as cet_job
from recet.jobs import RunConfig, run_job
from test_cet_versions import DATA, _read, differences

C3 = "C3_avc2026"
TEXT = ("PA", "Version", "TS", "EU", "CZ", "Qtr", "DSType", "ACElecKey", "ACKey", "GS", "GP", "ComboField", "ACGasKey", "Schedule")
SLICES = {"ac_elec": "E3AvoidedCostElecCO2Seq2026", "ac_gas": "E3AvoidedCostGasSeq2026", "emissions": "E3Emissions",
          "rates_elec": "E3RateScheduleElec", "rates_gas": "E3RateScheduleGas"}


def reference_folder(tmp_path: Path, releases=("25.1", "25.0")) -> Path:
    """A reference data folder whose cet_versions export holds the C3 job's table slices of each release (the layout of the real export)."""
    ex = tmp_path / "ref" / "cet_versions"
    rows = ["db,release,table,rows,columns,sha8,file,bytes"]
    for rel in releases:
        (ex / "tables" / rel).mkdir(parents=True, exist_ok=True)
        for sl, table in SLICES.items():
            df = _read(DATA / rel / C3 / "tables" / f"{sl}.csv")
            for c in df.columns:
                if c not in TEXT:
                    df[c] = pd.to_numeric(df[c])
            df.to_parquet(ex / "tables" / rel / f"{table}.parquet")
            rows.append(f"x,{rel},{table},{len(df)},{df.shape[1]},0,tables/{rel}/{table}.parquet,0")
    (ex / "manifest.csv").write_text("\n".join(rows) + "\n")
    return ex.parent


def _cfg(tmp_path, release="25.1", **kw) -> RunConfig:
    run = DATA / release / C3 / "me0"
    kw.setdefault("measures", str(run / "measure.csv"))
    return RunConfig(programcost=str(run / "programcost.csv"), out_dir=str(tmp_path / "out"), acc_version="2024",
                     engine=f"cet-{release}", reference_dir=str(reference_folder(tmp_path)), base_year=2028, run_name=f"r{release}", **kw)


def test_a_replica_runs_through_the_ordinary_run_path(tmp_path):
    res = run_job(_cfg(tmp_path), log=lambda s: None)
    run = DATA / "25.1" / C3 / "me0"
    for name in ("SavedCE", "SavedSavings", "SavedEmissions", "SavedCost"):          # the release's own tables, equal to its RunCET run
        assert not differences(_read(res.files[name]), _read(run / f"{name}.csv")), name
    frame = pd.read_csv(res.files["measure_level"])
    assert len(frame) == 41 and frame["Used_ACCVersion"].eq("2024 ACC").all()
    ce = _read(run / "SavedCE.csv").set_index("CET_ID")
    m = frame.set_index(frame["CEInputID"].astype(str))
    assert (m["ElecBen"] - pd.to_numeric(ce["ElecBen"]).reindex(m.index)).abs().max() < 1e-6     # Saved* columns under ReCET's names
    assert "FirstYearNetElecCO2" in frame and "NetElecCO2" not in frame                         # CET's NetElecCO2, renamed as ReCET writes it
    assert "Prg_ProgramName" in frame
    settings = json.loads((res.run_dir / "run_settings.json").read_text())
    assert settings["engine"]["key"] == "cet-25.1" and settings["engine"]["label"] == "CET 25.1 replica" and settings["engine"]["cet_version"] == "2026"
    assert settings["config"]["engine"] == "cet-25.1"
    sums = rv.RunResults.from_file(Path(res.files["measure_level"]))                           # the Results page's tables
    port = sums.table("portfolio", rv.ALL_PAS, ["TotalSystemBenefit", "TRC_ratio"])
    assert port["Measures"].iloc[0] == 41 and abs(port["TotalSystemBenefit"].iloc[0] - res.totals["TotalSystemBenefit"]) < 1e-3


def test_a_replica_run_in_parts(tmp_path):
    """A job larger than a part is priced part by part (cet_versions.price via parts.plan): the release's tables still equal RunCET's, the
    measure-level table has every measure, and the totals equal a whole-job run's."""
    whole = run_job(_cfg(tmp_path / "w"), log=lambda s: None)
    res = run_job(_cfg(tmp_path / "p", replica_part_size=7), log=lambda s: None)
    run = DATA / "25.1" / C3 / "me0"
    for name in ("SavedCE", "SavedSavings", "SavedEmissions", "SavedCost"):
        assert not differences(_read(res.files[name]), _read(run / f"{name}.csv")), name
    frame = pd.read_csv(res.files["measure_level"])
    assert len(frame) == 41 and set(frame.CEInputID) == set(pd.read_csv(whole.files["measure_level"]).CEInputID)
    assert res.frame is None and any("parts" in n for n in res.notes)
    for k, v in whole.totals.items():
        assert abs(res.totals[k] - v) <= 1e-9 * max(abs(v), 1.0), k
    assert not (res.run_dir / "_parts").exists()


def test_a_replica_keeps_a_repeated_ceinputid_together(tmp_path):
    """Rows sharing a CEInputID in different parts are priced together (CET groups a CET_ID's rows), as in a whole-job run. (CET's joins on CET_ID
    multiply such rows, as the replica does: three rows sharing one ID already make millions of quarterly rows; CEDARS IDs are unique.)"""
    m = pd.read_csv(DATA / "25.1" / C3 / "me0" / "measure.csv", dtype=str, keep_default_na=False)
    m.loc[[2, 35], "CEInputID"] = "SHARED"
    p = tmp_path / "m.csv"
    m.to_csv(p, index=False)
    a = run_job(_cfg(tmp_path / "w", measures=str(p)), log=lambda s: None)
    b = run_job(_cfg(tmp_path / "p", measures=str(p), replica_part_size=7), log=lambda s: None)
    for name in ("SavedCE", "SavedSavings", "SavedEmissions", "SavedCost"):     # SHARED has a row per (PA, PrgID), as in CET
        x, y = (pd.read_csv(r.files[name]).sort_values(["CET_ID", "PA", "PrgID"]).reset_index(drop=True) for r in (b, a))
        pd.testing.assert_frame_equal(x, y, rtol=1e-9, check_dtype=False, obj=name)


def test_two_engines_side_by_side(tmp_path):
    a = run_job(_cfg(tmp_path, "25.1"), log=lambda s: None)
    b = run_job(_cfg(tmp_path, "25.0"), log=lambda s: None)
    t = rv.compare(rv.RunResults.from_file(Path(a.files["measure_level"])), rv.RunResults.from_file(Path(b.files["measure_level"])), "portfolio",
                   ["ElecBen", "TRCCost"])
    ce_a, ce_b = (pd.to_numeric(_read(DATA / r / C3 / "me0" / "SavedCE.csv")["ElecBen"]).sum() for r in ("25.1", "25.0"))
    assert abs(t["ElecBen__a"].iloc[0] - ce_a) < 1e-3 and abs(t["ElecBen__b"].iloc[0] - ce_b) < 1e-3
    assert abs(t["ElecBen__diff"].iloc[0] - (ce_b - ce_a)) < 1e-3


@pytest.mark.parametrize("option, value, text", [
    ("filing_validation", "claims", "filing"), ("preflight_only", True, "Check inputs only"), ("allocate_statewide", True, "statewide allocation"),
    ("acc_electric_file", "x.parquet", "electric ACC file"), ("compare_with", "2022", "comparison with another ACC"),
])
def test_a_replica_refuses_recet_options(tmp_path, option, value, text):
    cfg = _cfg(tmp_path, **{option: value})
    assert any(text in p for p in cet_job.problems(cfg))
    with pytest.raises(ValueError, match=text):
        run_job(cfg, log=lambda s: None)


def test_engines_and_their_accs():
    assert cet_job.release_of("") is None and cet_job.release_of("recet") is None and cet_job.release_of("CET-24.3").name == "24.3"
    with pytest.raises(ValueError):
        cet_job.release_of("cet-9.9")
    assert cet_job.engines()[0][0] == "recet" and [k for k, _ in cet_job.engines()][1:] == [f"cet-{r}" for r in cet_job.RELEASES]
    assert list(cet_job.accs(cet_job.RELEASES["21.0"])) == [2017, 2018, 2019]                  # Version 2021 is not offered
    assert 2026 not in cet_job.accs(cet_job.RELEASES["25.1"]) and 2024 in cet_job.accs(cet_job.RELEASES["25.1"])
    assert cet_job.problems(RunConfig(engine="cet-21.0", acc_version="2024")) == ["the CET 21.0 replica has no 2024 ACC; it has the 2017, 2018, 2019 ACCs"]
    assert cet_job.problems(RunConfig(engine="", acc_version="2024", filing_validation="claims")) == []


def test_cli_engine(tmp_path):
    from recet import cli
    cfg = _cfg(tmp_path)
    with pytest.raises(SystemExit) as done:
        cli.main(["run", "--engine", "cet-25.1", "--acc", "2024", "--measures", cfg.measures, "--programcost", cfg.programcost, "--base-year", "2028",
                  "--data-dir", cfg.reference_dir, "--out", str(tmp_path / "cli"), "--name", "c"])
    assert done.value.code in (0, None)
    settings = json.loads((tmp_path / "cli" / "c_acc2024" / "run_settings.json").read_text())
    assert settings["engine"]["key"] == "cet-25.1"


# ------------------------------------------------------------------------------------------------------------------------------------- web app
def test_new_run_engine_choice(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("argon2")
    from recet.web.app import create_app
    from recet.web.config import Settings
    from test_web import P, _csrf, _login, _user
    from conftest import make_electric_acc, make_flat_profile_csv
    s = Settings(state_dir=tmp_path / "web", start_workers=False, secure_cookies=False, trust_proxy=False,
                 run_defaults={"acc_electric_file": str(make_electric_acc(tmp_path / "acc.parquet")),
                               "profiles_8760_file": str(make_flat_profile_csv(tmp_path / "p.csv")),
                               "reference_dir": str(reference_folder(tmp_path))})
    app = create_app(s)
    ctx = app.state.ctx
    _user(ctx, "eng@example.com")
    c = _login(app, "eng@example.com")
    page = c.get(f"{P}/runs/new").text
    assert 'name="engine"' in page and "CET 25.1 replica" in page and "Which ACCs each engine has" in page
    assert 'id="engine-label"' in page and 'aria-describedby="engine-help"' in page                     # a visible label and its help (WCAG)
    run = DATA / "25.1" / C3 / "me0"
    job = ("C3.zip", _zipped({"Measure.csv": (run / "measure.csv").read_bytes(), "ProgramCost.csv": (run / "programcost.csv").read_bytes()}),
           "application/zip")

    def post(**form):
        data = {"csrf": _csrf(c), "acc_version": "2024", "input_mode": "files", "market_effects": "0", "output_format": "csv", "base_year": "2028", **form}
        return c.post(f"{P}/runs", data=data, files={"job_zip": job})
    r = post(engine="cet-25.1", filing_validation="claims")                                         # a filing accepts only ReCET's own engine
    assert r.status_code == 400 and "filing" in r.text
    r = post(engine="cet-21.0")
    assert r.status_code == 400 and "has no 2024 ACC" in r.text
    assert post(engine="cet-9.9").status_code == 400
    r = post(engine="cet-25.1", label="replica run")
    assert r.status_code == 303, r.text
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    assert json.loads((ctx.runner.folder(run_id) / "config.json").read_text())["acc_electric_file"] in (None, "")   # the server's test ACC is not passed on
    assert ctx.runner.work_once()
    row = ctx.db.one("SELECT status FROM runs WHERE id = ?", (run_id,))
    assert row["status"] == "finished", (ctx.runner.folder(run_id) / "log.txt").read_text()
    page = c.get(f"{P}/runs/{run_id}").text
    assert "CET 25.1 replica" in page
    assert c.get(f"{P}/runs/{run_id}/results?level=program").status_code == 200
    # ReCET's own engine on the same server, then the two side by side
    from conftest import make_measure_csv
    r = c.post(f"{P}/runs", data={"csrf": _csrf(c), "acc_version": "2026", "input_mode": "files", "market_effects": "0", "output_format": "csv",
                                  "engine": "recet"}, files={"job_zip": ("j.zip", _zipped({"Measure.csv": make_measure_csv(tmp_path / "M.csv").read_bytes()}),
                                                                         "application/zip")})
    assert r.status_code == 303, r.text
    other = r.headers["location"].rsplit("/", 1)[-1]
    assert ctx.runner.work_once()
    cmp_ = c.get(f"{P}/runs/{run_id}/compare?with={other}").text
    assert "CET 25.1 replica" in cmp_ and re.search(r"ReCET [0-9.]+ \(hourly\)", cmp_)


def _zipped(members: dict[str, bytes]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_a_cedars_upload_runs_on_a_replica(tmp_path):
    """A CEDARS Measure.csv (no PA; FuelID for CET's FuelType) prices as CEDARS hands it to CET: each record's PA is its IOU_AC_Territory and a
    non-IOU program's ProgramCost rows are SCE's."""
    run = DATA / "25.1" / C3 / "me0"
    m, p = _read(run / "measure.csv"), _read(run / "programcost.csv")
    sw = set(p.loc[p.PA.str.strip().str.upper() == "SW", "PrgID"])
    assert sw and m.PrgID.isin(sw).any()                                                        # the C3 job has statewide records
    up = m.drop(columns=["JobID"]).rename(columns={"PA": "IOU_AC_Territory", "FuelType": "FuelID"})
    (tmp_path / "up").mkdir()
    up.to_csv(tmp_path / "up" / "Measure.csv", sep="|", index=False)
    p.drop(columns=["ID", "JobID"]).to_csv(tmp_path / "up" / "ProgramCost.csv", sep="|", index=False)
    cfg = _cfg(tmp_path)
    cfg.measures, cfg.programcost = str(tmp_path / "up" / "Measure.csv"), str(tmp_path / "up" / "ProgramCost.csv")
    res = run_job(cfg, log=lambda s: None)
    # CEDARS passes a statewide program's ProgramCost rows to CET as SCE's (the published CET's own program table of job 122714); the reference run
    # kept PA SW. So the records of the other programs equal RunCET's, and the job as a whole equals RunCET on the table CEDARS passes
    from recet import cet_versions
    from recet.cet_versions import QuarterlyTables
    want = cet_versions.run(m, p.assign(PA=p.PA.where(p.PA.str.strip().str.upper() != "SW", "SCE")), "25.1",
                            QuarterlyTables.from_folder(run.parent / "tables"), first_year=2028)
    sw_ids = set(m.CEInputID[m.PrgID.isin(sw)])
    for name in ("SavedCE", "SavedSavings", "SavedEmissions", "SavedCost"):
        ours = _read(res.files[name])
        assert not differences(ours, want.tables()[name].astype(str).replace("nan", "")), name
        ref = _read(run / f"{name}.csv")
        assert not differences(ours[~ours.CET_ID.isin(sw_ids)].reset_index(drop=True), ref[~ref.CET_ID.isin(sw_ids)].reset_index(drop=True)), name
    assert any("on their IOU_AC_Territory" in n for n in res.notes) and any("costed as SCE" in n for n in res.notes)
    frame = pd.read_csv(res.files["measure_level"])
    assert "IOU_AC_Territory" in frame and len(frame) == len(m)
    assert frame["PA"].notna().all()                                                            # the PA each record was priced on (SavedCE)


def test_cet_inputs_pa():
    m = pd.DataFrame({"PrgID": ["PGE_1", "SW_X", "SCE_2", "SW_Y", "MCE01", "PGE_1"], "IOU_AC_Territory": ["PGE", "SCE", "SCE", None, "PGE", ""],
                      "FuelID": [None] * 6})
    p = pd.DataFrame({"PA": ["PGE", "SW", "MCE"], "PrgID": ["pge_1 ", "SW_X", "MCE01"]})
    out, n = cet_job.cet_inputs(m, p)
    # the territory whenever there is one (CEDARS, jobs 122714 / 122716 / 122738), else the program's PA, else the PrgID's prefix
    assert list(out.PA) == ["PGE", "SCE", "SCE", "SW", "PGE", "PGE"] and n == 2 and "FuelType" in out and "FuelID" not in out
    q, k = cet_job.cet_programs(p.assign(PA=["PGE", " sw ", "BAY"]))
    assert list(q.PA) == ["PGE", "SCE", "SCE"] and k == 2 and list(p.PA) == ["PGE", "SW", "MCE"]   # every non-IOU PA; the caller's table not changed
