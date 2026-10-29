"""The CET replicas against their releases' own RunCET runs (tests/data/cet_versions, built by CEDARS-Research
poc/cet_version_fixtures.py from the local restores of each release's database backup).

A replica passes only when every output column of every measure equals RunCET's to 1e-9 (relative; 1e-15 absolute near 0)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from recet import cet_versions
from recet.cet_versions import QuarterlyTables

DATA = Path(__file__).parent / "data" / "cet_versions"
BUILT = [r for r in cet_versions.RELEASES]
RUNS = sorted(p for p in DATA.glob("*/*/me*") if p.parts[-3] in BUILT)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8")


def differences(ours: pd.DataFrame, ref: pd.DataFrame, rtol: float = 1e-9) -> list[str]:
    ref = ref.drop(columns=[c for c in ("ID", "JobID") if c in ref.columns])
    out = []
    if list(ours.columns) != list(ref.columns):
        out.append(f"columns differ: ours {list(ours.columns)} ref {list(ref.columns)}")
        return out
    m = ours.merge(ref, on="CET_ID", how="outer", suffixes=("", "_ref"), indicator=True)
    if (m._merge != "both").any():
        out.append(f"measures differ: only ours {list(m.CET_ID[m._merge == 'left_only'])}, only CET's {list(m.CET_ID[m._merge == 'right_only'])}")
    m = m[m._merge == "both"].reset_index(drop=True)
    for c in ref.columns:
        if c == "CET_ID":
            continue
        a, b = m[c], m[c + "_ref"]
        if c in ("PA", "PrgID"):
            bad = a.astype(str).str.rstrip().str.casefold() != b.astype(str).str.rstrip().str.casefold()
        else:
            a, b = pd.to_numeric(a), pd.to_numeric(b)
            bad = (a.isna() != b.isna()) | ((a - b).abs() > np.maximum(b.abs() * rtol, 1e-15))
        if bad.any():
            i = bad.idxmax()
            out.append(f"{c}: {int(bad.sum())} measures, e.g. {m.CET_ID[i]} ours {a[i]!r} CET {b[i]!r}")
    return out


@pytest.mark.parametrize("run", RUNS, ids=lambda p: "/".join(p.parts[-3:]))
def test_replica_equals_runcet(run):
    params = json.loads((run / "params.json").read_text())
    tables = QuarterlyTables.from_folder(run.parent / "tables")
    res = cet_versions.run(_read(run / "measure.csv"), _read(run / "programcost.csv"), params["release"], tables,
                           first_year=int(params["FirstYear"]), me_bens=float(params["MEBens"]), me_cost=float(params["MECost"]))
    problems = {name: differences(df, _read(run / f"{name}.csv")) for name, df in res.tables().items()}
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, json.dumps(problems, indent=1)


@pytest.mark.parametrize("run", RUNS, ids=lambda p: "/".join(p.parts[-3:]))
def test_replica_in_chunks_equals_runcet(run):
    """The same runs priced 7 measures at a time (a large job's chunking): still RunCET's outputs to 1e-9, in the same order."""
    params = json.loads((run / "params.json").read_text())
    tables = QuarterlyTables.from_folder(run.parent / "tables")
    args = (_read(run / "measure.csv"), _read(run / "programcost.csv"), params["release"], tables)
    kw = dict(first_year=int(params["FirstYear"]), me_bens=float(params["MEBens"]), me_cost=float(params["MECost"]))
    res = cet_versions.run(*args, **kw, chunk_size=7)
    problems = {name: differences(df, _read(run / f"{name}.csv")) for name, df in res.tables().items()}
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, json.dumps(problems, indent=1)
    whole = cet_versions.run(*args, **kw)
    for name, df in res.tables().items():
        assert list(df.CET_ID) == list(whole.tables()[name].CET_ID), name


def test_every_release_has_reference_runs():
    for r in cet_versions.RELEASES:
        assert any(p.parts[-3] == r for p in RUNS), r


def test_from_release_reads_the_export(tmp_path):
    """A real run's tables: the shipped small tables plus the parquet export (here written from a fixture's slices)."""
    run = DATA / "25.1" / "C3_avc2026" / "me0"
    t = run.parent / "tables"
    names = {"ac_elec": "E3AvoidedCostElecCO2Seq2026", "ac_gas": "E3AvoidedCostGasSeq2026", "emissions": "E3Emissions",
             "rates_elec": "E3RateScheduleElec", "rates_gas": "E3RateScheduleGas"}
    (tmp_path / "tables").mkdir()
    rows = ["db,release,table,rows,columns,sha8,file,bytes"]
    for slice_, table in names.items():
        df = _read(t / f"{slice_}.csv")
        for c in df.columns:
            if c not in ("PA", "Version", "TS", "EU", "CZ", "Qtr", "DSType", "ACElecKey", "ACKey", "GS", "GP", "ComboField", "ACGasKey", "Schedule"):
                df[c] = pd.to_numeric(df[c])
        if slice_ == "ac_elec":   # a PA the job does not have: filtered out while the parquet is read
            df = pd.concat([df, df.head(3).assign(PA="ZZ")], ignore_index=True)
        df.to_parquet(tmp_path / "tables" / f"{table}.parquet")
        rows.append(f"x,25.1,{table},{len(df)},{df.shape[1]},0,tables/{table}.parquet,0")
    (tmp_path / "manifest.csv").write_text("\n".join(rows) + "\n")
    measures, programs = _read(run / "measure.csv"), _read(run / "programcost.csv")
    tables = QuarterlyTables.from_release("25.1", "2026", tmp_path, measures=measures)
    assert tables.base_year == 2024 and tables.max_qac_elec == 123 and "ZZ" not in set(tables.ac_elec.PA)
    params = json.loads((run / "params.json").read_text())
    res = cet_versions.run(measures, programs, "25.1", tables, first_year=int(params["FirstYear"]))
    problems = {name: differences(df, _read(run / f"{name}.csv")) for name, df in res.tables().items()}
    assert not {k: v for k, v in problems.items() if v}


def test_cli_run_writes_the_tables(tmp_path):
    from recet import cli
    run = DATA / "24.3" / "synthetic_avc2024" / "me0.05"
    with pytest.raises(SystemExit) as done:
        cli.main(["cet-version", "run", "--release", "24.3", "--tables", str(run.parent / "tables"), "--measures", str(run / "measure.csv"),
                  "--programcost", str(run / "programcost.csv"), "--me-bens", "0.05", "--me-cost", "0.05", "--out", str(tmp_path)])
    assert done.value.code == 0
    for name in cet_versions.TABLES.values():
        assert not differences(_read(tmp_path / f"{name}.csv"), _read(run / f"{name}.csv")), name
    assert json.loads((tmp_path / "run.json").read_text())["acc"] == 2022


def test_cli_run_in_parts(tmp_path):
    """--part-size: the job read and priced a few measures at a time (parts.plan + price), the tables still RunCET's."""
    from recet import cli
    run = DATA / "25.0" / "C3_avc2026" / "me0"
    with pytest.raises(SystemExit) as done:
        cli.main(["cet-version", "run", "--release", "25.0", "--tables", str(run.parent / "tables"), "--measures", str(run / "measure.csv"),
                  "--programcost", str(run / "programcost.csv"), "--first-year", "2028", "--part-size", "9", "--out", str(tmp_path)])
    assert done.value.code == 0
    for name in cet_versions.TABLES.values():
        assert not differences(_read(tmp_path / f"{name}.csv"), _read(run / f"{name}.csv")), name
    assert json.loads((tmp_path / "run.json").read_text())["parts"] == 5 and not (tmp_path / "_parts").exists()


def test_versions_not_offered_are_refused(tmp_path):
    from recet.cet_versions import QuarterlyTables
    for release, version in [("21.0", "2021"), ("25.1", "2027")]:
        with pytest.raises(ValueError, match="not offered"):
            QuarterlyTables.from_release(release, version, tmp_path)
