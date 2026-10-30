"""The tie-out report an ACC load (and an engine release) is accepted on (BRD v0.5 BR-06.21): `recet data tie-out FOLDER --acc 2024`.

Three parts, each PASS or FAIL, written as tieout_acc<version>.json and .md:

1. **Structure.** Every (utility, zone, cost test, year) block of the electric ACC has hours 1-8,760 once each; no value is missing; the components add up
   to `total_avoided_cost` (within COMPONENT_TOLERANCE $/MWh); the years run without a gap; the cost tests are TRC, SCT Base and SCT High (TRC only for the
   2021 and 2022 ACCs).
2. **Control totals against the source workbook.** `control_totals` sums every value column per (utility, zone, cost test, year). With `--source-totals`, a
   CSV of the same totals computed from the published workbook by the extraction that made the file (CEDARS-Research `<year> ACC Final Extraction`), every
   total must match within TOTALS_RTOL; without it the part reports the pack's totals only (written as control_totals_acc<version>.csv) and is not a pass.
3. **Reference measures.** A fixed set of measures (each utility's first zone in the file: a single-baseline measure claimed in Q1 and a dual-baseline,
   fractional-life measure claimed in Q3, on a deterministic 8,760 drawn on another year's calendar) is priced by the engine (`jobs.run_job`) and by the
   independent reference calculation `tests/reference_calc.py` (loops and numpy only, reading the ACC file with pandas, not through the engine's ACC
   store), and every ElecBen, ElecBen_SB and ElecBen_SH must agree within REFERENCE_RTOL. This needs a ReCET checkout (reference_calc is a test module).

The report names the pack (its manifest's version and type), the file's SHA-256 and the engine release, so it can be published with the release notes."""
from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

COMPONENT_TOLERANCE = 1e-6           # $/MWh: components vs total_avoided_cost
TOTALS_RTOL = 1e-9                   # pack totals vs the workbook's
REFERENCE_RTOL = 1e-9                # engine vs reference calculation
KEYS = ["iou", "climate_zone", "cost_test", "year"]
IOU_TO_PA = {"PG&E": "PGE", "SCE": "SCE", "SDG&E": "SDGE"}
SHAPE_YEAR = 2019                    # the reference shape is drawn on 2019's calendar, so the weekday re-alignment is exercised


def _components(names) -> list[str]:
    from .pipeline.real_avoided_cost_v2 import ALL_COMPONENT_COLUMNS
    return [c for c in ALL_COMPONENT_COLUMNS if c in names]


def structure(df: pd.DataFrame, acc: str) -> dict:
    problems = []
    n = df.groupby(KEYS, observed=True)["hour_of_year"].agg(["count", "nunique", "min", "max"])
    bad = n[(n["count"] != 8760) | (n["nunique"] != 8760) | (n["min"] != 1) | (n["max"] != 8760)]
    if len(bad):
        problems.append(f"{len(bad)} block(s) without hours 1-8,760 exactly once, e.g. {bad.index[0]}")
    values = [c for c in df.columns if c not in (*KEYS, "hour_of_year", "datetime", "month", "day", "hour", "price_zone") and pd.api.types.is_numeric_dtype(df[c])]
    missing = {c: int(df[c].isna().sum()) for c in values if df[c].isna().any()}
    if missing:
        problems.append(f"missing values: {missing}")
    comps = _components(df.columns)
    worst = float((df[comps].sum(axis=1) - df["total_avoided_cost"]).abs().max()) if comps else float("nan")
    if not comps or not worst <= COMPONENT_TOLERANCE:
        problems.append(f"components do not add up to total_avoided_cost (worst {worst:.3g} $/MWh)")
    years = sorted(df["year"].unique())
    if years != list(range(int(years[0]), int(years[-1]) + 1)):
        problems.append(f"the years have gaps: {years}")
    tests = sorted(df["cost_test"].unique())
    want = ["TRC"] if str(acc) in ("2021", "2022") else ["SCT Base", "SCT High", "TRC"]
    if tests != want:
        problems.append(f"cost tests {tests}, expected {want}")
    return {"name": "structure", "status": "PASS" if not problems else "FAIL", "problems": problems,
            "blocks": int(len(n)), "pairs": int(df[["iou", "climate_zone"]].drop_duplicates().shape[0]), "years": f"{years[0]}-{years[-1]}",
            "cost_tests": tests, "worst_component_gap_per_mwh": worst}


