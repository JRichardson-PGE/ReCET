"""`recet worker check check.json`: run a filing's rules in this release, as `recet worker run` prices a job. The CEDARS platform keeps each filing's
rules as data and names the engine release that checks and prices the filing; it hands that release's worker a check spec, and the findings come back
from the same release for the filing's whole life, whatever release the website itself has installed.

    {
      "check_spec_version": 1,
      "job_id": "check-2028-claims-pge-0001",                  # letters, digits, . _ -
      "engine": {"release": "0.1.95"},                         # the worker refuses another release
      "rule_set": {"code": "claims", "version": "2026-03-12",  # what the rules are (echoed in the receipt)
                   "rules": [{"id": "V1a2b3c", "severity": "reject", "table": "Measure", "field": "NTGRkWh", "text": "...",
                              "kind": "rule", "source": "", "definition": {...}}]},   # definition: filing_rules.Rule.spec
      "tables": {"Measure": "s3://.../Measure.csv",            # each a URI or {"uri", "sha256"}: UTF-8 CSV with a header, read as text
                 "Claim": {"uri": "...", "sha256": "..."}},
      "context": {"filing_year": 2028, "upload_date": "2028-04-30", "filer": {"PA": "PGE", "PAType": "IOU"},
                  "keys": {"Claim": ["ClaimID"], "Measure": ["ClaimID"]},             # each table's key columns (links between tables)
                  "programs": [{"PrgID": "PGE_Ag_001", "Category": "..."}],          # the program records conditions on Program read
                  "lists": {"BldgType": [{"code": "Com", "start_year": 2015, "end_year": null}]},   # lists the engine does not ship
                  "value_list_changes": {"ac_climate_zone": {...}}},                 # changes over the shipped lists (editor.set_value_list_changes)
      "skip": {"Measure": ["V1a2b3c"]},                        # rules not to run on a table (they read a column the file lacks)
      "outputs": {"uri": "s3://.../check/"},                   # findings.json and receipt.json are written here
      "caller": {"platform_check_id": "..."}                   # echoed verbatim into the receipt
    }

findings.json: {"tables": {name: {"rows": n, "results": [{"rule_id", "severity", "rows": [0-based row numbers], "actual": [the field's value in each]}],
"notes": [...]}}, "library": whether the profile library was there}. `run_check` does the same in the caller's own process (a website running this
release), so both give the same findings.

Exit codes: 0 checked; 1 the check failed (receipt written, with the error); 2 the spec was refused (nothing run; the reasons on stderr)."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

CHECK_SPEC_VERSION = 1
RECEIPT_VERSION = 1
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOP = {"check_spec_version", "job_id", "engine", "rule_set", "tables", "context", "skip", "outputs", "caller", "description"}
CONTEXT = {"filing_year", "upload_date", "filer", "keys", "programs", "lists", "value_list_changes"}
RULE_KEYS = {"id", "severity", "table", "field", "text", "kind", "source", "definition"}
MAX_ACTUAL = 200                                     # characters of a failing value kept in findings.json


class CheckRefused(ValueError):
    """The check spec cannot be run as given; nothing was checked."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uri(v) -> tuple[str, str | None]:
    return (v, None) if isinstance(v, str) else (v["uri"], v.get("sha256"))


