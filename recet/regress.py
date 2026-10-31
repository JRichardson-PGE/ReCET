"""The regression gate (phase 0 of the CEDARS platform build plan): real jobs on real ACC and profile data, small enough for git, with their expected
outputs, checked on every pull request; and a comparison of any two runs, for the full jobs on the laptop.

`recet regress build`  (on a machine with the full reference data folder). Each job is run on the full folder while `record()` notes exactly what the
                       pricing reads: every (ACC file, utility, zone, cost test, column) series (all its years: the pricing reads every year of a series it
                       uses) and every (profile, zone) shape. `slice_reference` writes a reference folder holding only that: the ACC rows of the
                       (utility, zone) pairs read, with every value the run did not read set to NaN (so the file keeps the original's years, cost tests
                       and columns, and its size is that of the values read), the profile library's row groups of the profiles read with only the zones
                       read, and the whole catalog. Each job is then run again on that folder in a new process, and the build stops unless every
                       output table equals the full-folder run's (EXACT_TOLERANCE). The jobs' inputs, the folder and the outputs are written to the
                       fixtures folder with a manifest.json (hashes, versions, tolerances).
`recet regress check`  runs every job of a fixtures folder on its own reference folder and compares each output table with the expected one, within
                       TOLERANCE (tests/test_regression_gate.py runs it in CI). `--update` rewrites the expected outputs after a deliberate, documented
                       change of the results (no reference data needed).
`recet regress compare RUN_A RUN_B`  compares the tables of two run folders (e.g. one job run by two engine releases).

What a fixture cannot cover: a measure whose zone is System (its zone is chosen from every zone's prices over the whole ACC, so its slice would be most of
the file) or any route that blends zones; jobs are chosen without them (tests/test_system_zones.py covers the choice)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

TOLERANCE = {"rtol": 1e-9, "atol": 1e-6}          # check / compare: a number differs when |a - b| > atol + rtol x |b| (dollars, kWh, tonnes, ratios)
EXACT_TOLERANCE = {"rtol": 1e-12, "atol": 1e-9}   # build: the sliced folder must give the full folder's results (same machine, same code)
FIXTURE_VERSION = 1
SKIP_TABLES = ("field_definitions",)              # documentation of the columns, not results
SKIP_COLUMNS = {"run_settings", "RunName"}


# ---------------------------------------------------------------------------------------------------------------------------- recording what is read
class _Seen(dict):
    """A dict that notes the keys whose values are read: by key, by .get, or every key when it is iterated (a caller that iterates may read any value)."""

    def __init__(self, data, note):
        super().__init__(data)
        self._note = note

    def _all(self):
        for k in dict.keys(self):
            self._note(k)

    def __getitem__(self, k):
        self._note(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        if dict.__contains__(self, k):
            self._note(k)
        return dict.get(self, k, default)

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))


@dataclass
class Usage:
    """What a run read: acc[(file, iou, zone, test)] = the value columns read ("td" = transmission + distribution); profiles[(library, id)] = zones read."""
    acc: dict[tuple[str, str, str, str], set[str]] = field(default_factory=dict)
    profiles: dict[tuple[str, int], set[str]] = field(default_factory=dict)

    def merge(self, other: "Usage") -> None:
        for k, v in other.acc.items():
            self.acc.setdefault(k, set()).update(v)
        for k, v in other.profiles.items():
            self.profiles.setdefault(k, set()).update(v)


@contextlib.contextmanager
def record():
    """Within the block, note every ACC series (pipeline/acc_store.AccStore.series) and profile shape (real_impact_profiles._profile_shapes_by_zone) read.
    Every pipeline cache is cleared on the way in and out, so a series cached by an earlier run is read (and noted) again."""
    from . import runtime
    from .pipeline import acc_store, real_impact_profiles as rip
    usage = Usage()
    series0, shapes0 = acc_store.AccStore.series, rip._profile_shapes_by_zone

    def series(self, iou, zone, test, column, divisor=1000.0):
        out = series0(self, iou, zone, test, column, divisor)
        if out:
            cols = usage.acc.setdefault((str(Path(self.path).resolve()), iou, zone, test), set())
            return _Seen(out, lambda _y: cols.add(column))
        return out

    def shapes(profile_id):
        out = shapes0(profile_id)
        zones = usage.profiles.setdefault((str(rip._hourly_path().resolve()), int(profile_id)), set())
        return _Seen(out, zones.add)

    shapes.cache_clear = shapes0.cache_clear                 # runtime.clear_caches clears every lru_cache of the pipeline modules
    runtime.clear_caches()
    acc_store.AccStore.series, rip._profile_shapes_by_zone = series, shapes
    try:
        yield usage
    finally:
        acc_store.AccStore.series, rip._profile_shapes_by_zone = series0, shapes0
        runtime.clear_caches()


# ---------------------------------------------------------------------------------------------------------------------------- the sliced folder
def slice_reference(usage: Usage, src: Path, out: Path, log=print) -> dict[str, int]:
    """Write the reference folder `out` holding only what `usage` read from the folder `src`; returns {file: bytes}."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from .pipeline.acc_store import KEY_COLUMNS
    src, out = Path(src).resolve(), Path(out)
    sizes: dict[str, int] = {}
    by_file: dict[str, dict[tuple[str, str], dict[str, set[str]]]] = {}
    for (f, iou, zone, test), cols in usage.acc.items():
        by_file.setdefault(f, {}).setdefault((iou, zone), {})[test] = cols
    for f, pairs in sorted(by_file.items()):
        rel = Path(f).relative_to(src)
        pf = pq.ParquetFile(f)
        value_cols = [n for n in pf.schema_arrow.names if n not in KEY_COLUMNS and pa.types.is_floating(pf.schema_arrow.field(n).type)]
        pieces = []
        for g in range(pf.metadata.num_row_groups):
            t = pf.read_row_group(g)
            keep = None
            for iou, zone in pairs:
                m = pc.and_(pc.equal(t["iou"], iou), pc.equal(t["climate_zone"], zone))
                keep = m if keep is None else pc.or_(keep, m)
            t = t.filter(keep)
            if t.num_rows:
                pieces.append(t)
        t = pa.concat_tables(pieces).combine_chunks()
        df = t.to_pandas()
        for c in value_cols:                                          # a value the run did not read is NaN
            read = np.zeros(len(df), dtype=bool)
            for (iou, zone), tests in pairs.items():
                for test, cols in tests.items():
                    if c in cols or (c in ("transmission", "distribution") and "td" in cols):
                        read |= ((df["iou"] == iou) & (df["climate_zone"] == zone) & (df["cost_test"] == test)).to_numpy()
            df.loc[~read, c] = np.nan
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # floats split into byte streams compress about a quarter smaller under zstd (lossless; 28.8 MB for three pairs of the 2024 ACC without it)
        pq.write_table(pa.Table.from_pandas(df, schema=t.schema, preserve_index=False), dest, compression="zstd", compression_level=19,
                       use_byte_stream_split=value_cols, use_dictionary=[c for c in t.schema.names if c not in value_cols])
        sizes[str(rel).replace(os.sep, "/")] = dest.stat().st_size
        log(f"  {rel}: {len(df):,} rows of {len(pairs)} (utility, zone) pair(s), {dest.stat().st_size / 1e6:.1f} MB")
    libs = {f for f, _pid in usage.profiles}
    for lib in sorted(libs):
        rel = Path(lib).relative_to(src)
        cat_rel = rel.parent / "catalog.parquet"
        (out / cat_rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / cat_rel, out / cat_rel)
        sizes[str(cat_rel).replace(os.sep, "/")] = (out / cat_rel).stat().st_size
        pf = pq.ParquetFile(lib)
        index = {}
        for g in range(pf.metadata.num_row_groups):                   # one row group per profile (real_impact_profiles._row_group_index)
            index[int(pf.metadata.row_group(g).column(0).statistics.min)] = g
        dest = out / rel
        n = 0
        with pq.ParquetWriter(dest, pf.schema_arrow, compression="zstd", compression_level=19) as w:
            for (f, pid), zones in sorted(usage.profiles.items()):
                if f != lib or pid not in index or not zones:
                    continue
                t = pf.read_row_group(index[pid])
                t = t.filter(pc.is_in(t["climate_zone"], pa.array(sorted(zones))))
                if t.num_rows:
                    w.write_table(t, row_group_size=t.num_rows)
                    n += 1
        sizes[str(rel).replace(os.sep, "/")] = dest.stat().st_size
        log(f"  {rel}: {n} profile(s), {dest.stat().st_size / 1e6:.1f} MB")
    return sizes


