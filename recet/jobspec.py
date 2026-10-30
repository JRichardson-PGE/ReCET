"""The job spec: the versioned JSON a caller (the CEDARS platform's Calculations module, a batch queue, a person) hands `recet worker run` to price one job.
The schema is `recet/data/job_spec/job_spec_v1.schema.json` (JSON Schema 2020-12, for callers); `validate` checks the same rules here without a schema
library, and tests/test_worker.py keeps the two in step.

    {
      "spec_version": 1,
      "job_id": "2028-claims-pge-0001",                       # letters, digits, . _ -; names the run folder and the receipt
      "engine": {"release": "0.1.78", "calculation": "recet"}, # release: the ReCET version that must run it (the worker refuses another);
                                                               # calculation: "recet" or a CET replica, "cet-25.1" (RunConfig.engine)
      "inputs": {                                              # each a URI (path, file://, s3://) or {"uri": ..., "sha256": ...}
        "measures": "s3://bucket/jobs/0001/Measure.csv",       # or "job_zip" (Measure.csv + ProgramCost.csv, as the web app takes them) or "workbook"
        "programcost": {"uri": "s3://bucket/jobs/0001/ProgramCost.csv", "sha256": "..."}
      },
      "settings": {"acc_version": "2024", "acc_purpose": "claims"},   # any RunConfig setting that is not a file, folder or name
      "packs": {"reference_dir": "/data",                      # default: the worker's reference data folder (RECET_DATA)
                "files": {"acc/2024/electric_hourly.parquet": {"sha256": "..."}}},   # pinned: refused when the folder's file differs
      "allocation_version": "statewide_factors_20260918",      # the statewide factor table the run must use (built-in name, or a supplied file's SHA-256)
      "outputs": {"uri": "s3://bucket/runs/0001/", "format": "csv.zip"},
      "context": {"platform_run_id": 123}                      # the caller's own references, echoed verbatim into receipt.json
    }
"""
from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

SPEC_VERSION = 1
SCHEMA_FILE = Path(__file__).parent / "data" / "job_spec" / "job_spec_v1.schema.json"
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# job spec input name -> RunConfig field (job_zip is unpacked into measures / programcost)
FILE_INPUTS = {"measures": "measures", "programcost": "programcost", "workbook": "workbook", "profiles_8760": "profiles_8760_file",
               "profiles_gas": "profiles_gas_file", "run_parameters": "run_parameters_file", "sw_factors": "sw_factors_file", "programs": "programs_file",
               "acc_electric": "acc_electric_file", "acc_gas": "acc_gas_file", "cet_outputs": "cet_outputs"}
INPUTS = ("job_zip", *FILE_INPUTS)
# RunConfig fields a spec does not set in `settings`: files (inputs), folders and names (the worker's), the engine and format (their own blocks), test hooks
NOT_SETTINGS = {*FILE_INPUTS.values(), "out_dir", "run_name", "reference_dir", "engine", "output_format", "subset_step"}
OUTPUT_FORMATS = ("csv", "csv.zip", "parquet", "xlsx")
TOP = {"spec_version", "job_id", "engine", "inputs", "settings", "packs", "allocation_version", "outputs", "context", "description"}


def settings_fields() -> dict[str, type]:
    """{RunConfig field: the Python type a spec gives it} for every field `settings` may set."""
    from .jobs import RunConfig
    out = {}
    for f in fields(RunConfig):
        if f.name in NOT_SETTINGS:
            continue
        base = str(f.type).split("|")[0].strip()              # "int | None" -> int (None is allowed for every setting)
        out[f.name] = list if base.startswith("list") else {"bool": bool, "int": int, "float": float}.get(base, str)
    return out


def _input_errors(name: str, v) -> list[str]:
    if isinstance(v, str):
        return [] if v.strip() else [f"inputs.{name}: empty URI"]
    if isinstance(v, dict):
        out = []
        if not isinstance(v.get("uri"), str) or not v["uri"].strip():
            out.append(f"inputs.{name}.uri: a URI is required")
        if "sha256" in v and not (isinstance(v["sha256"], str) and SHA256.match(v["sha256"])):
            out.append(f"inputs.{name}.sha256: 64 lower-case hex digits")
        extra = set(v) - {"uri", "sha256"}
        if extra:
            out.append(f"inputs.{name}: unknown key(s) {sorted(extra)}")
        return out
    return [f"inputs.{name}: a URI or {{\"uri\", \"sha256\"}}"]


