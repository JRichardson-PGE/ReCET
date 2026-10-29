"""`recet cet-version`: price a job as a prior CET release does.

    recet cet-version list
    recet cet-version run --release 24.3 --acc 2022 --measures Measure.csv [--programcost ProgramCost.csv] --out results
                          [--first-year 2024] [--me-bens 0.05 --me-cost 0.05] [--export-dir FOLDER | --tables FOLDER]

The output folder gets the release's own four output tables (SavedCE.csv, SavedSavings.csv, SavedEmissions.csv,
SavedCost.csv, CET's columns in CET's order) and run.json (the release, its label and the run's parameters). A job is priced
--part-size measures at a time (default 10,000), so a 1,000,000-row job needs about the memory of a 10,000-row one; the
tables are then in CET_ID order within each part.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .. import versions


def _acc_of(cet_version: str) -> int | None:
    try:
        return versions.CET_VERSION_TO_ACC.get(int(float(cet_version)))
    except ValueError:
        return None


def _cet_version(a) -> str:
    if a.cet_version:
        return str(a.cet_version)
    by_acc = {acc: v for v, acc in versions.CET_VERSION_TO_ACC.items()}
    if int(a.acc) not in by_acc:
        raise SystemExit(f"no CET avoided-cost Version prices the {a.acc} ACC; known ACCs: {', '.join(map(str, sorted(by_acc)))}")
    return str(by_acc[int(a.acc)])


def _read(path: str) -> pd.DataFrame:
    from .. import fieldnames
    from ..pipeline.delimited import read_cet_csv
    return fieldnames.to_cet(read_cet_csv(path, dtype=str, keep_default_na=False, na_values=[""]))


def cmd_list(a) -> int:
    from importlib import resources
    from . import RELEASES
    for r in RELEASES.values():
        with resources.files("recet.data").joinpath(f"cet_versions/{r.name}/CETAvoidedCostVersions.csv").open(encoding="utf-8") as f:
            v = pd.read_csv(f, dtype=str)
        skip = dict(r.not_offered)
        accs = ", ".join(f"{_acc_of(x)} ACC" if _acc_of(x) else f"Version {x}" for x in sorted(v.Version, key=float) if x not in skip)
        accs += "".join(f"\n      not offered: Version {x} ({why})" for x, why in r.not_offered)
        print(f"{r.name:5} {r.label:20} backup {r.built} ({r.backup})\n      ACCs: {accs}")
    return 0


def cmd_run(a) -> int:
    import shutil
    from . import QuarterlyTables, get, price
    from .parts import PART_SIZE, plan
    rel = get(a.release)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    programs = _read(a.programcost) if a.programcost else pd.DataFrame(columns=["PA", "PrgID", "ClaimYearQuarter"])
    from .job import cet_inputs

    def read(f):                                  # a CEDARS upload has no PA: each record takes its program's (job.cet_inputs)
        return cet_inputs(_read(f), programs)[0]

    job = plan(a.measures, PART_SIZE if a.part_size is None else a.part_size, read, out / "_parts")
    first_year = a.first_year or job.first_claim_year
    version = _cet_version(a) if not a.tables else None
    tables = QuarterlyTables.from_folder(a.tables) if a.tables else QuarterlyTables.from_release(rel.name, version, a.export_dir, job.keys)
    priced = 0
    for i, (_rows, res) in enumerate(price(lambda: job.frames(read), programs, rel.name, tables, first_year, a.me_bens, a.me_cost)):
        priced += len(res.ce)
        for name, df in res.tables().items():       # part by part (a large job is never held whole); CET_ID order within a part
            df.to_csv(out / f"{name}.csv", index=False, mode="w" if i == 0 else "a", header=i == 0)
    shutil.rmtree(out / "_parts", ignore_errors=True)
    info = {"release": rel.name, "label": rel.label, "backup": rel.backup, "built": rel.built, "cet_version": tables.version,
            "acc": _acc_of(tables.version), "first_year": first_year, "me_bens": a.me_bens, "me_cost": a.me_cost,
            "measures": priced, "parts": job.parts, "inputs": {"measures": str(a.measures), "programcost": a.programcost}}
    (out / "run.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    acc = f"{info['acc']} ACC" if info["acc"] else f"Version {tables.version}"
    print(f"{rel.label}, {acc}: {priced} measures priced" + (f" in {job.parts} parts" if job.parts > 1 else "") + f"; outputs in {out}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("cet-version", help="price a job exactly as a prior CET release does (see `recet cet-version -h`)",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cet_cmd", required=True)
    s.add_parser("list", help="the releases ReCET reproduces and the ACCs each one has").set_defaults(func=cmd_list)
    r = s.add_parser("run", help="run a job on a release's own quarterly tables")
    r.add_argument("--release", required=True, help="the CET release, e.g. 25.1, 24.3, 21.0 (`recet cet-version list`)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--acc", help="the ACC year to price with, e.g. 2024")
    g.add_argument("--cet-version", help="CET's own avoided-cost Version number instead (e.g. 2026 for the 2024 ACC)")
    r.add_argument("--measures", required=True, help="CET Measure.csv in CET's CEDARS field names (pipe- or comma-delimited)")
    r.add_argument("--programcost", help="CET ProgramCost.csv")
    r.add_argument("--out", required=True, help="output folder")
    r.add_argument("--first-year", type=int, help="RunCET's FirstYear (default: the earliest claim year in the measures)")
    r.add_argument("--me-bens", type=float, default=0.0, help="market effects on benefits (CEDARS uses 0.05 for claims, 0 for filings)")
    r.add_argument("--me-cost", type=float, default=0.0, help="market effects on costs")
    r.add_argument("--export-dir", help="the release tables' parquet export (default: <reference data folder>/cet_versions)")
    r.add_argument("--tables", help="a folder of the release's tables as CSVs plus meta.json (the test fixtures' layout) instead of the export")
    r.add_argument("--part-size", type=int, help="measures priced at a time (default 10,000; 0 = the whole job at once). Memory grows with it, "
                                                 "not with the job: about 30 MB per 1,000 measures")
    r.set_defaults(func=cmd_run)


def main(a) -> int:
    if a.cet_cmd == "run" and not (a.acc or a.cet_version or a.tables):
        raise SystemExit("give --acc (or --cet-version, or --tables)")
    return a.func(a)