# ---------------------------------------------------------------------------------------------------------------------------- jobs and their tables
@dataclass
class Job:
    name: str
    measures: Path
    programcost: Path | None
    settings: dict = field(default_factory=dict)       # RunConfig fields, e.g. {"acc_version": "2024"}


def run_config(job: Job, ref: Path, out_dir: Path):
    from .jobs import RunConfig
    settings = {"acc_version": "2024", **job.settings}
    return RunConfig(measures=str(job.measures), programcost=str(job.programcost) if job.programcost else None, out_dir=str(out_dir), run_name=job.name,
                     reference_dir=str(ref), output_format="parquet", **settings)


def tables(run_dir: Path) -> dict[str, Path]:
    """{table: file} of a run folder's result tables (top-level .parquet / .csv / .csv.zip files), named without the ACC tag."""
    out = {}
    for f in sorted(Path(run_dir).iterdir()):
        name = f.name
        for suffix in (".csv.zip", ".parquet", ".csv"):
            if name.endswith(suffix):
                stem = name[: -len(suffix)]
                key = stem.rsplit("_acc", 1)[0] if "_acc" in stem else stem
                if key not in SKIP_TABLES:
                    out[key] = f
                break
    return out


def read_table(f: Path) -> pd.DataFrame:
    from . import outputs
    return outputs.read_table(f) if not str(f).endswith(".parquet") else pd.read_parquet(f)