def validate(spec) -> list[str]:
    """Everything wrong with a check spec (empty = valid)."""
    if not isinstance(spec, dict):
        return ["the check spec must be a JSON object"]
    errors = [f"unknown top-level key {k!r}" for k in sorted(set(spec) - TOP)]
    if spec.get("check_spec_version") != CHECK_SPEC_VERSION:
        errors.append(f"check_spec_version must be {CHECK_SPEC_VERSION}")
    if not (isinstance(spec.get("job_id"), str) and JOB_ID.match(spec["job_id"])):
        errors.append("job_id: 1-128 letters, digits, '.', '_' or '-', starting with a letter or digit")
    eng = spec.get("engine", {})
    if not isinstance(eng, dict) or set(eng) - {"release"}:
        errors.append("engine: an object with release")
    rs = spec.get("rule_set")
    if not isinstance(rs, dict) or not isinstance(rs.get("rules"), list):
        errors.append("rule_set: an object with a list of rules")
    else:
        for i, r in enumerate(rs["rules"]):
            if not isinstance(r, dict) or not {"id", "severity", "table", "kind", "definition"} <= set(r) or set(r) - RULE_KEYS:
                errors.append(f"rule_set.rules[{i}]: id, severity, table, kind and definition (and field, text, source)")
            elif r["severity"] not in ("reject", "warning") or r["kind"] not in ("rule", "spec") or not isinstance(r["definition"], dict):
                errors.append(f"rule_set.rules[{i}] ({r.get('id')}): severity reject or warning, kind rule or spec, definition an object")
    tables = spec.get("tables")
    if not isinstance(tables, dict) or not tables:
        errors.append("tables: an object of {table: URI}")
    else:
        for name, v in tables.items():
            if isinstance(v, str) and v.strip():
                continue
            if isinstance(v, dict) and isinstance(v.get("uri"), str) and v["uri"].strip() and not set(v) - {"uri", "sha256"} \
                    and ("sha256" not in v or (isinstance(v["sha256"], str) and SHA256.match(v["sha256"]))):
                continue
            errors.append(f"tables.{name}: a URI or {{\"uri\", \"sha256\"}}")
    ctx = spec.get("context", {})
    if not isinstance(ctx, dict):
        errors.append("context: an object")
    else:
        errors += [f"context: unknown key {k!r} (known: {', '.join(sorted(CONTEXT))})" for k in sorted(set(ctx) - CONTEXT)]
        if ctx.get("upload_date"):
            try:
                date.fromisoformat(str(ctx["upload_date"]))
            except ValueError:
                errors.append("context.upload_date: YYYY-MM-DD")
        if ctx.get("filing_year") is not None and not (isinstance(ctx["filing_year"], int) and not isinstance(ctx["filing_year"], bool)):
            errors.append("context.filing_year: a year")
        for k, t in (("filer", dict), ("keys", dict), ("programs", list), ("lists", dict), ("value_list_changes", dict)):
            if ctx.get(k) is not None and not isinstance(ctx[k], t):
                errors.append(f"context.{k}: {'an object' if t is dict else 'a list'}")
    skip = spec.get("skip", {})
    if not isinstance(skip, dict) or any(not isinstance(v, list) for v in skip.values()):
        errors.append("skip: an object of {table: [rule IDs]}")
    out = spec.get("outputs")
    if not isinstance(out, dict) or not isinstance(out.get("uri"), str) or not out["uri"].strip() or set(out) - {"uri"}:
        errors.append("outputs.uri: the folder (path, file:// or s3://) findings.json and receipt.json go to")
    if "caller" in spec and not isinstance(spec["caller"], dict):
        errors.append("caller: an object (echoed into receipt.json as given)")
    return errors


def rule_set_of(spec: dict):
    """The spec's rules as the engine's RuleSet."""
    from . import filing_rules as fr
    rs = spec["rule_set"]
    rules = [fr.Rule(r["id"], r["severity"], r["table"], r.get("field", ""), r.get("text", ""), r["kind"], r.get("source", ""), r["definition"])
             for r in rs["rules"]]
    return fr.RuleSet(str(rs.get("code") or "rules"), str(rs.get("version") or ""), None, rules)


def context_of(spec: dict, library: bool):
    """The spec's context as the engine's filing_rules.Context."""
    import pandas as pd

    from . import filing_rules as fr
    c = spec.get("context") or {}
    programs = None
    if c.get("programs") is not None:
        programs = pd.DataFrame([{k: "" if v is None else str(v) for k, v in p.items()} for p in c["programs"]])
        if programs.empty:
            programs = pd.DataFrame(columns=["PrgID"])
    return fr.Context(filing_year=c.get("filing_year"), library=library,
                      upload_date=date.fromisoformat(str(c["upload_date"])) if c.get("upload_date") else None,
                      filer=c.get("filer") or None, programs=programs, keys=c.get("keys") or None, lists=c.get("lists") or None)


def library_available() -> bool:
    try:
        from .pipeline import real_impact_profiles
        return bool(real_impact_profiles.is_available())
    except Exception:                                    # noqa: BLE001 - no library is the answer, whatever the reason
        return False


def run_check(spec: dict, tables: dict, library: bool | None = None) -> dict:
    """Run a check spec's rules on its tables ({name: raw-text DataFrame}) here; returns findings.json's content. The spec's value list changes are
    applied while it runs and the previous ones put back after."""
    import numpy as np
    import pandas as pd

    from . import editor
    from . import filing_rules as fr
    library = library_available() if library is None else library
    rs = rule_set_of(spec)
    ctx = context_of(spec, library)
    ctx.tables = tables
    skip = {k: set(v) for k, v in (spec.get("skip") or {}).items()}
    changes = (spec.get("context") or {}).get("value_list_changes")
    before = dict(editor._VALUE_LIST_CHANGES)
    out = {}
    try:
        if changes is not None:
            editor.set_value_list_changes(changes)
        for name, df in tables.items():
            own = fr.RuleSet(rs.name, rs.version, None, [r for r in rs.rules if r.table == name and r.id not in skip.get(name, set())])
            results, notes = fr.check_frame(own, name, df, ctx)
            rows = []
            for res in results:
                idx = np.flatnonzero(res.fail)
                actual = res.actual
                vals = []
                for i in idx:
                    v = "" if actual is None else actual.iloc[i] if hasattr(actual, "iloc") else ""
                    vals.append("" if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v)[:MAX_ACTUAL])
                rows.append({"rule_id": res.rule.id, "severity": res.rule.severity, "rows": [int(i) for i in idx], "actual": vals})
            out[name] = {"rows": int(len(df)), "results": rows, "notes": notes}
    finally:
        if changes is not None:
            editor.set_value_list_changes(before)
    return {"tables": out, "library": library}