def control_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Sum of every value column per (utility, zone, cost test, year)."""
    values = [c for c in df.columns if c not in (*KEYS, "hour_of_year", "datetime", "month", "day", "hour", "price_zone") and pd.api.types.is_numeric_dtype(df[c])]
    out = df.groupby(KEYS, observed=True)[values].sum().reset_index()
    for k in ("iou", "climate_zone", "cost_test"):
        out[k] = out[k].astype(str)
    return out.sort_values(KEYS).reset_index(drop=True)


def compare_totals(pack: pd.DataFrame, source: pd.DataFrame) -> dict:
    problems = []
    src = source.copy()
    for k in ("iou", "climate_zone", "cost_test"):
        src[k] = src[k].astype(str)
    m = pack.merge(src, on=KEYS, how="outer", suffixes=("", "__src"), indicator=True)
    only = m[m["_merge"] != "both"]
    if len(only):
        problems.append(f"{len(only)} block(s) in only one of the two, e.g. {tuple(only.iloc[0][KEYS])} ({only.iloc[0]['_merge']})")
    both = m[m["_merge"] == "both"]
    cols = [c for c in pack.columns if c not in KEYS and f"{c}__src" in m]
    worst = {}
    for c in cols:
        a, b = both[c].to_numpy(float), both[f"{c}__src"].to_numpy(float)
        rel = np.abs(a - b) / np.maximum(np.abs(b), 1e-12)
        worst[c] = float(rel.max()) if len(rel) else 0.0
        if (rel > TOTALS_RTOL).any():
            i = int(np.argmax(rel))
            problems.append(f"{c}: {int((rel > TOTALS_RTOL).sum())} total(s) differ, worst {tuple(both.iloc[i][KEYS])}: {a[i]!r} vs {b[i]!r}")
    not_compared = [c for c in pack.columns if c not in KEYS and c not in cols]
    return {"name": "control totals vs the source workbook", "status": "PASS" if not problems else "FAIL", "problems": problems,
            "blocks_compared": int(len(both)), "worst_relative_difference": worst, "columns_not_in_source": not_compared}


# ------------------------------------------------------------------------------------------------------------------------------- reference measures
def _reference_calc():
    f = Path(__file__).resolve().parents[1] / "tests" / "reference_calc.py"
    if not f.exists():
        return None
    spec = importlib.util.spec_from_file_location("recet_reference_calc", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shape() -> np.ndarray:
    i = np.arange(8760)
    w = 1 + 0.5 * np.sin(2 * np.pi * i / 24) + 0.3 * np.sin(2 * np.pi * i / 8760) + 0.2 * np.cos(2 * np.pi * i / 168)
    return w / w.sum()


MEASURES = (   # (id suffix, claim, EUL, RUL, kWh 1st, kWh 2nd, NTG, IR x RR as InstallationRatekWh)
    ("A", "2028Q1", "10", "0", "1000", "0", "0.85", "0.95"),
    ("B", "2028Q3", "12.6", "3.3", "800", "500", "0.7", "1"),
)


def _measure_rows(pairs: list[tuple[str, str]]) -> tuple[list[dict], list[tuple[str, str, str]]]:
    from . import templates
    rows, ids = [], []
    for iou, zone in pairs:
        pa = IOU_TO_PA[iou]
        for sfx, cyq, eul, rul, k1, k2, ntg, ir in MEASURES:
            r = {c: "" for c in templates.MEASURE_COLUMNS}
            r.update({"CEInputID": f"TIE_{pa}_{sfx}", "PrgID": f"{pa}_TIEOUT", "ClaimYearQuarter": cyq, "Sector": "Com", "MeasDescription": "tie-out reference measure",
                      "AC_Climate_Zone": zone.removeprefix("CZ"), "AC_Gas_Target_Sector": "Commercial", "AC_Elec_Impact_Profile": "TIEOUT_SHAPE",
                      "AC_Elec_Target_Sector": "Commercial", "IOU_AC_Territory": pa, "EUL_Yrs": eul, "RUL_Yrs": rul, "NumUnits": "3",
                      "UnitkWh1stBaseline": k1, "UnitkWh2ndBaseline": k2, "NTGRkWh": ntg, "InstallationRatekWh": ir})
            for c in ("UnitkW1stBaseline", "UnitkW2ndBaseline", "UnitTherm1stBaseline", "UnitTherm2ndBaseline", "UnitMeaCost1stBaseline", "UnitMeaCost2ndBaseline",
                      "UnitGasInfraBens", "UnitRefrigCosts", "UnitRefrigBens", "UnitMiscCosts", "UnitMiscBens", "MeasInflation", "UnitEndUserRebate",
                      "UnitIncentiveToOthers", "UnitDirectInstallLab", "UnitDirectInstallMat", "UnitTaxCredits"):
                r[c] = "0"
            for c in ("NTGRkW", "NTGRTherm", "NTGRCost"):
                r[c] = ntg
            for c in ("RealizationRatekW", "RealizationRatekWh", "RealizationRateTherm", "InstallationRatekW", "InstallationRateTherm"):
                r[c] = "1"
            rows.append(r)
            ids.append((r["CEInputID"], iou, zone))
    return rows, ids


def reference_measures(folder: Path, acc: str, df: pd.DataFrame, log=print) -> dict:
    """Part 3 (see the module docstring)."""
    from . import templates, versions
    from .jobs import RunConfig, run_job
    ref = _reference_calc()
    if ref is None:
        return {"name": "reference measures", "status": "NOT RUN", "problems": ["tests/reference_calc.py is not here: run the tie-out from a ReCET checkout"]}
    pairs = []
    for iou in IOU_TO_PA:
        zones = sorted(df.loc[df["iou"] == iou, "climate_zone"].astype(str).unique(), key=lambda z: (len(z), z))
        if zones:
            pairs.append((iou, zones[0]))
    rows, ids = _measure_rows(pairs)
    shape = _shape()
    with tempfile.TemporaryDirectory(prefix="recet_tieout_") as tmp:
        tmp = Path(tmp)
        m = tmp / "Measure.csv"
        m.write_text("\n".join(["|".join(templates.MEASURE_COLUMNS)] + ["|".join(r[c] for c in templates.MEASURE_COLUMNS) for r in rows]) + "\n", encoding="utf-8")
        prof = tmp / "profiles.csv"
        pd.concat([pd.DataFrame({"end_use_name": "TIEOUT_SHAPE", "pa": IOU_TO_PA[iou], "target_sector": "Commercial", "climate_zone": "ALL",
                                 "hour_of_year": np.arange(1, 8761), "impact": shape, "represents_year": SHAPE_YEAR}) for iou, _z in pairs]).to_csv(prof, index=False)
        cfg = RunConfig(measures=str(m), out_dir=str(tmp / "out"), run_name="tieout", acc_version=str(acc), reference_dir=str(folder),
                        profiles_8760_file=str(prof), emissions=False, output_format="parquet")
        res = run_job(cfg, log=lambda s: None)
        frame = res.frame.set_index("CEInputID")
    settings_version = {a: c for c, a in versions.CET_VERSION_TO_ACC.items()}[int(acc)]
    cal = pd.to_datetime(df["datetime"]).min().year if "datetime" in df else 2018
    ref.ACC_YEAR = int(cal)                                   # the reference calculation's weekday alignment follows the ACC's calendar
    tests = (("", "TRC", "qtr"), ("_SB", "SCT Base", "qtr_sct"), ("_SH", "SCT High", "qtr_sct"))
    rows_by_id = {r["CEInputID"]: r for r in rows}
    out, problems = [], []
    for cid, iou, zone in ids:
        st = ref.settings(IOU_TO_PA[iou], settings_version)
        d = ref.derive(rows_by_id[cid])
        for sfx, test, q in tests:
            sub = df[(df["iou"] == iou) & (df["climate_zone"] == zone) & (df["cost_test"] == test)]
            if sub.empty:
                continue
            series = {int(y): g.sort_values("hour_of_year")["total_avoided_cost"].to_numpy(float) / 1000.0 for y, g in sub.groupby("year")}
            want = ref.elec_value(d, shape, SHAPE_YEAR, series, st[q], 2028, d["AC_Net_kWh_1st_Baseline"], d["AC_Net_kWh_2nd_Baseline"])
            got = float(frame.loc[cid, f"ElecBen{sfx}"])
            rel = abs(got - want) / max(abs(want), 1e-12)
            out.append({"measure": cid, "utility": iou, "zone": zone, "test": test, "engine": got, "reference": want, "relative_difference": rel})
            if not rel <= REFERENCE_RTOL:
                problems.append(f"{cid} ElecBen{sfx}: engine {got!r}, reference {want!r} (relative difference {rel:.3g})")
    return {"name": "reference measures (engine vs tests/reference_calc.py)", "status": "PASS" if out and not problems else "FAIL", "problems": problems or
            ([] if out else ["no reference value was computed"]), "values": out}


# ------------------------------------------------------------------------------------------------------------------------------- the report
def run(folder: str | Path, acc: str, source_totals: str | Path | None = None, out_dir: str | Path | None = None, log=print) -> dict:
    """Write the tie-out report of ACC `acc` in reference folder `folder`; returns it. Its `status` is PASS only when every part passed."""
    from . import __version__, datapack, paths, versions
    folder = Path(folder)
    out_dir = Path(out_dir or folder / "tieout")
    out_dir.mkdir(parents=True, exist_ok=True)
    key = versions.BUILTIN[str(acc)].electric_key
    f = folder / paths.REF_FILES[key].rel
    if not f.exists():
        raise FileNotFoundError(f"{f} not found")
    log(f"reading {f}")
    df = pd.read_parquet(f)
    info = datapack.file_info(f, folder) or {}
    parts = [structure(df, acc)]
    log(f"structure: {parts[0]['status']}")
    totals = control_totals(df)
    totals.to_csv(out_dir / f"control_totals_acc{acc}.csv", index=False)
    if source_totals:
        parts.append(compare_totals(totals, pd.read_csv(source_totals)))
    else:
        parts.append({"name": "control totals vs the source workbook", "status": "NOT RUN",
                      "problems": [f"no --source-totals given: the pack's totals are in control_totals_acc{acc}.csv for the workbook side to compare"]})
    log(f"control totals: {parts[1]['status']}")
    parts.append(reference_measures(folder, str(acc), df, log))
    log(f"reference measures: {parts[2]['status']}")
    report = {"acc": str(acc), "file": paths.REF_FILES[key].rel, "sha256": info.get("sha256") or datapack.sha256_file(f), "bytes": f.stat().st_size,
              "pack": info.get("pack"), "draft": bool(info.get("draft")), "engine_release": __version__,
              "created": datetime.now().isoformat(timespec="seconds"), "source_totals": str(source_totals) if source_totals else None,
              "tolerances": {"components_per_mwh": COMPONENT_TOLERANCE, "totals_relative": TOTALS_RTOL, "reference_relative": REFERENCE_RTOL},
              "parts": parts, "status": "PASS" if all(p["status"] == "PASS" for p in parts) else "FAIL"}
    (out_dir / f"tieout_acc{acc}.json").write_text(json.dumps(report, indent=2, default=float) + "\n", encoding="utf-8")
    (out_dir / f"tieout_acc{acc}.md").write_text(markdown(report), encoding="utf-8")
    log(f"tie-out {report['status']}: {out_dir / f'tieout_acc{acc}.md'}")
    return report


def markdown(r: dict) -> str:
    pack = r.get("pack") or {}
    lines = [f"# Tie-out: the {r['acc']} ACC" + (" (DRAFT)" if r["draft"] else ""), "",
             f"**{r['status']}**. `{r['file']}` (SHA-256 `{r['sha256']}`, {r['bytes']:,} bytes), pack {pack.get('type', '?')} version "
             f"{pack.get('version', '?')}, priced by ReCET {r['engine_release']}, {r['created']}.", "",
             f"Tolerances: components {r['tolerances']['components_per_mwh']:g} $/MWh, control totals {r['tolerances']['totals_relative']:g} relative, "
             f"reference measures {r['tolerances']['reference_relative']:g} relative.", ""]
    for p in r["parts"]:
        lines += [f"## {p['name']}: {p['status']}", ""]
        lines += [f"- {x}" for x in p.get("problems", [])] or ["- no problems"]
        if p.get("values"):
            lines += ["", "| Measure | Utility | Zone | Test | Engine | Reference | Relative difference |", "|---|---|---|---|---|---|---|"]
            lines += [f"| {v['measure']} | {v['utility']} | {v['zone']} | {v['test']} | {v['engine']:,.6f} | {v['reference']:,.6f} | {v['relative_difference']:.2e} |"
                      for v in p["values"]]
        lines.append("")
    return "\n".join(lines)