def differences(a: pd.DataFrame, b: pd.DataFrame, rtol: float, atol: float, limit: int = 20) -> list[str]:
    """What differs between table `a` (the run) and `b` (the expected one): columns, row count, then each column's worst value."""
    out = []
    ca, cb = [c for c in a.columns if c not in SKIP_COLUMNS], [c for c in b.columns if c not in SKIP_COLUMNS]
    if ca != cb:
        gone, new = [c for c in cb if c not in ca], [c for c in ca if c not in cb]
        out.append(f"columns differ: {len(gone)} missing {gone[:8]}, {len(new)} new {new[:8]}" if gone or new else "column order differs")
    if len(a) != len(b):
        out.append(f"{len(a):,} rows, expected {len(b):,}")
        return out
    a, b = a.reset_index(drop=True), b.reset_index(drop=True)
    for c in (c for c in cb if c in ca):
        x, y = a[c], b[c]
        nx, ny = pd.api.types.is_numeric_dtype(x) and not pd.api.types.is_bool_dtype(x), pd.api.types.is_numeric_dtype(y) and not pd.api.types.is_bool_dtype(y)
        if nx and ny:
            xv, yv = x.to_numpy(dtype=np.float64), y.to_numpy(dtype=np.float64)
            both_nan = np.isnan(xv) & np.isnan(yv)
            bad = ~both_nan & ~(np.abs(xv - yv) <= atol + rtol * np.abs(yv))
            if bad.any():
                i = int(np.nanargmax(np.where(bad, np.abs(xv - yv), -1.0)))
                out.append(f"{c}: {int(bad.sum()):,} value(s) differ, worst row {i}: {xv[i]!r} vs expected {yv[i]!r}")
        else:
            xs, ys = x.astype("string").fillna("<NA>"), y.astype("string").fillna("<NA>")
            bad = (xs != ys).to_numpy()
            if bad.any():
                i = int(np.argmax(bad))
                out.append(f"{c}: {int(bad.sum()):,} value(s) differ, first row {i}: {xs.iloc[i]!r} vs expected {ys.iloc[i]!r}")
        if len(out) >= limit:
            out.append("...")
            break
    return out


