"""A run on a CET replica engine (D13, BR-06.4): `RunConfig.engine` names the engine, "" (or "recet") for ReCET's own hourly pricing, or
"cet-<release>" for an exact replica of that CET release (`releases.RELEASES`). The web app's New run, `recet run --engine` and `jobs.run_job`
all come here for a replica.

A replica run writes the release's own four output tables (SavedCE, SavedSavings, SavedEmissions, SavedCost; CET's columns in CET's order) and a
measure-level table in ReCET's layout: the input columns, then every Saved* column under ReCET's name where ReCET renamed it
(fieldnames.cet_outputs_to_recet), then the program metadata columns. So the Results and Compare pages read it as they read a ReCET run, and two
runs on different engines can be put side by side. run_settings.json records the engine (`engine` block).

A replica prices what the release priced and nothing else: the options that change ReCET's pricing (supplied ACC or profile files, run parameters,
statewide allocation, components, the combined 8,760, a two-version comparison) have no meaning for it and are refused (`problems`), and a filing
accepts only ReCET's current release (D13), so the CEDARS filing checks are refused too.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .releases import RELEASES, Release

PREFIX = "cet-"
RECET = "recet"


def release_of(engine: str | None) -> Release | None:
    """The CET release an engine names ("cet-25.1" -> 25.1), or None for ReCET's own engine ("" or "recet"). Raises ValueError for an unknown one."""
    e = (engine or "").strip().lower()
    if e in ("", RECET):
        return None
    name = e[len(PREFIX):] if e.startswith(PREFIX) else e
    if name not in RELEASES:
        raise ValueError(f"unknown engine {engine!r}: give {RECET!r} or one of {', '.join(PREFIX + r for r in RELEASES)}")
    return RELEASES[name]


def engine_key(release: Release | None) -> str:
    return RECET if release is None else PREFIX + release.name


def recet_label() -> str:
    from .. import __version__
    return f"ReCET {__version__} (hourly)"


def label(engine: str | None) -> str:
    """'ReCET 0.1.72 (hourly)' or 'CET 25.1 replica'."""
    rel = release_of(engine)
    return recet_label() if rel is None else rel.label


def engines() -> list[tuple[str, str]]:
    """(key, label) of every engine, ReCET's own first, then the replicas newest first."""
    return [(RECET, recet_label())] + [(PREFIX + r.name, r.label) for r in RELEASES.values()]


def _versions(release: Release) -> pd.DataFrame:
    from importlib import resources
    with resources.files("recet.data").joinpath(f"cet_versions/{release.name}/CETAvoidedCostVersions.csv").open(encoding="utf-8") as f:
        return pd.read_csv(f, dtype=str)


def accs(release: Release) -> dict[int, str]:
    """{ACC year: CET avoided-cost Version} of the versions the release offers (its CETAvoidedCostVersions less `not_offered`), oldest first."""
    from .. import versions
    skip = dict(release.not_offered)
    out = {}
    for v in sorted(_versions(release).Version, key=float):
        acc = versions.acc_of_cet_version(v)
        if acc is not None and v not in skip:
            out[int(acc)] = v
    return out


def cet_version(release: Release, acc) -> str:
    """The CET Version that prices the `acc` ACC on `release`, or ValueError naming the ACCs it has."""
    have = accs(release)
    try:
        return have[int(str(acc).strip())]
    except (KeyError, ValueError):
        raise ValueError(f"the {release.label} has no {acc} ACC; it has the {', '.join(f'{a}' for a in have)} ACCs") from None


