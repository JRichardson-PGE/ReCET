"""`recet worker run job.json`: price one job from a job spec (jobspec.py) and write its outputs and a receipt. This is the contract the CEDARS platform's
Calculations module calls (phase 0 of the platform build plan): the platform keeps the job queue, run records and storage layout; the worker only reads a
spec, fetches its inputs (a local path, file:// or s3://; storage.py), checks the release and the pinned reference data, runs `jobs.run_job` in a work folder
and sends the run folder, with `receipt.json`, to the spec's outputs URI.

receipt.json (RECEIPT_VERSION) says exactly what produced the outputs: the engine (release, calculation, image and commit when the image sets RECET_IMAGE /
RECET_COMMIT, Python and library versions), each input's URI, SHA-256, size (and Measure rows), the run's full settings, the ACC (version, files) and the
ACC each record was priced on (by ACC version and program year), every reference file used with its size and SHA-256 (from the reference folder's
manifest.json, else computed) and the hash of the packaged data, the run parameters and statewide allocation table used, the filing rule set, every output
file's SHA-256, the totals, notes and timings. A failed run still gets a receipt (status "failed", the error), sent with whatever the run wrote.

Exit codes: 0 succeeded; 1 the run failed (receipt written); 2 the spec was refused (nothing run; the reasons on stderr)."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import jobspec, storage

RECEIPT_VERSION = 1
PROGRESS_SECONDS = 10                               # progress.json is sent to the outputs URI at each stage change and at most this often between
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024            # a job zip's Measure.csv + ProgramCost.csv, as the web app's limit (RECET_WEB_MAX_UNZIPPED_MB)


class SpecRefused(ValueError):
    """The spec cannot be run as given; nothing was priced."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_spec(uri: str, work: Path) -> tuple[dict, str]:
    """(spec, its SHA-256) from a path or URI."""
    f = storage.for_uri(uri).fetch(uri, work / "job.json")
    try:
        return json.loads(f.read_text(encoding="utf-8")), sha256_file(f)
    except json.JSONDecodeError as exc:
        raise SpecRefused(f"the job spec is not valid JSON: {exc}") from exc


def fetch_inputs(spec: dict, work: Path) -> tuple[dict[str, Path], dict[str, dict]]:
    """Fetch every input into `work/inputs`, checking any pinned SHA-256. Returns ({RunConfig field: local file}, {input: receipt record})."""
    from .pipeline import delimited
    files, record = {}, {}
    d = work / "inputs"
    for name, v in spec["inputs"].items():
        uri, pinned = jobspec.input_uri(v)
        suffix = Path(uri.replace("\\", "/").rstrip("/").split("/")[-1]).suffix or (".zip" if name == "job_zip" else ".csv")
        local = storage.for_uri(uri).fetch(uri, d / f"{name}{suffix}")       # a fixed name: the URI's own path is never used locally
        digest = sha256_file(local)
        if pinned and pinned != digest:
            raise SpecRefused(f"inputs.{name}: SHA-256 {digest} is not the pinned {pinned} ({uri})")
        record[name] = {"uri": uri, "sha256": digest, "bytes": local.stat().st_size}
        if name == "job_zip":
            from .web import jobzip
            try:
                unpacked = jobzip.unpack(local, d / "job_zip", MAX_UNZIPPED_BYTES)
            except jobzip.JobZipError as exc:
                raise SpecRefused(f"inputs.job_zip: {exc}") from exc
            for k, p in unpacked.items():
                files[k] = p
                record[f"job_zip/{k}"] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
        else:
            files[jobspec.FILE_INPUTS[name]] = local
    if "measures" in files and delimited.detect_sep(files["measures"]):
        key = "measures" if "measures" in record else "job_zip/measures"
        record[key]["rows"] = delimited.count_rows(files["measures"])
    return files, record


def reference_files(ref: Path | None) -> dict[str, dict]:
    """{relative path: {bytes, sha256, sha256_from}} of the reference folder's files (paths.REF_FILES), the SHA-256 from its manifest.json when the manifest
    has the file at its present size, else left to `_hash_if_needed`."""
    from . import datapack, paths
    out = {}
    if ref is None:
        return out
    manifest = {}
    m = Path(ref) / datapack.MANIFEST
    if m.exists():
        try:
            manifest = json.loads(m.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError):
            manifest = {}
    for rf in paths.REF_FILES.values():
        f = Path(ref) / rf.rel
        if not f.exists():
            continue
        info = {"bytes": f.stat().st_size}
        mf = manifest.get(rf.rel, {})
        if mf.get("sha256") and mf.get("size") in (None, info["bytes"]):
            info.update(sha256=mf["sha256"], sha256_from="manifest.json")
        out[rf.rel] = info
    return out