def compare_runs(run_a: Path, run_b: Path, rtol: float = TOLERANCE["rtol"], atol: float = TOLERANCE["atol"]) -> dict[str, list[str]]:
    """{table: differences} between two run folders (run_a measured against run_b); a table in only one of them is a difference too."""
    ta, tb = tables(run_a), tables(run_b)
    out = {}
    for k in sorted(set(ta) | set(tb)):
        if k not in ta or k not in tb:
            out[k] = [f"only in {'the first' if k in ta else 'the second'} run"]
            continue
        d = differences(read_table(ta[k]), read_table(tb[k]), rtol, atol)
        if d:
            out[k] = d
    return out


def _sha(f: Path) -> str:
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _run_in_new_process(cfg, log) -> Path:
    """Run `cfg` in a fresh interpreter (no cache of this process carries over); returns the run folder."""
    with tempfile.TemporaryDirectory(prefix="recet_regress_") as tmp:
        f = Path(tmp) / "config.json"
        f.write_text(cfg.to_json(), encoding="utf-8")
        env = {**os.environ, "RECET_HOME": str(Path(tmp) / "home")}       # its own cache folder, so nothing cached from the full folder is used
        p = subprocess.run([sys.executable, "-m", "recet", "run", "--config", str(f)], capture_output=True, text=True, env=env)
        if p.returncode:
            log(p.stdout[-4000:] + p.stderr[-4000:])
            raise RuntimeError(f"the run on the sliced folder failed (exit {p.returncode})")
    return Path(cfg.out_dir) / f"{cfg.run_name}_acc{cfg.acc_version}"


