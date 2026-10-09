"""Program metadata: the CEDARS Programs list (one row per PrgID: PA, name, status, segment, sectors, implementer, category, years and the program flags).

The built-in table is CEDARS' Programs export of 2026-10-03 (`data/programs/`, 1,416 programs; the `ProgramManager` column, which holds staff names, is left out).
A supplied table (`programs_file`, the `Programs` workbook sheet) uses the same layout: a supplied PrgID replaces the built-in row, a new one is added, and any
extra column is carried too.

Statewide programs are listed once per funding IOU, with the IOU prepended (`PGE_SW_HVAC_Up`, `SCE_SW_HVAC_Up`, ...), plus a thin `SW_HVAC_Up` row with PA = SW
(most of its fields are blank). Each measure is matched on its PrgID as priced:
  * statewide allocation on: an allocated share's PrgID is `<IOU>_SW_...`, so it gets that IOU's own row;
  * statewide allocation off: a statewide program keeps its `SW_...` PrgID and gets the `SW_...` row; a field that row leaves blank is filled from the
    `<IOU>_SW_...` rows when every one of them that exists gives the same non-blank value (they often differ, e.g. ProgramName and ProgramImplementer).
`Prg_MatchedOn` records which. The metadata is attached to the measure-level table as `Prg_<column>` and does not change any priced value; the only option
that uses it is `apply_exclude_from_ce` (outputs.summarize).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import paths

BUILTIN_CSV = paths.package_data("programs") / "cedars_programs_20261003.csv"
REQUIRED = ["PA", "PrgID"]
COLUMNS = ["PA", "PrgID", "ProgramName", "ProgramStatus", "ProgramSegment", "StatewideProgram", "PrimarySector", "Sector", "BPSector", "ProgramImplementer",
           "ProgramCategory", "ImplementationContractor", "StartYear", "EndYear", "Resource_Flag", "NonResource_Flag", "Deemed_Flag", "Custom_Flag", "Upstream_Flag",
           "Midstream_Flag", "Downstream_Flag", "DirectInstall", "Audit_Flag", "Financing", "ParentProgram", "Exclude_From_Budget", "Exclude_From_CE",
           "Exclude_From_Statewide_Flag"]
PREFIX = "Prg_"
IOUS = ("PGE", "SCE", "SDGE", "SCG")
TRUE = {"true", "1", "yes", "y", "t"}

_USER: pd.DataFrame | None = None


class ProgramsError(ValueError):
    pass


def set_user_programs(table: pd.DataFrame | None) -> None:
    global _USER
    _USER = table


def _read(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise ProgramsError(f"file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        df = pd.read_excel(p, dtype=str, keep_default_na=False)
    elif suffix == ".csv":
        df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    elif suffix == ".txt":
        df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig", sep=None, engine="python")
    elif suffix == ".parquet":
        df = pd.read_parquet(p).astype(str)
    else:
        raise ProgramsError(f"unsupported file type {suffix!r} for {p.name} (use .csv or .xlsx)")
    df.columns = [str(c).strip() for c in df.columns]
    lower = {c.lower(): c for c in df.columns}
    df = df.rename(columns={lower[c.lower()]: c for c in COLUMNS if c.lower() in lower and lower[c.lower()] != c})
    return df.fillna("").astype(str).apply(lambda s: s.str.strip())


def load_programs(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """A supplied program table: PA and PrgID required (PrgID unique and non-blank), every other column optional. Returns (table, notes)."""
    df = _read(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ProgramsError(f"program metadata: missing column(s) {missing}; found {list(df.columns)}")
    df = df[df["PrgID"] != ""].copy()
    if df.empty:
        raise ProgramsError("program metadata: no rows")
    dup = df["PrgID"][df["PrgID"].duplicated()]
    if len(dup):
        raise ProgramsError(f"program metadata: PrgID repeated: {sorted(dup.unique())[:5]}")
    notes = []
    absent = [c for c in COLUMNS if c not in df.columns]
    if absent:
        notes.append(f"columns not in the file (left blank unless the built-in row has them): {absent}")
    extra = [c for c in df.columns if c not in COLUMNS]
    if extra:
        notes.append(f"extra columns carried as {PREFIX}<column>: {extra}")
    return df, notes


def builtin() -> pd.DataFrame:
    return _read(BUILTIN_CSV)


def table() -> pd.DataFrame:
    """The built-in table with any supplied rows replacing (same PrgID) or extending it. A replaced row takes the supplied row as a whole."""
    base = builtin()
    if _USER is None:
        return base
    user = _USER.copy()
    return pd.concat([base[~base["PrgID"].isin(user["PrgID"])], user], ignore_index=True).fillna("")


def lookup(prg_ids, programs: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per entry of `prg_ids` (same order): `Prg_MatchedOn` and `Prg_<column>` for every column of the table (blank when not found)."""
    t = (table() if programs is None else programs).set_index("PrgID", drop=False)
    cols = [c for c in t.columns]
    rows, matched = {}, {}
    ids = pd.Series(prg_ids, dtype=object).fillna("").astype(str).str.strip()
    for pid in ids.unique():
        rec = t.loc[pid].to_dict() if pid in t.index else None
        copies = t[t["PrgID"].isin([f"{i}_{pid}" for i in IOUS])] if pid.startswith("SW_") else t.iloc[0:0]
        filled = False
        if len(copies):
            rec = rec or {c: "" for c in cols}
            for c in cols:
                if c in ("PA", "PrgID") or rec.get(c, ""):
                    continue
                vals = set(copies[c])
                if len(vals) == 1 and "" not in vals:
                    rec[c] = vals.pop()
                    filled = True
        if rec is None:
            matched[pid], rows[pid] = "(not found)", {c: "" for c in cols}
            continue
        if pid not in t.index:
            rec["PrgID"], rec["PA"] = pid, "SW"
        matched[pid] = f"{pid} + agreeing IOU copies" if filled and pid in t.index else ("agreeing IOU copies" if filled else pid)
        rows[pid] = rec
    out = pd.DataFrame([rows[p] for p in ids], columns=[c for c in cols if c != "PrgID"]).add_prefix(PREFIX)     # PrgID: the measure's own
    out.insert(0, f"{PREFIX}MatchedOn", [matched[p] for p in ids])
    return out.reset_index(drop=True)


def is_true(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.lower().isin(TRUE)
