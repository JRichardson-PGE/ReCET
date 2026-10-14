"""User-supplied inputs that REPLACE (when the key matches) or EXTEND (when it does not) what the package ships.

Six kinds, each with a template (`recet.templates`, `recet template ...`, or the GUI's "Template" buttons):

1. 8,760-hour electric impact profiles   -> `load_user_profiles`   (long CSV/Parquet: end_use_name, pa, target_sector, climate_zone, hour_of_year, impact)
2. gas monthly savings profiles           -> `load_user_gas_profiles` (CSV/Parquet: Profile, Month, Impact -- the same three columns as the built-in library)
3. electric ACC (hourly avoided costs)    -> `check_electric_acc` / `prepare_electric_acc`
4. gas ACC (monthly components)           -> `check_gas_acc` / `prepare_gas_acc`
5. run parameters (CET's E3Settings)      -> `load_run_parameters`  (one row per PA: discount rates, base year, gas CO2; a supplied PA replaces the built-in row)
6. SW allocation factors                  -> `load_sw_factors`      (the built-in table's layout; a supplied (Year, ProgramID) replaces the built-in row)

Matching rules ("if a profile matches an existing defined version, the separate input replaces it"):

* 8,760 profiles match a measure on (E3MeaElecEndUseShape = `end_use_name`, IOU_AC_Territory = `pa`, E3TargetSector = `target_sector`), the same three keys
  the CEDARS library is indexed on. Blank / `*` / `ALL` in `pa` or `target_sector` means "any". Names and sectors compare case-insensitively.
  The most specific matching entry wins (PA and sector both given > one of them > neither). A matching user profile is used INSTEAD of the library's
  profile -- and instead of a TOU (H-factor) profile of the same name. `climate_zone` is the measure's E3ClimateZone ("1", "3A", "13" ... "CZ13" and "01" are
  accepted); a zone the file does not have falls back to its `System` (or `ALL`) rows, and if there are none the library's profile is used for that zone
  (reported per measure in `ProfileSource`).
* Gas profiles match on `Profile` = the measure's E3GasSavProfile. A name in the built-in library is replaced, a new name is added.
* ACC files are chosen per run (see `recet.versions`); a file supplied for an ACC version label the package already has replaces that version's file.

Everything is validated when loaded and raises `OverrideError` with the row/hour that is wrong, never a silent fix -- except the two documented
normalisations (impacts that do not sum to 1 are rescaled and the run says so).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

HOURS = 8760
SUM_TOLERANCE = 1e-3            # an impact shape whose sum is within 0.1% of 1 is used exactly as given
DEFAULT_REPRESENTS_YEAR = 2009  # same default as the CEDARS library for a profile with no year stated


class OverrideError(ValueError):
    """A user-supplied input file is malformed; the message says where."""


def _read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise OverrideError(f"file not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(p)
    if suffix in (".xlsx", ".xlsm"):
        return pd.read_excel(p)
    if suffix in (".csv", ".txt", ".tsv"):
        return pd.read_csv(p, sep=None, engine="python") if suffix != ".csv" else pd.read_csv(p, low_memory=False)
    raise OverrideError(f"unsupported file type {suffix!r} for {p.name} (use .csv, .parquet or .xlsx)")


def _require_columns(df: pd.DataFrame, required: list[str], what: str) -> pd.DataFrame:
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    lower = {c.lower(): c for c in df.columns}
    ren = {lower[r.lower()]: r for r in required if r.lower() in lower and lower[r.lower()] != r}
    df = df.rename(columns=ren)
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise OverrideError(f"{what}: missing column(s) {missing}; found {list(df.columns)}")
    return df


# ======================================================================================================================================
# 1. 8,760-hour electric impact profiles
# ======================================================================================================================================
PROFILE_COLUMNS = ["end_use_name", "pa", "target_sector", "climate_zone", "hour_of_year", "impact"]

_PA_ALIASES = {"PGE": "PGE", "PG&E": "PGE", "PGAE": "PGE", "SCE": "SCE", "SDGE": "SDGE", "SDG&E": "SDGE", "SCG": "SCG", "SOCALGAS": "SCG", "SOCAL GAS": "SCG"}


def norm_pa(value) -> str | None:
    """Canonical PA/IOU code (PGE/SCE/SDGE/SCG), or None for 'any'."""
    s = "" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value).strip().upper()
    if s in ("", "*", "ALL", "ANY"):
        return None
    if s not in _PA_ALIASES:
        raise OverrideError(f"unknown pa {value!r}; use PGE, SCE, SDGE, SCG or blank/* for any")
    return _PA_ALIASES[s]


def norm_sector(value) -> str | None:
    s = "" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value).strip().upper()
    return None if s in ("", "*", "ALL", "ANY") else s


def norm_zone(value) -> str:
    """'CZ13' / '13' / ' 13 ' -> '13'; '01' -> '1'; '3a' -> '3A'; blank/*/ALL -> 'ALL'; System -> 'SYSTEM'."""
    s = "" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value).strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s in ("", "*", "ALL", "ANY"):
        return "ALL"
    s = re.sub(r"^CZ\s*", "", s)
    if s.isdigit():
        s = str(int(s))
    elif re.fullmatch(r"0\d[A-Z]", s):
        s = s[1:]
    return s


@dataclass
class _Entry:
    end_use: str                                   # as written in the file (display)
    pa: str | None
    sector: str | None
    represents_year: int
    zones: dict[str, np.ndarray] = field(default_factory=dict)   # normalised zone -> length-8760 shape (sums to ~1)

    def specificity(self) -> int:
        return (self.pa is not None) + (self.sector is not None)


class UserProfiles:
    """The loaded user 8,760 profiles. `lookup()` is what pipeline/real_impact_profiles.resolve_shape calls."""

    def __init__(self, entries: list[_Entry], notes: list[str]):
        self.entries = entries
        self.notes = notes          # normalisation messages worth printing (rescaled shapes, ...)
        self._by_end_use: dict[str, list[_Entry]] = {}
        for e in entries:
            self._by_end_use.setdefault(e.end_use.strip().lower(), []).append(e)

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, shape_key: str, iou: str, sector: str) -> _Entry | None:
        cands = self._by_end_use.get(str(shape_key).strip().lower(), [])
        pa = norm_pa(iou) if str(iou).strip() else None
        sec = norm_sector(sector)
        best = None
        for e in cands:
            if (e.pa is None or e.pa == pa) and (e.sector is None or e.sector == sec):
                if best is None or e.specificity() > best.specificity():
                    best = e
        return best

    def lookup(self, shape_key: str, iou: str, sector: str, zone: str):
        """(shape, represents_year) | "zone_missing" (an entry matches but has no shape for this zone) | None (no entry matches)."""
        e = self.match(shape_key, iou, sector)
        if e is None:
            return None
        z = norm_zone(zone)
        for key in (z, "SYSTEM", "ALL"):
            if key in e.zones:
                return e.zones[key], e.represents_year
        return "zone_missing"

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([{"end_use_name": e.end_use, "pa": e.pa or "any", "target_sector": e.sector or "any", "represents_year": e.represents_year,
                              "zones": ", ".join(sorted(e.zones))} for e in self.entries])


def load_user_profiles(path: str | Path, normalize: bool = True) -> UserProfiles:
    """Read and validate a long-format 8,760 profile file (see module docstring and templates). `normalize`: rescale a zone whose impacts do not sum to 1."""
    df = _require_columns(_read_table(path), PROFILE_COLUMNS, "8,760 profile file")
    has_year = "represents_year" in {c.lower() for c in df.columns}
    if has_year:
        df = df.rename(columns={c: "represents_year" for c in df.columns if c.lower() == "represents_year"})
    df = df.dropna(how="all")
    if df.empty:
        raise OverrideError("8,760 profile file has no rows")
    for c in ("end_use_name", "hour_of_year", "impact"):
        if df[c].isna().any():
            raise OverrideError(f"8,760 profile file: blank {c} in row(s) {list((df.index[df[c].isna()][:5] + 2).tolist())} (spreadsheet row numbers)")
    df = df.copy()
    df["end_use_name"] = df["end_use_name"].astype(str).str.strip()
    try:
        df["pa_n"] = df["pa"].map(norm_pa)
    except OverrideError:
        raise
    df["sector_n"] = df["target_sector"].map(norm_sector)
    df["zone_n"] = df["climate_zone"].map(norm_zone)
    df["hour_of_year"] = pd.to_numeric(df["hour_of_year"], errors="raise").astype(int)
    df["impact"] = pd.to_numeric(df["impact"], errors="raise").astype(float)
    if not np.isfinite(df["impact"].to_numpy()).all():
        raise OverrideError("8,760 profile file: impact contains NaN/inf")
    if has_year:
        yr = pd.to_numeric(df["represents_year"], errors="coerce")
        df["year_n"] = yr.fillna(DEFAULT_REPRESENTS_YEAR).astype(int)
    else:
        df["year_n"] = DEFAULT_REPRESENTS_YEAR

    entries: list[_Entry] = []
    notes: list[str] = []
    seen_keys: set[tuple] = set()
    for (end_use, pa, sector), g in df.groupby(["end_use_name", "pa_n", "sector_n"], dropna=False, sort=False):
        pa = None if pd.isna(pa) else pa
        sector = None if pd.isna(sector) else sector
        key = (end_use.lower(), pa, sector)
        if key in seen_keys:
            raise OverrideError(f"8,760 profile file: profile {end_use!r} (pa={pa or 'any'}, sector={sector or 'any'}) appears in two separate blocks; keep its rows together")
        seen_keys.add(key)
        years = sorted(g["year_n"].unique())
        if len(years) != 1:
            raise OverrideError(f"profile {end_use!r}: represents_year must be the same on every row (found {years})")
        entry = _Entry(end_use=end_use, pa=pa, sector=sector, represents_year=int(years[0]))
        for zone, z in g.groupby("zone_n", sort=False):
            hours = z["hour_of_year"].to_numpy()
            if len(z) != HOURS or set(hours.tolist()) != set(range(1, HOURS + 1)):
                missing = sorted(set(range(1, HOURS + 1)) - set(hours.tolist()))
                dupes = z["hour_of_year"][z["hour_of_year"].duplicated()].unique().tolist()
                raise OverrideError(
                    f"profile {end_use!r}, zone {zone}: needs exactly one row for each hour_of_year 1..8760 (has {len(z)} rows; "
                    f"missing {missing[:6]}{'...' if len(missing) > 6 else ''}; duplicated {dupes[:6]}{'...' if len(dupes) > 6 else ''})")
            shape = z.sort_values("hour_of_year")["impact"].to_numpy(dtype=np.float64)
            total = float(shape.sum())
            if abs(total - 1.0) > SUM_TOLERANCE:
                if not normalize:
                    raise OverrideError(f"profile {end_use!r}, zone {zone}: impacts sum to {total:.6f}, not 1 (normalization is off)")
                if abs(total) < 1e-9:
                    raise OverrideError(f"profile {end_use!r}, zone {zone}: impacts sum to ~0, so they cannot be scaled into a share-of-annual-savings shape")
                shape = shape / total
                notes.append(f"8,760 profile {end_use!r} zone {zone}: impacts summed to {total:.6g}; rescaled to sum to 1")
            entry.zones[zone] = shape
        entries.append(entry)
    return UserProfiles(entries, notes)


# ======================================================================================================================================
# 2. gas monthly profiles
# ======================================================================================================================================
GAS_PROFILE_COLUMNS = ["Profile", "Month", "Impact"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def load_user_gas_profiles(path: str | Path, normalize: bool = True) -> tuple[dict[str, np.ndarray], list[str]]:
    """{profile name: normalised length-12 array (Jan..Dec)} and a list of notes. Month may be 1-12 or Jan..Dec (or January..December)."""
    df = _require_columns(_read_table(path), GAS_PROFILE_COLUMNS, "gas profile file").dropna(how="all")
    if df.empty:
        raise OverrideError("gas profile file has no rows")
    df = df.copy()
    df["Profile"] = df["Profile"].astype(str).str.strip()

    def month_number(v) -> int:
        s = str(v).strip().lower()
        if s.replace(".0", "").isdigit():
            n = int(float(s))
        elif s[:3] in _MONTHS:
            n = _MONTHS.index(s[:3]) + 1
        else:
            raise OverrideError(f"gas profile file: unrecognised Month {v!r} (use 1-12 or Jan..Dec)")
        if not 1 <= n <= 12:
            raise OverrideError(f"gas profile file: Month {v!r} is not 1-12")
        return n

    df["m"] = df["Month"].map(month_number)
    df["Impact"] = pd.to_numeric(df["Impact"], errors="raise").astype(float)
    if not np.isfinite(df["Impact"].to_numpy()).all():
        raise OverrideError("gas profile file: Impact contains NaN/inf")
    out: dict[str, np.ndarray] = {}
    notes: list[str] = []
    from .pipeline import gas_savings_profiles as lib
    library_names = {n.lower(): n for n in lib._table()["Profile"].unique()}
    for name, g in df.groupby("Profile", sort=False):
        if sorted(g["m"].tolist()) != list(range(1, 13)):
            raise OverrideError(f"gas profile {name!r}: needs exactly one row for each month 1-12 (has months {sorted(g['m'].tolist())})")
        canonical = library_names.get(name.lower())
        if canonical is not None and canonical != name:
            notes.append(f"gas profile {name!r} matches the library profile {canonical!r} apart from capitalisation; treated as {canonical!r}")
            name = canonical
        vec = g.sort_values("m")["Impact"].to_numpy(dtype=np.float64)
        total = float(vec.sum())
        if abs(total - 1.0) > SUM_TOLERANCE:
            if not normalize:
                raise OverrideError(f"gas profile {name!r}: monthly impacts sum to {total:.6f}, not 1 (normalization is off)")
            if abs(total) < 1e-9:
                raise OverrideError(f"gas profile {name!r}: monthly impacts sum to ~0")
            vec = vec / total
            notes.append(f"gas profile {name!r}: impacts summed to {total:.6g}; rescaled to sum to 1")
        if name in out:
            raise OverrideError(f"gas profile {name!r} appears twice")
        out[name] = vec
    return out, notes


# ======================================================================================================================================
# 3. electric ACC
# ======================================================================================================================================
ELECTRIC_ACC_REQUIRED = ["iou", "climate_zone", "cost_test", "year", "hour_of_year", "total_avoided_cost"]
ELECTRIC_ACC_OPTIONAL = {
    "transmission": "needed to price TOU (H-factor) profiles' T&D term",
    "distribution": "needed to price TOU (H-factor) profiles' T&D term",
    "emission_rate_tonnes_per_kwh": "needed for the electric CO2 columns",
}
ELECTRIC_COMPONENTS = ("energy", "generation_capacity", "avoided_as_procurement", "losses", "methane_leakage", "ghg_cap_and_trade", "ghg_cap_and_invest",
                       "ghg_adder", "ghg_portfolio_rebalancing", "air_quality_adder", "transmission", "distribution")
_ELECTRIC_IOUS = {"PG&E", "SCE", "SDG&E"}
_IOU_ALIASES = {"PGE": "PG&E", "PG&E": "PG&E", "SCE": "SCE", "SDGE": "SDG&E", "SDG&E": "SDG&E"}
_COST_TEST_ALIASES = {"TRC": "TRC", "SCT BASE": "SCT Base", "SCT HIGH": "SCT High"}


@dataclass
class AccCheck:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    def raise_if_bad(self, what: str) -> None:
        if not self.ok:
            raise OverrideError(f"{what} is not usable:\n  - " + "\n  - ".join(self.errors))

    def text(self) -> str:
        lines = [f"info: {self.info}"] if self.info else []
        lines += [f"ERROR: {e}" for e in self.errors] + [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def _electric_frame(path: str | Path, columns: list[str] | None = None) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        return pd.read_parquet(p, columns=columns)
    return _read_table(p) if columns is None else _read_table(p)[columns]


def check_electric_acc(path: str | Path, tolerance: float = 1e-6) -> AccCheck:
    """Validate an electric ACC file (parquet, or CSV/xlsx for small ones) against the layout in the module docstring / template."""
    chk = AccCheck(ok=True)
    p = Path(path)
    if not p.exists():
        return AccCheck(ok=False, errors=[f"file not found: {p}"])
    try:
        if p.suffix.lower() == ".parquet":
            import pyarrow.parquet as pq
            names = list(pq.ParquetFile(str(p)).schema_arrow.names)
        else:
            names = list(_read_table(p).columns)
    except Exception as exc:                                    # unreadable file
        return AccCheck(ok=False, errors=[f"could not read {p.name}: {exc}"])
    missing = [c for c in ELECTRIC_ACC_REQUIRED if c not in names]
    if missing:
        return AccCheck(ok=False, errors=[f"missing required column(s) {missing}; found {names}"])
    for col, why in ELECTRIC_ACC_OPTIONAL.items():
        if col not in names:
            chk.warnings.append(f"no `{col}` column: {why} will not work with this file")
    comps = [c for c in ELECTRIC_COMPONENTS if c in names]
    if not [c for c in comps if c not in ("transmission", "distribution")]:
        chk.warnings.append('no component columns beyond transmission/distribution: "calculate ACC components separately" will not work with this file')

    keys = _electric_frame(p, ["iou", "climate_zone", "cost_test", "year", "hour_of_year"])
    ious = set(map(str, keys["iou"].unique()))
    bad = ious - _ELECTRIC_IOUS
    if bad:
        chk.errors.append(f"iou values must be PG&E, SCE or SDG&E; found {sorted(bad)}")
    tests = set(map(str, keys["cost_test"].unique()))
    if "TRC" not in tests:
        chk.errors.append(f"cost_test must include 'TRC' (found {sorted(tests)})")
    for t in ("SCT Base", "SCT High"):
        if t not in tests:
            chk.warnings.append(f"no cost_test '{t}' rows: the societal cost tests (SCB/SCH) cannot be run with this file")
    counts = keys.groupby(["iou", "climate_zone", "cost_test", "year"], observed=True).size()
    wrong = counts[counts != HOURS]
    if len(wrong):
        chk.errors.append(f"{len(wrong)} (iou, climate_zone, cost_test, year) block(s) do not have exactly 8,760 rows, e.g. {wrong.head(3).to_dict()}")
    hmin, hmax = int(keys["hour_of_year"].min()), int(keys["hour_of_year"].max())
    if (hmin, hmax) != (1, HOURS):
        chk.errors.append(f"hour_of_year must run 1..8760 (found {hmin}..{hmax})")
    years = sorted(int(y) for y in keys["year"].unique())
    if years != list(range(years[0], years[-1] + 1)):
        chk.errors.append(f"years must be consecutive (found {years})")
    zones_by_iou = {str(i): sorted(map(str, g["climate_zone"].unique())) for i, g in keys.groupby("iou", observed=True)}
    for iou in _ELECTRIC_IOUS - ious:
        chk.warnings.append(f"no rows for {iou}: measures in that IOU's territory cannot be priced")
    chk.info = {"years": f"{years[0]}-{years[-1]}", "ious": sorted(ious), "cost_tests": sorted(tests), "zones": zones_by_iou, "components": comps}

    if not chk.errors and p.suffix.lower() == ".parquet":
        summable = [c for c in comps]
        if len(summable) >= 2:                                    # total == sum of components (checked on TRC, first year)
            df = pd.read_parquet(p, columns=["cost_test", "year", "total_avoided_cost"] + summable, filters=[("year", "==", years[0])])
            df = df[df["cost_test"] == "TRC"]
            resid = float((df["total_avoided_cost"] - df[summable].sum(axis=1)).abs().max()) if len(df) else 0.0
            chk.info["max |total - sum(components)| ($/MWh, TRC first year)"] = resid
            if resid > tolerance * max(1.0, float(df["total_avoided_cost"].abs().max())):
                chk.warnings.append(f"total_avoided_cost differs from the sum of its component columns by up to {resid:.4g} $/MWh; component results will not add to the total")
    chk.ok = not chk.errors
    return chk


def prepare_electric_acc(path: str | Path, work_dir: str | Path) -> Path:
    """A parquet path the pipeline can read: the file itself if it is parquet, else a validated conversion written into work_dir. Labels are canonicalised
    (PGE -> PG&E, 'sct base' -> 'SCT Base')."""
    p = Path(path)
    chk = check_electric_acc(p)
    chk.raise_if_bad(f"electric ACC file {p.name}")
    if p.suffix.lower() == ".parquet":
        return p
    df = _read_table(p)
    df["iou"] = df["iou"].astype(str).str.strip().str.upper().map(_IOU_ALIASES)
    df["cost_test"] = df["cost_test"].astype(str).str.strip().str.upper().map(_COST_TEST_ALIASES)
    df["climate_zone"] = df["climate_zone"].map(lambda v: "CZ" + norm_zone(v) if not str(v).upper().startswith("CZ") else str(v).upper())
    out = Path(work_dir) / f"{p.stem}_acc_electric.parquet"
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out


# ======================================================================================================================================
# 4. gas ACC
# ======================================================================================================================================
GAS_ACC_MONTHS = _MONTHS
GAS_ACC_COLUMNS = (["utility", "class", "cost_test", "scc", "year"] + [f"market_{m}" for m in _MONTHS] + [f"td_{m}" for m in _MONTHS]
                   + ["nox", "co2", "upstream_methane", "behind_meter_methane"])
GAS_ACC_OPTIONAL = ("air_quality_adder",)   # SCT-only component; the 2021/2022 Gas ACCs have none (priced as zero when missing)
_GAS_UTILITY_ALIASES = {"PG&E": "PG&E", "PGE": "PG&E", "SOCAL GAS": "Socal Gas", "SOCALGAS": "Socal Gas", "SCG": "Socal Gas", "SDG&E": "SDG&E", "SDGE": "SDG&E"}
_GAS_CLASSES = {"Residential", "Commercial", "Total Core"}


def check_gas_acc(path: str | Path) -> AccCheck:
    p = Path(path)
    if not p.exists():
        return AccCheck(ok=False, errors=[f"file not found: {p}"])
    try:
        df = _read_table(p)
    except Exception as exc:
        return AccCheck(ok=False, errors=[f"could not read {p.name}: {exc}"])
    chk = AccCheck(ok=True)
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    missing = [c for c in GAS_ACC_COLUMNS if c not in df.columns]
    if missing:
        return AccCheck(ok=False, errors=[f"missing column(s) {missing}"])
    if "air_quality_adder" not in df.columns:
        chk.warnings.append("no air_quality_adder column: priced as zero (as in the 2021 and 2022 Gas ACCs)")
    num = [c for c in GAS_ACC_COLUMNS + [c for c in GAS_ACC_OPTIONAL if c in df.columns] if c not in ("utility", "class", "cost_test", "scc")]
    if df[num].isna().any().any():
        chk.errors.append("blank/NaN in a numeric column (year, monthly market/td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder)")
    util = df["utility"].astype(str).str.strip().str.upper().map(_GAS_UTILITY_ALIASES)
    if util.isna().any():
        chk.errors.append(f"utility must be PG&E, Socal Gas (or SoCalGas) or SDG&E; found {sorted(df['utility'][util.isna()].astype(str).unique())}")
    if not set(df["class"].astype(str).str.strip()) <= _GAS_CLASSES:
        chk.errors.append(f"class must be Residential, Commercial or Total Core; found {sorted(set(df['class'].astype(str)) - _GAS_CLASSES)}")
    tests = set(df["cost_test"].astype(str).str.strip().str.upper())
    if "TRC" not in tests:
        chk.errors.append("cost_test must include TRC")
    sct = df[df["cost_test"].astype(str).str.strip().str.upper() == "SCT"]
    if len(sct) == 0:
        chk.warnings.append("no SCT rows: the societal cost tests (SCB/SCH) cannot price gas with this file")
    elif not {"BASE", "HIGH"} <= set(sct["scc"].astype(str).str.strip().str.upper()):
        chk.warnings.append("SCT rows need scc = Base and High")
    years = sorted(int(y) for y in df["year"].dropna().unique())
    if years and years != list(range(years[0], years[-1] + 1)):
        chk.errors.append(f"years must be consecutive (found {years[0]}..{years[-1]} with gaps)")
    combos = df.assign(utility_n=util).groupby(["utility_n", "class", "cost_test", "scc"], dropna=False)["year"].nunique()
    short = combos[combos != len(years)]
    if len(short):
        chk.warnings.append(f"{len(short)} (utility, class, cost_test, scc) combination(s) do not cover every year, e.g. {list(short.index[:3])}")
    for u in ("PG&E", "Socal Gas", "SDG&E"):
        if u not in set(util.dropna()):
            chk.warnings.append(f"no rows for {u}: gas measures priced on {u} cannot be priced (SCE-territory gas measures use Socal Gas)")
    chk.info = {"years": f"{years[0]}-{years[-1]}" if years else "none", "rows": len(df)}
    chk.ok = not chk.errors
    return chk


def prepare_gas_acc(path: str | Path, work_dir: str | Path) -> Path:
    """A parquet the pipeline can read, with canonical labels (utility 'Socal Gas', cost_test 'TRC'/'SCT', scc '' -> NaN like the packaged tables)."""
    p = Path(path)
    check_gas_acc(p).raise_if_bad(f"gas ACC file {p.name}")
    df = _read_table(p).rename(columns=lambda c: str(c).strip())
    df["utility"] = df["utility"].astype(str).str.strip().str.upper().map(_GAS_UTILITY_ALIASES)
    df["cost_test"] = df["cost_test"].astype(str).str.strip().str.upper()
    df["scc"] = df["scc"].astype(str).str.strip().str.capitalize().where(df["cost_test"] == "SCT", np.nan)
    df["class"] = df["class"].astype(str).str.strip()
    df["year"] = df["year"].astype(int)
    for c in GAS_ACC_COLUMNS + [c for c in GAS_ACC_OPTIONAL if c in df.columns]:
        if c not in ("utility", "class", "cost_test", "scc"):
            df[c] = df[c].astype(float) if c != "year" else df[c]
    out = Path(work_dir) / f"{p.stem}_acc_gas.parquet"
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    df[GAS_ACC_COLUMNS + [c for c in GAS_ACC_OPTIONAL if c in df.columns]].to_parquet(out, index=False)
    return out


# ======================================================================================================================================
# 5. Run parameters (CET's E3Settings table: discount rates, base year, gas CO2)
# ======================================================================================================================================
RUN_PARAMETER_COLUMNS = ["PA", "DiscountRateAnnual", "DiscountRateAnnual_SCT", "CO2Gas"]
RUN_PARAMETER_OPTIONAL = {"DiscountRateQtr": "DiscountRateAnnual / 4", "DiscountRateQtr_SCT": "DiscountRateAnnual_SCT / 4",
                          "BaseYear": "the built-in value for the run's E3 version"}
RUN_PARAMETER_OUT = ["DiscountRateAnnual", "DiscountRateQtr", "DiscountRateAnnual_SCT", "DiscountRateQtr_SCT", "BaseYear", "CO2Gas"]


def _match_case(df: pd.DataFrame, names) -> pd.DataFrame:
    lower = {str(c).lower(): c for c in df.columns}
    return df.rename(columns={lower[n.lower()]: n for n in names if n not in df.columns and n.lower() in lower})


def load_run_parameters(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """One row per PA (PGE, SCE, SDGE, SCG ...). Returns (table indexed by PA, notes). A supplied PA's row replaces the built-in row for the run; a PA not in the
    file keeps the built-in values. Rates are fractions (0.0766, not 7.66). The quarterly rates default to the annual rate / 4, which is how CET stores them."""
    df = _match_case(_require_columns(_read_table(path), RUN_PARAMETER_COLUMNS, "run parameters"), RUN_PARAMETER_OPTIONAL)
    df = df[df["PA"].notna() & (df["PA"].astype(str).str.strip() != "")].copy()
    if df.empty:
        raise OverrideError("run parameters: no rows")
    df["PA"] = df["PA"].astype(str).str.strip().str.upper().map(lambda s: _PA_ALIASES.get(s, s))
    if df["PA"].duplicated().any():
        raise OverrideError(f"run parameters: PA listed more than once: {sorted(df.loc[df['PA'].duplicated(), 'PA'].unique())}")
    notes: list[str] = []
    for c in RUN_PARAMETER_OUT:
        if c not in df.columns:
            df[c] = np.nan
            continue
        num = pd.to_numeric(df[c], errors="coerce")
        bad = num.isna() & df[c].notna() & (df[c].astype(str).str.strip() != "")
        if bad.any():
            raise OverrideError(f"run parameters: {c} is not a number for PA {list(df.loc[bad, 'PA'])}")
        if c in RUN_PARAMETER_COLUMNS and num.isna().any():
            raise OverrideError(f"run parameters: {c} is blank for PA {list(df.loc[num.isna(), 'PA'])}")
        df[c] = num
    for c in ("DiscountRateAnnual", "DiscountRateAnnual_SCT"):
        if ((df[c] < 0) | (df[c] >= 1)).any():
            raise OverrideError(f"run parameters: {c} must be a fraction between 0 and 1 (e.g. 0.0766 for 7.66%); got {df[c].tolist()}")
    for q, a in (("DiscountRateQtr", "DiscountRateAnnual"), ("DiscountRateQtr_SCT", "DiscountRateAnnual_SCT")):
        given = df[q].notna()
        mismatch = given & ((df[q] - df[a] / 4).abs() > 1e-9)
        if mismatch.any():
            notes.append(f"{q} is not {a} / 4 for {list(df.loc[mismatch, 'PA'])}; it is used as given (CET discounts quarterly streams with it)")
        df.loc[~given, q] = df.loc[~given, a] / 4
    unknown = sorted(set(df["PA"]) - {"PGE", "SCE", "SDGE", "SCG"})
    if unknown:
        notes.append(f"run parameters for PA(s) {unknown}: used only if a measure or program-cost row has that PA")
    return df.set_index("PA")[RUN_PARAMETER_OUT], notes


# ======================================================================================================================================
# 6. Statewide (SW) program allocation factors
# ======================================================================================================================================
SW_IOUS = ["PGE", "SDGE", "SCE", "SCG"]
SW_FACTOR_COLUMNS = ["Year", "ProgramID"] + [f"{i}{k}" for k in ("GasFactor", "ElectricFactor") for i in SW_IOUS]


def load_sw_factors(path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """Same layout as the built-in table: Year, ProgramID, <IOU>GasFactor and <IOU>ElectricFactor for PGE, SDGE, SCE, SCG, and either <IOU>WeightedFactor or
    PercentElectric (the weighted factor is then PercentElectric x electric + (1 - PercentElectric) x gas). A supplied (Year, ProgramID) row replaces the built-in
    row; a new one is added. Returns (table, notes)."""
    df = _require_columns(_read_table(path), SW_FACTOR_COLUMNS, "SW allocation factors")
    df = _match_case(df, [f"{i}WeightedFactor" for i in SW_IOUS] + ["PercentElectric", "ProgramName"])
    df = df[df["ProgramID"].notna() & (df["ProgramID"].astype(str).str.strip() != "")].copy()
    if df.empty:
        raise OverrideError("SW allocation factors: no rows")
    df["ProgramID"] = df["ProgramID"].astype(str).str.strip()
    df["Year"] = pd.to_numeric(df["Year"], errors="coerce")
    if df["Year"].isna().any():
        raise OverrideError("SW allocation factors: Year must be a number on every row")
    df["Year"] = df["Year"].astype(int)
    dup = df.duplicated(["Year", "ProgramID"])
    if dup.any():
        raise OverrideError(f"SW allocation factors: (Year, ProgramID) repeated: {df.loc[dup, ['Year', 'ProgramID']].values.tolist()[:5]}")
    notes: list[str] = []
    for c in [c for c in df.columns if c.endswith(("GasFactor", "ElectricFactor", "WeightedFactor")) or c == "PercentElectric"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if not all(f"{i}WeightedFactor" in df.columns for i in SW_IOUS):
        if "PercentElectric" not in df.columns or df["PercentElectric"].isna().any():
            raise OverrideError("SW allocation factors: give <IOU>WeightedFactor for every IOU, or PercentElectric on every row so it can be computed")
        for i in SW_IOUS:
            df[f"{i}WeightedFactor"] = df["PercentElectric"] * df[f"{i}ElectricFactor"] + (1 - df["PercentElectric"]) * df[f"{i}GasFactor"]
        notes.append("WeightedFactor computed as PercentElectric x ElectricFactor + (1 - PercentElectric) x GasFactor")
    for k in ("GasFactor", "ElectricFactor", "WeightedFactor"):
        cols = [f"{i}{k}" for i in SW_IOUS]
        if df[cols].isna().any().any():
            raise OverrideError(f"SW allocation factors: blank {k} values (use 0 for an IOU with no share)")
        if (df[cols] < 0).any().any():
            raise OverrideError(f"SW allocation factors: negative {k} values")
        s = df[cols].sum(axis=1)
        off = ~(np.isclose(s, 1.0, atol=1e-3) | np.isclose(s, 0.0, atol=1e-9))
        if off.any():
            notes.append(f"{k}s do not sum to 1 (or 0) across the four IOUs on {int(off.sum())} row(s), e.g. {df.loc[off, ['Year', 'ProgramID']].values.tolist()[:3]}: "
                         "the allocated shares will not add up to the program's total")
    return df, notes