# RunConfig fields a replica cannot honour: (field, what it is). A set field is refused, with this text.
REFUSED = [
    ("acc_electric_file", "a supplied electric ACC file"), ("acc_gas_file", "a supplied gas ACC file"),
    ("settings_from", "run parameters from another ACC"), ("profiles_8760_file", "supplied 8,760 profiles"),
    ("profiles_gas_file", "supplied gas profiles"), ("run_parameters_file", "supplied run parameters"),
    ("sw_factors_file", "supplied statewide allocation factors"), ("allocate_statewide", "statewide allocation"),
    ("components", "ACC components"), ("portfolio_8760", "the combined 8,760 profile"),
    ("compare_with", "a comparison with another ACC"), ("cet_outputs", "a comparison with a CET outputs file"),
    ("filing_validation", "CEDARS filing checks (a filing accepts only ReCET's current release)"),
    ("preflight_only", "Check inputs only (that is ReCET's input check)"),
]


def problems(cfg) -> list[str]:
    """Why `cfg` cannot run on its engine: each option a replica cannot use, and an ACC the release does not have. Empty for ReCET's own engine."""
    rel = release_of(cfg.engine)
    if rel is None:
        return []
    out = [f"the {rel.label} cannot use {what}" for fld, what in REFUSED if getattr(cfg, fld, None)]
    try:
        cet_version(rel, cfg.acc_version)
    except ValueError as exc:
        out.append(str(exc))
    return out


def _read(path: str) -> pd.DataFrame:
    from .. import fieldnames
    from ..pipeline.delimited import read_cet_csv
    return fieldnames.to_cet(read_cet_csv(path, dtype=str, keep_default_na=False, na_values=[""]))


