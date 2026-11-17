"""Runs inside OpenBCA's own Python (the checkout's .venv), never imported by ReCET:

    python engine.py run <workspace> <db> <export_dir>
    python engine.py compare <db_a> <db_b>        every OpenBCA table of two runs (comparison C1: hosted vs OpenBCA's own run)

The workspace is a staged copy of an OpenBCA checkout with the job's workbooks in `excel_input_parsing/input_templates/`. This does what OpenBCA's
`model_runners.py openbca_excel_model` does (one SQLmesh plan over both projects) with the database in the run folder, then copies OpenBCA's output
views to parquet. Each table's row count and the process's peak memory are printed for the run log.
"""
import os
import resource
import sys
import time
import warnings
from pathlib import Path

# OpenBCA's own output views, exported as they stand (schema.view -> file stem). Everything else stays in the DuckDB file.
EXPORTS = {
    "core_layer3_finalization.results_summary_by_id": "openbca_results_summary_by_id",
    "core_layer3_finalization.jst_ratio": "openbca_jst_ratio",
    "core_validations.required_parameters_v": "openbca_check_required_parameters",
    "core_validations.unique_ids_v": "openbca_check_unique_ids",
    "core_validations.load_shape_v": "openbca_check_load_shape",
    "core_validations.global_parameters_v": "openbca_check_global_parameters",
    "core_validations.avoided_cost_load_shape_granularity_v": "openbca_check_granularity",
}


def main(workspace: str, db: str, export_dir: str) -> int:
    ws = Path(workspace).resolve()
    os.chdir(ws)
    sys.path.insert(0, str(ws))
    os.environ["DB"] = str(Path(db).resolve())
    os.environ["DBV"] = str(Path(db).resolve().with_name("openbca_input_validation.db"))
    warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
    from sqlmesh.core.console import set_console, TerminalConsole
    from sqlmesh.core.context import Context
    import duckdb

    set_console(TerminalConsole())
    t0 = time.time()
    ctx = Context(paths=[ws / "excel_input_parsing", ws / "core"])
    try:
        ctx.plan(run=True, ignore_cron=True, auto_apply=True)
    finally:
        ctx.close()
    print(f"OpenBCA pipeline: {time.time() - t0:.1f} s", flush=True)

    out = Path(export_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(os.environ["DB"], read_only=True)
    try:
        for view, stem in EXPORTS.items():
            n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
            con.execute(f"COPY (SELECT * FROM {view}) TO '{(out / (stem + '.parquet')).as_posix()}' (FORMAT PARQUET)")
            print(f"  {view}: {n:,} rows", flush=True)
    finally:
        con.close()
    print(f"OpenBCA peak memory: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 ** 2:.2f} GiB", flush=True)
    return 0


SCHEMAS = ("openbca_input", "core_layer0_base", "core_layer1_mappings", "core_layer2_precompute", "core_layer3_finalization", "core_validations")


def compare(db_a: str, db_b: str, digits: int = 10) -> int:
    """Compare every table and view of OpenBCA's schemas in two databases: same tables, same columns, the same rows in any order. Each table is copied to
    parquet from its own database (OpenBCA's views name their database's catalog, "openbca", so the two cannot be attached side by side), then the two
    copies are compared in DuckDB with EXCEPT ALL both ways: exactly first, then with every floating-point value to `digits` significant digits (DuckDB's
    parallel sums need not add in the same order). Prints one line per table; returns the number of tables that differ."""
    import tempfile
    import duckdb

    def tables(con):
        rows = con.execute("SELECT table_schema, table_name FROM information_schema.tables").fetchall()
        return {f"{s}.{t}" for s, t in rows if s in SCHEMAS}

    cons = {"a": duckdb.connect(str(Path(db_a).resolve()), read_only=True), "b": duckdb.connect(str(Path(db_b).resolve()), read_only=True)}
    ta, tb = tables(cons["a"]), tables(cons["b"])
    bad = len(ta ^ tb)
    for name in sorted(ta ^ tb):
        print(f"  only in {'A' if name in ta else 'B'}: {name}", flush=True)
    work = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        for name in sorted(ta & tb):
            files = {}
            for k, con in cons.items():
                files[k] = (Path(tmp) / f"{k}_{name}.parquet").as_posix()
                con.execute(f"COPY (SELECT * FROM {name}) TO '{files[k]}' (FORMAT PARQUET)")
            cols = {k: work.execute(f"DESCRIBE SELECT * FROM read_parquet('{f}')").fetchall() for k, f in files.items()}
            n = work.execute(f"SELECT COUNT(*) FROM read_parquet('{files['a']}')").fetchone()[0]
            note = ""
            if [(c[0], c[1]) for c in cols["a"]] != [(c[0], c[1]) for c in cols["b"]]:
                why = "columns differ"
            else:
                def diff(select):
                    a, b = (f"SELECT {select} FROM read_parquet('{files[k]}')" for k in "ab")
                    return work.execute(f"SELECT (SELECT COUNT(*) FROM ({a} EXCEPT ALL {b})) + (SELECT COUNT(*) FROM ({b} EXCEPT ALL {a}))").fetchone()[0]
                rounded = ", ".join(f"printf('%.{digits}g', \"{c}\")" if t in ("DOUBLE", "FLOAT") else f"\"{c}\"" for c, t, *_ in cols["a"])
                exact = diff("*")
                d = exact and diff(rounded)
                why = f"{d:,} rows differ" if d else ""
                if exact and not d:
                    note = f" (equal to {digits} significant digits; {exact:,} rows differ exactly)"
            bad += bool(why)
            print(f"  {'DIFFERS' if why else 'same   '} {name} ({n:,} rows){': ' + why if why else note}", flush=True)
            for f in files.values():
                Path(f).unlink()
    print(f"{len(ta & tb)} tables compared, {bad} differ", flush=True)
    return bad


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    sys.exit(main(*rest) if cmd == "run" else min(compare(*rest), 1))