# ---------------------------------------------------------------------------------------------------------------------------- build / check
def build(jobs: list[Job], src_ref: Path, dest: Path, budget_mb: float = 30.0, log=print) -> dict:
    """Build the fixtures folder `dest` (see the module docstring). Refuses (writing nothing to `dest`) when a sliced run differs from its full run or the
    folder would be over `budget_mb`."""
    from . import __version__
    from .jobs import run_job
    from .pipeline import delimited
    src_ref = Path(src_ref).resolve()
    work = Path(tempfile.mkdtemp(prefix="recet_regress_build_"))
    try:
        usage, full_runs = Usage(), {}
        for job in jobs:
            log(f"== {job.name}: the full reference folder ({src_ref})")
            t0 = time.time()
            with record() as u:
                res = run_job(run_config(job, src_ref, work / "full"), log=lambda s: None)
            usage.merge(u)
            full_runs[job.name] = res.run_dir
            ml = read_table(tables(res.run_dir)["measure_level"])
            unpriced = {c: int((ml[c].astype("string") == "UNPRICED").sum()) for c in ("ElecPricingStatus", "GasPricingStatus") if c in ml}
            log(f"  {len(ml):,} measures priced in {time.time() - t0:.0f}s (unpriced: {unpriced}); read {len(u.acc)} ACC series and {len(u.profiles)} profiles")
        if not usage.acc:
            raise RuntimeError("the jobs read no electric ACC series (no measure was priced on the ACC?); nothing was written")
        stage = work / "fixtures"
        log("== slicing")
        sizes = slice_reference(usage, src_ref, stage / "ref", log)
        manifest = {"fixture_version": FIXTURE_VERSION, "built": time.strftime("%Y-%m-%d"), "built_with": f"recet {__version__}",
                    "source_reference": {rel: _sha(src_ref / rel) for rel in sizes}, "tolerance": TOLERANCE,
                    "ref": {rel: {"bytes": n, "sha256": _sha(stage / "ref" / rel)} for rel, n in sizes.items()}, "jobs": {}}
        for job in jobs:
            log(f"== {job.name}: the sliced folder, in a new process")
            jd = stage / "jobs" / job.name
            (jd / "inputs").mkdir(parents=True)
            shutil.copyfile(job.measures, jd / "inputs" / "Measure.csv")
            if job.programcost:
                shutil.copyfile(job.programcost, jd / "inputs" / "ProgramCost.csv")
            local = Job(job.name, jd / "inputs" / "Measure.csv", jd / "inputs" / "ProgramCost.csv" if job.programcost else None, job.settings)
            sliced = _run_in_new_process(run_config(local, stage / "ref", work / "sliced"), log)
            diff = compare_runs(sliced, full_runs[job.name], **EXACT_TOLERANCE)
            if diff:
                for k, d in diff.items():
                    log(f"  {k}: " + "; ".join(d))
                raise RuntimeError(f"{job.name}: the sliced reference folder does not reproduce the full folder's results; nothing was written")
            (jd / "expected").mkdir()
            for k, f in tables(sliced).items():
                shutil.copyfile(f, jd / "expected" / f"{k}.parquet")
            manifest["jobs"][job.name] = {"settings": {"acc_version": "2024", **job.settings},
                                          "measures": delimited.count_rows(local.measures),
                                          "tables": sorted(tables(sliced)), "cost_tests": cost_tests_used(sliced)}
            log(f"  equal to the full folder's run in all {len(tables(sliced))} tables")
        total = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
        manifest["bytes"] = total
        log(f"== fixtures: {total / 1e6:.1f} MB")
        if total > budget_mb * 1e6:
            raise RuntimeError(f"the fixtures are {total / 1e6:.1f} MB, over the {budget_mb:g} MB budget; nothing was written (choose fewer zones or profiles)")
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if (dest / "README.md").exists():
            shutil.copyfile(dest / "README.md", stage / "README.md")       # the folder's own notes are kept
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(stage, dest)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cost_tests_used(run_dir: Path) -> dict | None:
    """{id, sha256} of the cost-test set a run priced on (run_settings.json), which the fixtures record: a change of the set fails `check`."""
    f = Path(run_dir) / "run_settings.json"
    if not f.exists():
        return None
    ct = json.loads(f.read_text(encoding="utf-8")).get("cost_tests") or {}
    return {"id": ct.get("id"), "sha256": ct.get("sha256")} if ct else None


