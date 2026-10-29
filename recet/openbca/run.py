"""Run OpenBCA on a job's workbooks from a pinned OpenBCA checkout, in the run folder (step 1 of the OpenBCA module scope).

    find_checkout()   the OpenBCA checkout and its interpreter: RECET_OPENBCA_HOME (a git checkout of a release tag, with `uv sync` done), and
                      RECET_OPENBCA_PYTHON (default: the checkout's .venv)
    run_openbca()     stage a copy of the checkout's SQLmesh projects in <run>/openbca/, put the workbooks in its input_templates folder under OpenBCA's
                      fixed names, run engine.py with OpenBCA's interpreter, and write OpenBCA's own output tables in the run's format
    compare_dbs()     comparison C1: every OpenBCA table of two runs' databases

The checkout is copied, never written to: each run has its own projects, SQLmesh cache and database, so runs never share state. Only the copy's
input_templates folder is ours. OpenBCA's own outputs are written as they stand; nothing is recomputed on them (Jake, 2026-10-28).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from .. import outputs

Log = Callable[[str], None]
ENGINE = Path(__file__).with_name("engine.py")
# What a checkout must have, and what of it a run copies (the rest -- the Streamlit app, demo files, images -- is not needed).
PROJECT_DIRS = ("config", "core", "excel_input_parsing")
SKIP = shutil.ignore_patterns(".cache", "__pycache__", "*.db", "*.xlsm", "*.xlsx")
# OpenBCA reads the workbooks under these names only.
PROGRAM_INPUT = "OpenBCA_Program_Input.xlsm"
CONFIGURATION = "OpenBCA_Configuration.xlsm"


@dataclass
class Checkout:
    home: Path
    python: Path

    @property
    def version(self) -> str:
        """The checkout's release tag (or commit), for the run log and run_settings."""
        try:
            return subprocess.run(["git", "-C", str(self.home), "describe", "--tags", "--always"], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"


@dataclass
class OpenBCAResult:
    run_dir: Path
    files: dict[str, Path] = field(default_factory=dict)
    seconds: float = 0.0
    version: str = ""


def find_checkout(home: str | Path | None = None, python: str | Path | None = None) -> Checkout:
    home = home or os.environ.get("RECET_OPENBCA_HOME")
    home = Path(home).expanduser() if home else None
    if home is None or not home.is_dir():
        raise FileNotFoundError("no OpenBCA checkout: set RECET_OPENBCA_HOME to a checkout of github.com/openbca/openbca (a release tag) and run `uv sync` in it")
    missing = [d for d in PROJECT_DIRS if not (home / d).is_dir()]
    if missing:
        raise FileNotFoundError(f"{home} is not an OpenBCA checkout (no {', '.join(missing)})")
    py = Path(python or os.environ.get("RECET_OPENBCA_PYTHON") or home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not py.exists():
        raise FileNotFoundError(f"no OpenBCA interpreter at {py}: run `uv sync` in {home} or set RECET_OPENBCA_PYTHON")
    return Checkout(home.resolve(), py.absolute())  # absolute, not resolved: resolving the venv's symlink would leave the venv


# A jurisdiction's tables (ca_config.TABLES) replace these parser models in the staged copy: each reads <workspace>/jurisdiction/<table>.parquet.
JURISDICTION_MODEL = """from typing import Any
from pathlib import Path
from sqlmesh import model, ExecutionContext
import pandas as pd

# Written by ReCET (recet/openbca/run.py): this run's jurisdiction table, built by ReCET, replaces OpenBCA's Configuration-workbook parser.
SOURCE = {source!r}   # a plain string: SQLmesh serializes a model's globals, and a Path cannot be


@model(name="openbca_input.{table}", kind="FULL", columns={columns!r})
def execute(context: ExecutionContext, **kwargs: Any):
    df = pd.read_parquet(Path(SOURCE))
    if df.empty:
        yield from ()
    else:
        yield df
"""
JURISDICTION_MODELS = {
    "avoided_costs_ts": ("openbca_input_avoided_costs_ts.py", {"avoided_cost": "string", "avoided_cost_subset": "string", "year": "int", "quarter": "int",
                         "month": "int", "day_of_year": "int", "hour_of_day": "int", "hour_of_year": "int", "avoided_cost_value": "float"}),
    "value_stream_groups": ("openbca_input_value_stream_groups.py", {"avoided_cost": "string", "impact_category": "string", "include_in_test": "boolean",
                            "calc_type": "string", "pct_adder": "float", "value_stream_group": "string", "marginal_ghg": "boolean"}),
    "global_parameters": ("openbca_input_global_parameters.py", {"inflation_rate": "float", "dollar_year": "int", "discount_rate": "float",
                          "discount_cadence": "int", "electric_line_loss": "float", "peak_capacity_line_loss": "float",
                          "natural_gas_line_loss": "float", "cost_treatment": "string"}),
    "first_year_avoided_costs_by_id": ("first_year_avoided_costs_by_id.py", {"id": "string", "year": "int", "value_stream": "string",
                                       "gross_dollar_value": "float"}),
}


def stub_configuration(path: Path) -> Path:
    """A Configuration workbook with only what OpenBCA's measures parser still reads when the jurisdiction comes from ReCET: the five custom value
    streams on 'Configuration Data' (the 5 rows before the last 5 under the header in C:D), named CUSTOM 1-5 with no impact category, which the parser
    marks STANDARD_n with no savings. Every other Configuration read is replaced (JURISDICTION_MODELS)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Configuration Data"
    ws["C4"], ws["D4"] = "Value Stream", "Impact Category"
    for i in range(10):
        ws.cell(row=5 + i, column=3, value=f"CUSTOM {i + 1}" if i < 5 else "(not used)")
    wb.save(path)
    return path


def stage(checkout: Checkout, workspace: Path, program_input: Path, configuration: Path | None = None, jurisdiction: Path | None = None) -> Path:
    """Copy the checkout's projects into `workspace` and the workbooks into its input_templates folder. With `jurisdiction` (a folder of the
    ca_config.TABLES parquet files) the staged parser models for those tables read them instead of a Configuration workbook, and a stub workbook stands
    in for the Configuration when none is given. Returns the workspace."""
    if configuration is None and jurisdiction is None:
        raise ValueError("a Configuration workbook or a jurisdiction folder is needed")
    if workspace.exists():
        shutil.rmtree(workspace)
    for d in PROJECT_DIRS:
        shutil.copytree(checkout.home / d, workspace / d, ignore=SKIP)
    templates = workspace / "excel_input_parsing" / "input_templates"
    templates.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(program_input, templates / PROGRAM_INPUT)
    if configuration is not None:
        shutil.copyfile(configuration, templates / CONFIGURATION)
    else:
        stub_configuration(templates / CONFIGURATION)
    if jurisdiction is not None:
        shutil.copytree(jurisdiction, workspace / "jurisdiction")
        for table, (filename, columns) in JURISDICTION_MODELS.items():
            if not (workspace / "jurisdiction" / f"{table}.parquet").exists():
                raise FileNotFoundError(f"{jurisdiction} has no {table}.parquet")
            (workspace / "excel_input_parsing" / "models" / filename).write_text(JURISDICTION_MODEL.format(
                table=table, columns=columns, source=str((workspace / "jurisdiction" / f"{table}.parquet").resolve())))
    shutil.copyfile(ENGINE, workspace / ENGINE.name)
    return workspace


def _engine(checkout: Checkout, args: list[str], cwd: Path, log: Log) -> int:
    proc = subprocess.Popen([str(checkout.python), ENGINE.name if args[0] == "run" else str(ENGINE), *args], cwd=str(cwd), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"})
    for line in proc.stdout:
        line = line.rstrip()
        if line.strip():
            log(line)
    return proc.wait()


def run_openbca(program_input: str | Path, configuration: str | Path | None, out_dir: str | Path, run_name: str = "", fmt: str = "csv",
                checkout: Checkout | None = None, log: Log = print, jurisdiction: str | Path | None = None) -> OpenBCAResult:
    """Run OpenBCA on one job. Writes <out_dir>/<run_name>/: openbca/ (the staged projects and openbca.db, kept for inspection) and OpenBCA's output
    tables (EXPORTS in engine.py) in `fmt`."""
    checkout = checkout or find_checkout()
    t0 = time.time()
    run_dir = (Path(out_dir) / (run_name.strip() or time.strftime("openbca_%Y%m%d_%H%M%S"))).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    result = OpenBCAResult(run_dir=run_dir, version=checkout.version)
    log(f"OpenBCA {result.version} from {checkout.home}")
    workspace = stage(checkout, run_dir / "openbca", Path(program_input), Path(configuration) if configuration else None,
                      Path(jurisdiction) if jurisdiction else None)
    export = workspace / "export"
    code = _engine(checkout, ["run", str(workspace), str(workspace / "openbca.db"), str(export)], workspace, log)
    if code:
        raise RuntimeError(f"OpenBCA stopped with exit code {code}; see the log above")
    for f in sorted(export.glob("*.parquet")):
        result.files[f.stem] = outputs.write_table(pd.read_parquet(f), run_dir / f.stem, fmt)
    result.seconds = time.time() - t0
    log(f"results in {run_dir}")
    return result


def compare_dbs(db_a: str | Path, db_b: str | Path, checkout: Checkout | None = None, log: Log = print) -> bool:
    """Comparison C1: True when every OpenBCA table of the two databases is the same (rows in any order, numbers to 1e-9)."""
    checkout = checkout or find_checkout()
    return _engine(checkout, ["compare", str(Path(db_a).resolve()), str(Path(db_b).resolve())], Path.cwd(), log) == 0


def main(argv: list[str] | None = None) -> int:
    """`recet openbca run|compare|compare-recet` (see cli.py)."""
    import argparse
    ap = argparse.ArgumentParser(prog="recet openbca")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="run OpenBCA on a Program Input and a Configuration workbook")
    p.add_argument("--program-input", required=True)
    p.add_argument("--configuration", help="an OpenBCA Configuration workbook (or --jurisdiction)")
    p.add_argument("--jurisdiction", help="a folder of jurisdiction tables (recet.openbca.ca_config.build) instead of a Configuration workbook")
    p.add_argument("--out", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--format", default="csv", choices=outputs.FORMATS)
    p = sub.add_parser("compare-recet", help="price a CET job in ReCET and in OpenBCA (TRC and PAC) and compare them measure by measure (C3 / C4)")
    p.add_argument("--measures", required=True)
    p.add_argument("--programcost")
    p.add_argument("--acc", default="2024")
    p.add_argument("--data-dir", help="ReCET's reference data folder (default: as `recet run`)")
    p.add_argument("--base-year", type=int)
    p.add_argument("--out", required=True)
    p = sub.add_parser("compare", help="compare every OpenBCA table of two runs' openbca.db files (C1)")
    p.add_argument("db_a")
    p.add_argument("db_b")
    for p in sub.choices.values():
        p.add_argument("--openbca-home", help="OpenBCA checkout (default: RECET_OPENBCA_HOME)")
    a = ap.parse_args(argv)
    checkout = find_checkout(a.openbca_home)
    if a.action == "run":
        if not (a.configuration or a.jurisdiction):
            ap.error("give --configuration or --jurisdiction")
        run_openbca(a.program_input, a.configuration, a.out, a.name, a.format, checkout, jurisdiction=a.jurisdiction)
        return 0
    if a.action == "compare-recet":
        from ..jobs import RunConfig
        from .translate import compare_job
        cfg = RunConfig(measures=a.measures, programcost=a.programcost, acc_version=a.acc, reference_dir=a.data_dir, base_year=a.base_year)
        print(compare_job(cfg, a.out, checkout).T.to_string(header=False))
        return 0
    return 0 if compare_dbs(a.db_a, a.db_b, checkout) else 1


if __name__ == "__main__":
    sys.exit(main())
