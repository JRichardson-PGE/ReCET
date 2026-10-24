"""Command line for recet.

    recet                                   open the graphical interface (same as `recet gui`)
    recet run --measures M.csv [--programcost P.csv] --acc 2026 --out results
    recet run --config job.json             a saved configuration (the GUI's "Save settings" writes one)
    recet run --workbook job.xlsx --acc 2026 --out results      a job workbook instead of the CSVs
    recet preflight --measures M.csv --acc 2026
    recet template all --out templates      every template + INPUT_FORMATS.md
    recet template workbook --out job.xlsx  an Excel job workbook (Measure, ProgramCost, RunParameters, SWAllocation, GasProfiles, Profiles8760)
    recet template export-profile --end-use "28 = Res. New Const. Lighting" --pa PGE --sector Residential --out shape.csv
    recet check acc-electric FILE | acc-gas FILE | profiles-8760 FILE | profiles-gas FILE | run-parameters FILE | sw-factors FILE | programs FILE
    recet data status | set-dir PATH | build DEST | verify DIR [--full] | pack DIR OUT | unpack PARTS DEST | clear-cache
    recet data build-system-zones [OUT]     rebuild the System climate zone catalog (every library profile x each built-in ACC; ~4 minutes)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def _add_run_options(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("inputs")
    g.add_argument("--config", type=Path, help="a JSON RunConfig (other options override it)")
    g.add_argument("--measures", help="CET Measure.csv (pipe- or comma-delimited)")
    g.add_argument("--programcost", help="CET ProgramCost.csv (optional)")
    g.add_argument("--workbook", help="an Excel job workbook instead of the CSVs (Measure sheet required; see `recet template workbook`)")
    g.add_argument("--out", dest="out_dir", help="output folder (a run sub-folder is created inside it)")
    g.add_argument("--name", dest="run_name", help="run folder name prefix (default: a timestamp)")
    g = ap.add_argument_group("ACC version and supplied files")
    g.add_argument("--acc", dest="acc_version", help="ACC version: 2021, 2022, 2024, 2026, or a new label defined by the files below")
    g.add_argument("--acc-electric-file", help="electric ACC file: replaces the built-in one for --acc (or defines a new version)")
    g.add_argument("--acc-gas-file", help="gas ACC file: same")
    g.add_argument("--settings-from", type=int, help="ACC version whose run parameters / E3 tables a NEW ACC label uses (default: the newest built-in)")
    g.add_argument("--run-parameters", dest="run_parameters_file", help="run parameters per PA (discount rates, base year, gas CO2): replace the built-in values")
    g.add_argument("--sw-factors", dest="sw_factors_file", help="statewide allocation factors: replace / extend the built-in table (used with --allocate-statewide)")
    g.add_argument("--programs", dest="programs_file", help="program metadata (CEDARS Programs list layout): replace / extend the built-in table")
    g.add_argument("--profiles-8760", dest="profiles_8760_file", help="supplied 8,760 electric profiles (replace matching library profiles)")
    g.add_argument("--profiles-gas", dest="profiles_gas_file", help="supplied gas monthly profiles (replace matching library profiles)")
    g.add_argument("--no-normalize-profiles", dest="normalize_profiles", action="store_false", default=None, help="error instead of rescaling profiles that do not sum to 1")
    g.add_argument("--data-dir", dest="reference_dir", help="reference data folder (the big files); see `recet data status`")
    g = ap.add_argument_group("modes")
    g.add_argument("--components", action="store_true", default=None, help="price every ACC component separately (ElecValue_*/GasValue_* columns)")
    g.add_argument("--portfolio-8760", action="store_true", default=None, help="write the combined 8,760 electric load profile")
    g.add_argument("--profile-years", type=int, nargs="+", help="calendar years for the combined profile (default: First Year)")
    g.add_argument("--market-effects", type=float, choices=[0.0, 0.05], help="CET Market Effects, applied to benefits and costs: 0 or 0.05")
    g.add_argument("--no-societal", dest="include_societal", action="store_false", default=None, help="skip SCB/SCH (about 3x faster)")
    g.add_argument("--no-emissions", dest="emissions", action="store_false", default=None, help="skip the CO2/NOx/PM10 columns")
    g.add_argument("--allocate-statewide", action="store_true", default=None, help="split statewide (SW_) programs across the IOUs")
    g.add_argument("--apply-exclude-from-ce", action="store_true", default=None,
                   help="leave programs flagged Exclude_From_CE in the program metadata out of the portfolio summary (they are still priced)")
    g.add_argument("--base-year", type=int, help="CET's First Year (default: earliest claim year)")
    g.add_argument("--cet-outputs", help="a CET *_outputs.csv to compare against")
    g.add_argument("--compare-with", help="also run this other ACC version (built-in) and write a per-measure comparison")
    g.add_argument("--format", dest="output_format", choices=["csv", "csv.zip", "parquet", "xlsx"],
                   help="table format of the outputs (csv.zip: each CSV inside a zip, about a fifth of the size; xlsx: one formatted Excel file per table)")
    g.add_argument("--preflight-only", action="store_true", default=None, help="check the inputs and stop")
    g.add_argument("--excel-max-rows", type=int, help="with --format xlsx, a job with more measures than this writes every table as --excel-fallback instead (default 50,000)")
    g.add_argument("--excel-fallback", choices=["csv.zip", "csv", "parquet"], help="the format of every table of a job over --excel-max-rows (default csv.zip)")
    g.add_argument("--parquet-row-group", type=int, help="rows per parquet row group when a large job is written in parts (default 200,000; smaller uses less memory)")
    g.add_argument("--chunk-size", type=int, help="run a job with more Measure rows than this in parts of this many, to bound memory (default 20,000; 0 = never)")


def _config_from_args(a: argparse.Namespace):
    from .jobs import RunConfig
    import json
    base = json.loads(a.config.read_text(encoding="utf-8")) if getattr(a, "config", None) else {}
    for k, v in vars(a).items():
        if k in {"config", "cmd", "func"} or v is None:
            continue
        base[k] = v
    return RunConfig.from_dict(base)


def _cmd_run(a: argparse.Namespace) -> int:
    from .jobs import run_compare, run_job
    cfg = _config_from_args(a)
    if a.cmd == "preflight":
        cfg.preflight_only = True
    if not cfg.measures and not cfg.workbook:
        print("error: --measures or --workbook is required (or a --config that has one)", file=sys.stderr)
        return 2
    log = lambda s: print(s, flush=True)
    try:
        if cfg.compare_with and not cfg.preflight_only:
            run_compare(cfg, log)
        else:
            run_job(cfg, log)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def _cmd_gui(a: argparse.Namespace) -> int:
    from .gui import launch
    launch()
    return 0


def _cmd_template(a: argparse.Namespace) -> int:
    from . import paths, templates
    if a.data_dir:
        paths.set_reference_dir(a.data_dir)
    if a.kind == "all":
        for p in templates.write_all(a.out, a.acc):
            print(p)
    elif a.kind == "export-profile":
        if not (a.end_use and a.pa and a.sector):
            print("export-profile needs --end-use, --pa and --sector", file=sys.stderr)
            return 2
        print(templates.export_library_profile(a.end_use, a.pa, a.sector, a.out))
    else:
        print(templates.write_template(a.kind, a.out, a.acc))
    return 0


def _cmd_check(a: argparse.Namespace) -> int:
    from . import overrides
    try:
        if a.what == "acc-electric":
            chk = overrides.check_electric_acc(a.file)
        elif a.what == "acc-gas":
            chk = overrides.check_gas_acc(a.file)
        elif a.what == "profiles-8760":
            u = overrides.load_user_profiles(a.file)
            print(u.summary().to_string(index=False))
            print("\n".join(u.notes))
            return 0
        elif a.what == "run-parameters":
            table, notes = overrides.load_run_parameters(a.file)
            print(table.to_string())
            print("\n".join(notes))
            return 0
        elif a.what == "programs":
            from . import programs
            try:
                table, notes = programs.load_programs(a.file)
            except programs.ProgramsError as exc:
                print(f"NOT USABLE: {exc}")
                return 1
            print(f"{len(table):,} programs, PAs: {', '.join(sorted(table['PA'].unique()))}")
            print("\n".join(notes))
            return 0
        elif a.what == "sw-factors":
            table, notes = overrides.load_sw_factors(a.file)
            print(f"{len(table)} rows, {table['ProgramID'].nunique()} programs, years {table['Year'].min()}-{table['Year'].max()}")
            print("\n".join(notes))
            return 0
        else:
            prof, notes = overrides.load_user_gas_profiles(a.file)
            print("profiles:", ", ".join(prof))
            print("\n".join(notes))
            return 0
    except overrides.OverrideError as exc:
        print(f"NOT USABLE: {exc}")
        return 1
    print(chk.text())
    print("usable" if chk.ok else "NOT USABLE")
    return 0 if chk.ok else 1


def _folder_gb(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1e9


def _cmd_data(a: argparse.Namespace) -> int:
    from . import datapack, paths
    if a.action == "status":
        print(f"reference data folder: {paths.reference_dir() or '(not set)'}   [{paths.reference_dir_source()}]")
        for r in paths.ref_status():
            state = f"found ({r['size_gb']} GB) {r['path']}" if r["found"] else ("MISSING" if r["required"] else "not present (optional)")
            print(f"  {r['file']:44s} {state}")
        cache = paths.cache_dir()
        print(f"cache folder: {cache if cache is not None else '(disabled by RECET_NO_CACHE)'}" + (f"  ({_folder_gb(cache):.2f} GB; `recet data clear-cache` removes it)" if cache is not None and cache.exists() else ""))
        return 0 if not paths.missing_required() else 1
    if a.action == "clear-cache":
        cache = paths.cache_dir()
        if cache is None or not cache.exists():
            print("no cache to remove")
            return 0
        import shutil
        gb = _folder_gb(cache)
        shutil.rmtree(cache, ignore_errors=True)
        print(f"removed {cache} ({gb:.2f} GB); it is rebuilt on the next run")
        return 0
    if a.action == "build-system-zones":
        from .pipeline import system_zones
        system_zones.build_catalog(Path(a.path) if a.path else None)
        return 0
    if a.action == "set-dir":
        print(f"saved: {paths.save_config(data_dir=str(Path(a.path).expanduser()))}")
        return 0
    if a.action == "build":
        datapack.build_reference_folder(a.path, link=a.link)
        return 0
    if a.action == "verify":
        return 0 if datapack.verify(a.path, full=a.full) else 1
    if a.action == "pack":
        datapack.pack(a.path, a.dest, a.part_gb)
        return 0
    datapack.unpack(a.path, a.dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="recet", description="ReCET, the rebuilt CEDARS Cost Effectiveness Tool", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"recet {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the graphical interface (default)").set_defaults(func=_cmd_gui)
    for name, help_ in (("run", "run a job"), ("preflight", "check a job's inputs without pricing")):
        p = sub.add_parser(name, help=help_, formatter_class=argparse.RawDescriptionHelpFormatter)
        _add_run_options(p)
        p.set_defaults(func=_cmd_run)
    p = sub.add_parser("template", help="write a template for a supplied input")
    p.add_argument("kind", choices=["all", "workbook", "profiles-8760", "profiles-gas", "acc-electric", "acc-gas", "run-parameters", "sw-factors", "programs",
                                    "measure", "export-profile"])
    p.add_argument("--out", required=True, help="file (a folder for `all`)")
    p.add_argument("--acc", default="2026", help="which built-in ACC the ACC templates are sliced from")
    p.add_argument("--data-dir", help="reference data folder (ACC templates are real slices when it is available)")
    p.add_argument("--end-use"), p.add_argument("--pa"), p.add_argument("--sector")
    p.set_defaults(func=_cmd_template)
    p = sub.add_parser("check", help="validate a supplied file")
    p.add_argument("what", choices=["acc-electric", "acc-gas", "profiles-8760", "profiles-gas", "run-parameters", "sw-factors", "programs"])
    p.add_argument("file")
    p.set_defaults(func=_cmd_check)
    p = sub.add_parser("data", help="manage the reference data folder")
    p.add_argument("action", choices=["status", "set-dir", "build", "verify", "pack", "unpack", "clear-cache", "build-system-zones"])
    p.add_argument("path", nargs="?", help="folder (set-dir/build/verify/pack), parts folder (unpack), or output folder (build-system-zones; default: the package's own)")
    p.add_argument("dest", nargs="?", help="output folder (pack/unpack)")
    p.add_argument("--full", action="store_true", help="verify: also check SHA-256")
    p.add_argument("--link", action="store_true", help="build: hard-link instead of copy (same drive)")
    p.add_argument("--part-gb", type=float, default=4.0, help="pack: part size in GB")
    p.set_defaults(func=_cmd_data)
    return ap


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        a.func = _cmd_gui
    if getattr(a, "cmd", None) == "data" and a.action in {"set-dir", "build", "verify", "pack", "unpack"} and not a.path:
        ap.error(f"data {a.action} needs a path")
    if getattr(a, "cmd", None) == "data" and a.action in {"pack", "unpack"} and not a.dest:
        ap.error(f"data {a.action} needs a destination")
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