def fixture_jobs(fixtures: Path) -> list[Job]:
    manifest = json.loads((Path(fixtures) / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for name, spec in manifest["jobs"].items():
        d = Path(fixtures) / "jobs" / name / "inputs"
        pc = d / "ProgramCost.csv"
        out.append(Job(name, d / "Measure.csv", pc if pc.exists() else None, spec.get("settings", {})))
    return out


def check(fixtures: Path, out_dir: Path, update: bool = False, log=print) -> dict[str, dict[str, list[str]]]:
    """Run every job of `fixtures` on its sliced folder and compare with the expected tables; {job: {table: differences}} (empty = all equal).
    `update` rewrites the expected tables instead (after a deliberate change of the results)."""
    from .jobs import run_job
    fixtures = Path(fixtures)
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    tol = manifest.get("tolerance", TOLERANCE)
    for rel, meta in manifest["ref"].items():
        f = fixtures / "ref" / rel
        if not f.exists() or _sha(f) != meta["sha256"]:
            raise RuntimeError(f"fixture file {rel} is missing or changed (its SHA-256 is in manifest.json)")
    result = {}
    for job in fixture_jobs(fixtures):
        res = run_job(run_config(job, fixtures / "ref", Path(out_dir)), log=lambda s: None)
        expected = fixtures / "jobs" / job.name / "expected"
        if update:
            shutil.rmtree(expected, ignore_errors=True)
            expected.mkdir()
            for k, f in tables(res.run_dir).items():
                pd.read_parquet(f).to_parquet(expected / f"{k}.parquet", index=False) if f.suffix == ".parquet" else shutil.copyfile(f, expected / f"{k}.parquet")
            manifest["jobs"][job.name]["cost_tests"] = cost_tests_used(res.run_dir)
            log(f"{job.name}: expected tables rewritten")
            result[job.name] = {}
            continue
        diff = compare_runs(res.run_dir, expected, **tol)
        want, got = manifest["jobs"][job.name].get("cost_tests"), cost_tests_used(res.run_dir)
        if want != got:
            diff["cost_tests"] = [f"the run priced on cost-test set {got}, the fixtures were made on {want}: a changed set needs a CHANGES.md entry "
                                  "and `recet regress check --update`"]
        result[job.name] = diff
        log(f"{job.name}: " + ("equal to the expected outputs in every table" if not diff else f"{len(diff)} table(s) differ"))
        for k, d in diff.items():
            log(f"  {k}: " + "; ".join(d))
    if update:
        (fixtures / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    """`recet regress build|check|compare` (see the module docstring)."""
    import argparse
    from . import paths
    ap = argparse.ArgumentParser(prog="recet regress", description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("build", help="build regression fixtures from jobs and the full reference data folder")
    p.add_argument("--job", action="append", required=True, metavar="NAME=FOLDER",
                   help="a job: a folder holding Measure.csv (and ProgramCost.csv); repeat for several")
    p.add_argument("--acc", default="2024", help="the ACC version the jobs are priced on (default 2024)")
    p.add_argument("--data-dir", help="the full reference data folder (default: the configured one)")
    p.add_argument("--out", required=True, help="the fixtures folder to write (replaced)")
    p.add_argument("--budget-mb", type=float, default=30.0, help="refuse fixtures larger than this (default 30)")
    p = sub.add_parser("check", help="run the fixtures' jobs and compare with their expected outputs")
    p.add_argument("fixtures", nargs="?", default=str(Path(__file__).resolve().parents[1] / "tests" / "data" / "regression"))
    p.add_argument("--out", help="where to keep the runs (default: a temporary folder)")
    p.add_argument("--update", action="store_true", help="rewrite the expected outputs (after a deliberate change of the results)")
    p = sub.add_parser("compare", help="compare the tables of two run folders")
    p.add_argument("run_a"), p.add_argument("run_b")
    p.add_argument("--rtol", type=float, default=TOLERANCE["rtol"]), p.add_argument("--atol", type=float, default=TOLERANCE["atol"])
    a = ap.parse_args(argv)
    if a.action == "build":
        src = Path(a.data_dir) if a.data_dir else paths.reference_dir()
        if src is None:
            ap.error("no reference data folder: pass --data-dir or set one (`recet data set-dir`)")
        jobs = []
        for spec in a.job:
            name, _, folder = spec.partition("=")
            d = Path(folder)
            pc = next((d / n for n in ("ProgramCost.csv", "programcost.csv") if (d / n).exists()), None)
            jobs.append(Job(name, d / "Measure.csv", pc, {"acc_version": a.acc}))
        try:
            build(jobs, src, Path(a.out), a.budget_mb)
        except RuntimeError as exc:
            print(f"regress build: {exc}", file=sys.stderr)
            return 1
        return 0
    if a.action == "check":
        with tempfile.TemporaryDirectory(prefix="recet_regress_check_") as tmp:
            res = check(Path(a.fixtures), Path(a.out or tmp), a.update)
        return 1 if any(res.values()) else 0
    diff = compare_runs(Path(a.run_a), Path(a.run_b), a.rtol, a.atol)
    for k, d in diff.items():
        print(f"{k}: " + "; ".join(d))
    print("the two runs agree in every table" if not diff else f"{len(diff)} table(s) differ")
    return 1 if diff else 0