def validate(spec) -> list[str]:
    """Everything wrong with `spec` (empty = valid)."""
    if not isinstance(spec, dict):
        return ["the job spec must be a JSON object"]
    errors = [f"unknown top-level key {k!r}" for k in sorted(set(spec) - TOP)]
    if spec.get("spec_version") != SPEC_VERSION:
        errors.append(f"spec_version must be {SPEC_VERSION}")
    if not (isinstance(spec.get("job_id"), str) and JOB_ID.match(spec["job_id"])):
        errors.append("job_id: 1-128 letters, digits, '.', '_' or '-', starting with a letter or digit")
    eng = spec.get("engine", {})
    if not isinstance(eng, dict) or set(eng) - {"release", "calculation"}:
        errors.append("engine: an object with release and/or calculation")
    else:
        if "release" in eng and not (isinstance(eng["release"], str) and eng["release"].strip()):
            errors.append("engine.release: a ReCET version, e.g. 0.1.78")
        if "calculation" in eng and not (isinstance(eng["calculation"], str) and (eng["calculation"] in ("", "recet") or eng["calculation"].startswith("cet-"))):
            errors.append("engine.calculation: 'recet' or a CET replica such as 'cet-25.1'")
    inp = spec.get("inputs")
    if not isinstance(inp, dict):
        errors.append("inputs: an object of URIs")
    else:
        errors += [f"inputs: unknown input {k!r} (known: {', '.join(INPUTS)})" for k in sorted(set(inp) - set(INPUTS))]
        for k, v in inp.items():
            if k in INPUTS:
                errors += _input_errors(k, v)
        given = [k for k in ("measures", "job_zip", "workbook") if k in inp]
        if len(given) != 1:
            errors.append("inputs: exactly one of measures, job_zip or workbook")
        if "job_zip" in inp and "programcost" in inp:
            errors.append("inputs: a job_zip carries its own ProgramCost.csv; leave programcost out")
    st = spec.get("settings", {})
    if not isinstance(st, dict):
        errors.append("settings: an object of RunConfig settings")
    else:
        allowed = settings_fields()
        for k, v in st.items():
            if k not in allowed:
                errors.append(f"settings.{k}: not a setting a job spec may set" + (" (it has its own block)" if k in NOT_SETTINGS else ""))
                continue
            t = allowed[k]
            ok = (v is None or (isinstance(v, bool) if t is bool else isinstance(v, list) if t is list else
                                (isinstance(v, int) and not isinstance(v, bool)) if t is int else
                                (isinstance(v, (int, float)) and not isinstance(v, bool)) if t is float else isinstance(v, (str, int))))
            if not ok:
                errors.append(f"settings.{k}: expected {t.__name__}")
    packs = spec.get("packs", {})
    if not isinstance(packs, dict) or set(packs) - {"reference_dir", "files"}:
        errors.append("packs: an object with reference_dir and/or files")
    else:
        if "reference_dir" in packs and not isinstance(packs["reference_dir"], str):
            errors.append("packs.reference_dir: a local folder")
        for rel, pin in (packs.get("files") or {}).items():
            if not isinstance(pin, dict) or not pin or set(pin) - {"sha256", "version"}:
                errors.append(f"packs.files.{rel}: an object with sha256 and/or version")
            elif "sha256" in pin and not (isinstance(pin["sha256"], str) and SHA256.match(pin["sha256"])):
                errors.append(f"packs.files.{rel}.sha256: 64 lower-case hex digits")
    if "allocation_version" in spec and not (isinstance(spec["allocation_version"], str) and spec["allocation_version"].strip()):
        errors.append("allocation_version: the statewide factor table's name or a supplied file's SHA-256")
    if "context" in spec and not isinstance(spec["context"], dict):
        errors.append("context: an object (echoed into receipt.json as given)")
    out = spec.get("outputs")
    if not isinstance(out, dict) or not isinstance(out.get("uri"), str) or not out["uri"].strip():
        errors.append("outputs.uri: the folder (path, file:// or s3://) the run's files go to")
    elif set(out) - {"uri", "format"} or out.get("format", "csv.zip") not in OUTPUT_FORMATS:
        errors.append(f"outputs: uri and format ({', '.join(OUTPUT_FORMATS)})")
    return errors


def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def input_uri(v) -> tuple[str, str | None]:
    """(uri, sha256 or None) of one input."""
    return (v, None) if isinstance(v, str) else (v["uri"], v.get("sha256"))