def read_table(path: Path):
    """A table file as raw text: every value a string, blanks kept as blanks."""
    import pandas as pd
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def check(spec_uri: str, work: Path | None = None, log=print) -> tuple[int, dict | None]:
    """Run one check spec; returns (exit code, receipt or None when the spec was refused)."""
    import shutil

    from . import __version__, storage
    t0, started = time.time(), _now()
    own_work = work is None
    work = Path(tempfile.mkdtemp(prefix="recet_check_")) if own_work else Path(work)
    work.mkdir(parents=True, exist_ok=True)
    try:
        try:
            f = storage.for_uri(spec_uri).fetch(spec_uri, work / "check.json")
            spec_sha = _sha(f)
            try:
                spec = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CheckRefused(f"the check spec is not valid JSON: {exc}") from exc
            errors = validate(spec)
            if errors:
                raise CheckRefused("; ".join(errors))
            release = (spec.get("engine") or {}).get("release")
            if release and release != __version__:
                raise CheckRefused(f"engine.release {release} was asked for; this worker is recet {__version__}")
            files, inputs = {}, {}
            for i, (name, v) in enumerate(spec["tables"].items()):
                uri, pinned = _uri(v)
                local = storage.for_uri(uri).fetch(uri, work / "tables" / f"{i:02d}.csv")
                digest = _sha(local)
                if pinned and pinned != digest:
                    raise CheckRefused(f"tables.{name}: SHA-256 {digest} is not the pinned {pinned} ({uri})")
                files[name] = local
                inputs[name] = {"uri": uri, "sha256": digest, "bytes": local.stat().st_size}
        except (CheckRefused, storage.StorageError, TypeError) as exc:
            print(f"recet worker: check refused: {exc}", file=sys.stderr)
            return 2, None

        log(f"recet worker {__version__}: check {spec['job_id']}")
        status, error, findings = "succeeded", None, None
        try:
            tables = {name: read_table(p) for name, p in files.items()}
            for name, df in tables.items():
                inputs[name]["rows"] = int(len(df))
            findings = run_check(spec, tables)
        except Exception as exc:                                       # a failed check still gets a receipt
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log(traceback.format_exc())
        out_dir = work / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = {}
        if findings is not None:
            fp = out_dir / "findings.json"
            fp.write_text(json.dumps(findings) + "\n", encoding="utf-8")
            outputs["findings.json"] = {"uri": storage.join(spec["outputs"]["uri"], "findings.json"), "sha256": _sha(fp), "bytes": fp.stat().st_size}
        rules_json = json.dumps(spec["rule_set"]["rules"], sort_keys=True).encode()
        receipt = {
            "receipt_version": RECEIPT_VERSION, "kind": "check", "job_id": spec["job_id"], "status": status, "error": error,
            "caller": spec.get("caller"), "started": started, "finished": _now(), "seconds": round(time.time() - t0, 2),
            "engine": {"release": __version__, "python": sys.version.split()[0]},
            "spec": {"sha256": spec_sha, "check_spec_version": spec["check_spec_version"]},
            "rule_set": {"code": spec["rule_set"].get("code"), "version": spec["rule_set"].get("version"), "rules": len(spec["rule_set"]["rules"]),
                         "sha256": hashlib.sha256(rules_json).hexdigest()},
            "tables": inputs,
            "library": (findings or {}).get("library"),
            "findings": sum(len(r["rows"]) for t in (findings or {}).get("tables", {}).values() for r in t["results"]) if findings else None,
            "outputs": outputs,
        }
        (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str) + "\n", encoding="utf-8")
        storage.put_dir(out_dir, spec["outputs"]["uri"])
        log(f"{status}: findings sent to {spec['outputs']['uri']}")
        return (0 if status == "succeeded" else 1), receipt
    finally:
        if own_work:
            shutil.rmtree(work, ignore_errors=True)