def _hash_if_needed(ref: Path, rel: str, info: dict) -> str:
    if "sha256" not in info:
        info.update(sha256=sha256_file(Path(ref) / rel), sha256_from="computed")
    return info["sha256"]


def check_packs(spec: dict, ref: Path | None, files: dict[str, dict]) -> None:
    """Refuse the job when a pinned reference file is missing or differs."""
    pins = (spec.get("packs") or {}).get("files") or {}
    problems = []
    for rel, pin in pins.items():
        if ref is None or rel not in files:
            problems.append(f"packs.files: {rel} is not in the reference folder ({ref})")
            continue
        if "sha256" in pin and _hash_if_needed(ref, rel, files[rel]) != pin["sha256"]:
            problems.append(f"packs.files: {rel} has SHA-256 {files[rel]['sha256']}, not the pinned {pin['sha256']}")
        if "version" in pin:
            files[rel]["version"] = pin["version"]
    if problems:
        raise SpecRefused("; ".join(problems))


_PACKAGE_DATA: str | None = None


def package_data_sha256() -> str:
    """One SHA-256 over every file shipped in recet/data (relative path and content), so a receipt names the small tables too."""
    global _PACKAGE_DATA
    if _PACKAGE_DATA is None:
        from . import paths
        root = paths.package_data()
        h = hashlib.sha256()
        for f in sorted(p for p in Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            h.update(f.relative_to(root).as_posix().encode() + b"\0" + sha256_file(f).encode() + b"\n")
        _PACKAGE_DATA = h.hexdigest()
    return _PACKAGE_DATA


def allocation_record(cfg, spec: dict | None = None) -> dict:
    """The statewide factor table the run used: the built-in one by name, or a supplied file (its URI and mode: "replace" = the whole table, "merge" = its
    rows over the built-in ones); its SHA-256; the allocation_version the spec named."""
    from .pipeline import statewide_allocation
    requested = (spec or {}).get("allocation_version")
    if cfg.sw_factors_file:
        uri = jobspec.input_uri(spec["inputs"]["sw_factors"])[0] if spec and "sw_factors" in spec.get("inputs", {}) else None
        return {"table": "supplied", "file": uri, "sha256": sha256_file(Path(cfg.sw_factors_file)), "requested": requested, "applied": cfg.allocate_statewide,
                "mode": "replace" if cfg.sw_factors_replace else "merge"}
    f = statewide_allocation.FACTORS_CSV
    return {"table": f.stem, "sha256": sha256_file(f), "requested": requested, "applied": cfg.allocate_statewide}


def programs_record(cfg, spec: dict | None = None) -> dict:
    """The program table the run used: the built-in CEDARS Programs list by name, or a supplied file (its URI, SHA-256 and mode: "replace" = the whole
    table, "merge" = its rows over the built-in ones)."""
    from . import programs
    if cfg.programs_file:
        uri = jobspec.input_uri(spec["inputs"]["programs"])[0] if spec and "programs" in spec.get("inputs", {}) else None
        return {"table": "supplied", "file": uri, "sha256": sha256_file(Path(cfg.programs_file)), "mode": "replace" if cfg.programs_replace else "merge"}
    return {"table": programs.BUILTIN_CSV.stem, "sha256": sha256_file(programs.BUILTIN_CSV)}


def check_allocation(spec: dict, cfg) -> None:
    want = spec.get("allocation_version")
    if want:
        rec = allocation_record(cfg, spec)
        if want not in (rec["table"], rec["sha256"]):
            raise SpecRefused(f"allocation_version {want!r}: this run would use {rec['table']} ({rec['sha256'][:12]}...)")


def run_parameters_record(cfg, settings_acc) -> dict:
    """The run parameters (discount rates, base year, gas CO2 per PA) the run used, and the ACC version whose rows were taken."""
    from .pipeline import discount_settings
    if cfg.run_parameters_file:
        return {"source": "supplied", "sha256": sha256_file(Path(cfg.run_parameters_file)), "settings_acc_version": settings_acc}
    return {"source": "built-in", "table": discount_settings.SETTINGS_CSV.name, "sha256": sha256_file(discount_settings.SETTINGS_CSV),
            "settings_acc_version": settings_acc}


def engine_record(cfg) -> dict:
    import numpy
    import pandas
    import pyarrow
    from . import __version__
    from .cet_versions.job import engine_record as calc
    return {"release": __version__, "calculation": calc(cfg.engine), "image": os.environ.get("RECET_IMAGE", ""), "commit": os.environ.get("RECET_COMMIT", ""),
            "python": platform.python_version(), "platform": platform.platform(),
            "libraries": {"numpy": numpy.__version__, "pandas": pandas.__version__, "pyarrow": pyarrow.__version__}}


def build_config(spec: dict, files: dict[str, Path], ref: Path | None, out_dir: Path):
    from .jobs import RunConfig
    eng = spec.get("engine") or {}
    calc = eng.get("calculation", "")
    cfg = RunConfig(**{k: v for k, v in (spec.get("settings") or {}).items() if v is not None}, **{k: str(p) for k, p in files.items()},
                    out_dir=str(out_dir), run_name=spec["job_id"], reference_dir=str(ref) if ref else None,
                    engine="" if calc in ("", "recet") else calc, output_format=(spec["outputs"].get("format") or "csv.zip"))
    if not isinstance(cfg.acc_version, str):
        cfg.acc_version = str(cfg.acc_version)
    return cfg


class ProgressSender:
    """While a run runs: point progress.py at `local` (RECET_PROGRESS_FILE) and send it to `uri` whenever its stage changes, and at most every `every`
    seconds while its parts move (BR-06.2: the caller's run page shows the stage and time left of a batch run). A failed send is skipped, never fatal."""

    def __init__(self, local: Path, uri: str, every: float = PROGRESS_SECONDS, poll: float = 1.0):
        self.local, self.uri, self.every, self.poll = Path(local), uri, every, poll
        self._stop = threading.Event()
        self._sent: tuple[str | None, str | None] = (None, None)          # (stage, text) last sent
        self._last = 0.0
        self.sends = 0

    def __enter__(self):
        from . import progress
        self._env = os.environ.get(progress.ENV)
        os.environ[progress.ENV] = str(self.local)
        progress._T0 = time.time()                                      # the time left counts from this run's start
        self._thread = threading.Thread(target=self._loop, name="recet-progress", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        from . import progress
        self._stop.set()
        self._thread.join(timeout=30)
        self.send(force=True)
        if self._env is None:
            os.environ.pop(progress.ENV, None)
        else:
            os.environ[progress.ENV] = self._env
        return False

    def _loop(self):
        while not self._stop.wait(self.poll):
            self.send()

    def send(self, force: bool = False) -> None:
        try:
            text = self.local.read_text(encoding="utf-8")
            stage = json.loads(text).get("stage")
        except (OSError, ValueError):
            return
        if text == self._sent[1]:
            return
        if not (force or stage != self._sent[0] or time.time() - self._last >= self.every):
            return
        try:
            storage.for_uri(self.uri).put(self.local, self.uri)
        except Exception:                                               # the run goes on; the next change is sent again
            return
        self._sent, self._last = (stage, text), time.time()
        self.sends += 1


def run(spec_uri: str, work: Path | None = None, log=print) -> tuple[int, dict | None]:
    """Run one job spec; returns (exit code, receipt or None when the spec was refused)."""
    from . import __version__, paths
    from .jobs import run_job
    t0 = time.time()
    started = _now()
    own_work = work is None
    work = Path(tempfile.mkdtemp(prefix="recet_worker_")) if own_work else Path(work)
    work.mkdir(parents=True, exist_ok=True)
    try:
        try:
            spec, spec_sha = load_spec(spec_uri, work)
            errors = jobspec.validate(spec)
            if errors:
                raise SpecRefused("; ".join(errors))
            release = (spec.get("engine") or {}).get("release")
            if release and release != __version__:
                raise SpecRefused(f"engine.release {release} was asked for; this worker is recet {__version__}")
            files, inputs = fetch_inputs(spec, work)
            packs = spec.get("packs") or {}
            ref = Path(packs["reference_dir"]) if packs.get("reference_dir") else paths.reference_dir()
            ref_files = reference_files(ref)
            check_packs(spec, ref, ref_files)
            cfg = build_config(spec, files, ref, work / "out")
            check_allocation(spec, cfg)
        except (SpecRefused, storage.StorageError, TypeError) as exc:
            print(f"recet worker: job refused: {exc}", file=sys.stderr)
            return 2, None

        log(f"recet worker {__version__}: job {spec['job_id']}")
        status, error, result = "succeeded", None, None
        sender = ProgressSender(work / "progress.json", storage.join(spec["outputs"]["uri"], "progress.json"))
        try:
            with sender:
                result = run_job(cfg, log=log)
        except Exception as exc:                                       # a failed run still gets a receipt
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log(traceback.format_exc())
        run_dir = result.run_dir if result is not None else next((p for p in (work / "out").glob(f"{spec['job_id']}_acc*") if p.is_dir()), None)
        if run_dir is None:
            run_dir = work / "out" / f"{spec['job_id']}_failed"
            run_dir.mkdir(parents=True, exist_ok=True)
        receipt = make_receipt(spec, spec_sha, cfg, result, run_dir, inputs, ref, ref_files, status, error, started, t0)
        (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str) + "\n", encoding="utf-8")
        written = storage.put_dir(run_dir, spec["outputs"]["uri"])
        log(f"{status}: {len(written)} file(s) sent to {spec['outputs']['uri']}")
        return (0 if status == "succeeded" else 1), receipt
    finally:
        if own_work:
            shutil.rmtree(work, ignore_errors=True)


def make_receipt(spec, spec_sha, cfg, result, run_dir: Path, inputs, ref, ref_files, status, error, started, t0) -> dict:
    settings = {}
    f = run_dir / "run_settings.json"
    if f.exists():
        try:
            settings = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            settings = {}
    acc = settings.get("acc", {})
    used = {}
    for k in ("electric", "gas"):                                     # the ACC files actually read, by their place in the reference folder
        p = acc.get(k)
        if p and ref is not None:
            try:
                rel = Path(p).resolve().relative_to(Path(ref).resolve()).as_posix()
            except ValueError:
                rel = None
            if rel in ref_files:
                _hash_if_needed(ref, rel, ref_files[rel])
                used[rel] = ref_files[rel]
    for rel in ("impact_profiles/catalog.parquet", "impact_profiles/hourly.parquet"):
        if rel in ref_files:
            used[rel] = ref_files[rel]
    outputs = {}
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name != "receipt.json":
            rel = p.relative_to(run_dir).as_posix()
            outputs[rel] = {"uri": storage.join(spec["outputs"]["uri"], rel), "sha256": sha256_file(p), "bytes": p.stat().st_size}
    from .jobs import RunConfig
    config = asdict(cfg) if isinstance(cfg, RunConfig) else {}
    for k in jobspec.FILE_INPUTS.values():                              # the work folder's paths say nothing; the inputs block has the URIs
        if config.get(k):
            config[k] = f"<input {Path(config[k]).stem}>"
    config.pop("out_dir", None)
    return {
        "receipt_version": RECEIPT_VERSION, "job_id": spec["job_id"], "status": status, "error": error, "context": spec.get("context"),
        "started": started, "finished": _now(), "seconds": round(time.time() - t0, 1),
        "engine": engine_record(cfg),
        "spec": {"sha256": spec_sha, "spec_version": spec["spec_version"], "allocation_version": spec.get("allocation_version")},
        "inputs": inputs,
        "settings": config,
        "acc": {"version": cfg.acc_version, "label": acc.get("label"), "avoided_cost_version": acc.get("avoided_cost_version"),
                "settings_acc_version": acc.get("settings_acc_version"), "electric_source": acc.get("electric_source"),
                "by_record": (result.acc_check or {}).get("by_acc_and_year") if result is not None else None},
        "packs": {"reference_dir": str(ref or ""), "files": used, "package_data_sha256": package_data_sha256()},
        "run_parameters": run_parameters_record(cfg, acc.get("settings_acc_version")),
        "allocation": allocation_record(cfg, spec),
        "programs": programs_record(cfg, spec),
        "rule_set": settings.get("filing_validation"),
        "outputs": outputs,
        "totals": result.totals if result is not None else {},
        "notes": result.notes if result is not None else [],
    }


def main(argv: list[str] | None = None) -> int:
    """`recet worker run|validate|schema`."""
    import argparse
    ap = argparse.ArgumentParser(prog="recet worker", description="run one job from a job spec (recet/jobspec.py) and write its receipt")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="run a job spec: fetch its inputs, run it, send its outputs and receipt.json to its outputs URI")
    p.add_argument("spec", help="the job spec: a path, file:// or s3:// URI")
    p.add_argument("--work", help="the work folder (default: a temporary folder, removed afterwards)")
    p = sub.add_parser("validate", help="check a job spec without running it")
    p.add_argument("spec")
    sub.add_parser("schema", help="print the job spec's JSON schema")
    a = ap.parse_args(argv)
    if a.action == "schema":
        print(jobspec.SCHEMA_FILE.read_text(encoding="utf-8"))
        return 0
    if a.action == "validate":
        with tempfile.TemporaryDirectory() as tmp:
            try:
                spec, _ = load_spec(a.spec, Path(tmp))
            except (SpecRefused, storage.StorageError) as exc:
                print(exc, file=sys.stderr)
                return 2
        errors = jobspec.validate(spec)
        for e in errors:
            print(e, file=sys.stderr)
        print("valid" if not errors else f"{len(errors)} problem(s)")
        return 2 if errors else 0
    code, _receipt = run(a.spec, Path(a.work) if a.work else None)
    return code