def cet_inputs(measures: pd.DataFrame, programs: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """A CEDARS upload's Measure rows as CET's input table has them (InputMeasureCEDARS): a CEDARS Measure.csv has no PA and calls the fuel
    FuelID. Each record's PA is its program's (ProgramCost PA by PrgID, else the PrgID's prefix); a statewide record (PA SW, for which CET has no
    avoided costs or settings) is priced on its IOU_AC_Territory, as the reference runs were made (CEDARS-Research poc/cet_reference_jobs.py). A
    file that already has CET's PA column is used as it is. Returns the table and the number of statewide records priced on their territory."""
    from .sql import key
    m = measures.copy()
    if "FuelType" not in m and "FuelID" in m:
        m = m.rename(columns={"FuelID": "FuelType"})
    if "PA" in m:
        return m, 0
    prg = m["PrgID"].astype("string")
    pa = pd.Series(pd.NA, index=m.index, dtype="string")
    if {"PA", "PrgID"} <= set(programs.columns):
        by_prg = dict(zip(key(programs["PrgID"].astype("string")), programs["PA"].astype("string")))
        pa = key(prg).map(by_prg).astype("string")
    pa = pa.fillna(prg.str.split("_").str[0])
    sw = key(pa).eq(key(pd.Series(["SW"]))[0]).fillna(False).astype(bool)
    if "IOU_AC_Territory" in m:
        terr = m["IOU_AC_Territory"].astype("string").str.strip()
        sw &= terr.fillna("").ne("")
        pa = pa.where(~sw, terr)
    else:
        sw &= False
    pa = pa.str.strip()
    m.insert(0, "PA", pa.astype(object).where(pa.notna(), None))
    return m, int(sw.sum())


def measure_level(raw: pd.DataFrame, res, acc_label: str) -> pd.DataFrame:
    """The measure-level table of a replica run: the input columns (ReCET's names, as filed), each Saved* column under ReCET's name (a column already
    there is not repeated), one row per input measure in input order; a measure the release did not price (e.g. a PA it has no avoided costs for)
    has blank outputs. Then the program metadata columns."""
    from .. import fieldnames, outputs
    frame = raw.reset_index(drop=True).copy()
    frame.insert(0, "Used_ACCVersion", acc_label)
    ids = frame["CEInputID"].astype(str).str.strip()
    have = set(frame.columns) | {"ID", "JobID", "CET_ID"}
    add = {}
    for t in res.tables().values():
        t = fieldnames.cet_outputs_to_recet(t).copy()
        t.index = t["CET_ID"].astype(str).str.strip()
        for c in t.columns:
            if c not in have:
                add[c] = t[c].reindex(ids).to_numpy()
                have.add(c)
    frame = pd.concat([frame, pd.DataFrame(add, index=frame.index)], axis=1)
    return outputs.attach_programs(frame, frame["PrgID"])


def run_job(cfg, log=print):
    """Run `cfg` (a jobs.RunConfig whose engine is a replica) end to end; returns a jobs.RunResult. Raises ValueError for `problems`."""
    from .. import jobs, outputs, paths, progress, results_view, versions, workbook
    from . import QuarterlyTables, run
    t_start = time.time()
    rel = release_of(cfg.engine)
    bad = problems(cfg)
    if bad:
        raise ValueError("; ".join(bad))
    if not cfg.measures and not cfg.workbook:
        raise ValueError("no Measure.csv or job workbook given")
    cfg = jobs.RunConfig.from_dict(asdict(cfg))
    if cfg.reference_dir:
        paths.set_reference_dir(cfg.reference_dir)
    progress.report("preparing")
    run_dir = jobs._run_folder(cfg, str(cfg.acc_version))
    result = jobs.RunResult(run_dir=run_dir)
    if cfg.workbook:
        log(f"reading job workbook {cfg.workbook}")
        for fld, f in workbook.extract(cfg.workbook, run_dir / "_prepared_inputs" / "workbook").items():
            if fld in ("measures", "programcost") and not getattr(cfg, fld):
                setattr(cfg, fld, str(f))
    version = cet_version(rel, cfg.acc_version)
    acc = int(cfg.acc_version)
    tag = f"acc{acc}"
    log(f"engine: {rel.label} (CET's own calculation, its errors included; backup {rel.backup}, {rel.built})")
    log(f"{versions.acc_short(acc)}: CET avoided-cost Version {version}")
    for fld, what in (("include_societal", "the societal tests"), ("emissions", "the emissions")):
        if not getattr(cfg, fld):
            note = f"{what} cannot be turned off on the {rel.label}: it writes what the release writes"
            result.notes.append(note)
            log(f"note: {note}")
    if cfg.acc_purpose:
        result.notes.append("the ACC-for-program-year check is a ReCET check and was not run on this engine")

    log("\n== loading ==")
    progress.plan(False, cfg.output_format, True, True, False)
    progress.report("loading")
    programs = _read(cfg.programcost) if cfg.programcost else pd.DataFrame(columns=["PA", "PrgID", "ClaimYearQuarter"])
    measures, on_territory = cet_inputs(_read(cfg.measures), programs)
    if on_territory:
        note = (f"{on_territory:,} statewide record(s) are priced on their IOU_AC_Territory (CEDARS does not pass statewide programs to CET, which has "
                "no avoided costs or settings for PA SW)")
        result.notes.append(note)
        log(f"note: {note}")
    if "ACCVersion" in measures and measures["ACCVersion"].fillna("").astype(str).str.strip().ne("").any():
        note = f"the ACCVersion column is a ReCET option: the {rel.label} prices every record on the {versions.acc_short(acc)}"
        result.notes.append(note)
        log(f"note: {note}")
    first_year = cfg.base_year or int(measures["ClaimYearQuarter"].astype(str).str[:4].astype(int).min())
    me = float(cfg.market_effects or 0.0)
    log(f"{len(measures):,} measures, First Year {first_year}, market effects {me:.0%} on benefits and costs")
    tl = time.time()
    tables = QuarterlyTables.from_release(rel.name, version, None, measures)
    log(f"the release's Version {version} tables read in {time.time() - tl:.0f}s ({len(tables.ac_elec):,} electric and {len(tables.ac_gas):,} gas "
        "avoided-cost rows for this job)")

    log(f"\n== pricing ({rel.label}) ==")
    progress.report("pricing")
    tp = time.time()
    res = run(measures, programs, rel.name, tables, first_year=first_year, me_bens=me, me_cost=me)
    priced = len(res.ce)
    log(f"priced in {time.time() - tp:.0f}s: {priced:,} of {len(measures):,} measures in SavedCE")
    if priced < len(measures):
        note = (f"{len(measures) - priced:,} measure(s) are not in the release's outputs (CET leaves out a measure whose PA has no avoided costs or "
                f"settings, e.g. a statewide program's): their output columns are blank")
        result.notes.append(note)
        log(f"note: {note}")

    log("\n== writing results ==")
    progress.report("writing")
    excel = jobs.excel_limit(cfg, result, log)
    cfg.output_format = jobs.run_format(cfg, len(measures), log)
    for name, df in res.tables().items():
        result.files[name] = outputs.write_table(df, run_dir / f"{name}_{tag}", cfg.output_format, excel)
    frame = measure_level(outputs.read_raw(cfg.measures), res, versions.acc_short(acc))
    result.frame = frame
    writer = outputs.TableWriter(run_dir / f"measure_level_{tag}", cfg.output_format, len(frame), cfg.parquet_row_group, keep_parquet=True, excel=excel,
                                 select=cfg.output_template or None)
    writer.write(frame)
    result.files["measure_level"] = writer.close()
    from .. import key_metrics
    jobs.write_template_files(cfg, writer, key_metrics.sums(frame), run_dir, tag, excel, result, log)
    summary = outputs.summarize(frame, cfg.apply_exclude_from_ce)
    result.files["portfolio_summary"] = outputs.write_table(summary, run_dir / f"portfolio_summary_{tag}", cfg.output_format, excel)
    result.totals = {k: float(v) for k, v in summary.iloc[0].items() if isinstance(v, (int, float)) and k != "measures"}

    progress.report("finishing")
    write_settings(cfg, rel, version, acc, run_dir, result, t_start, priced, first_year)
    results_view.keep_group_sums(run_dir, frame)
    log(f"\ndone in {time.time() - t_start:.0f}s -- results in {run_dir}")
    progress.report("done")
    return result


def engine_record(engine: str | None, version: str | None = None) -> dict:
    """run_settings.json's `engine` block: which engine priced the run."""
    rel = release_of(engine)
    if rel is None:
        from .. import __version__
        return {"key": RECET, "label": recet_label(), "package_version": __version__}
    rec = {"key": engine_key(rel), "label": rel.label, "release": rel.name, "backup": rel.backup, "built": rel.built,
           "replica": "CET's own calculation reproduced exactly, its errors included (CALCULATIONS C20)"}
    if version is not None:
        rec["cet_version"] = version
    return rec


def write_settings(cfg, rel: Release, version: str, acc: int, run_dir: Path, result, t_start: float, priced: int, first_year: int) -> None:
    """run_settings.json of a replica run, in the layout jobs.write_settings uses (the ACC block names the release's tables, not ACC files)."""
    from datetime import datetime
    from .. import __version__, paths, versions
    settings = {
        "package_version": __version__, "finished": datetime.now().isoformat(timespec="seconds"), "seconds": round(time.time() - t_start),
        "engine": engine_record(cfg.engine, version),
        "config": asdict(cfg),
        "acc": {"key": str(acc), "label": versions.acc_short(acc), "cet_version": version, "tables": f"CET {rel.name} backup, Version {version}"},
        "reference_dir": {"path": str(paths.reference_dir() or ""), "source": paths.reference_dir_source()},
        "inputs": {"measures": str(cfg.measures), "programcost": str(cfg.programcost or ""), "measures_priced": priced, "first_year": first_year},
        "totals": result.totals,
    }
    if result.notes:
        settings["notes"] = result.notes
    (run_dir / "run_settings.json").write_text(json.dumps(settings, indent=2, default=str), encoding="utf-8")
    result.files["run_settings"] = run_dir / "run_settings.json"
