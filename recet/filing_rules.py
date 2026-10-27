"""CET's filing validation rules, as one rule library (BRD BR-06.3 / BR-03.6; it starts BR-03.1 and BR-03.3).

CEDARS publishes the checks its upload applies to a Measure.csv / ProgramCost.csv, per kind of filing, in its "source of truth" files: `validation_rules.csv`
(a record that fails one is rejected), `warning_rules.csv` (a record that fails one gets a warning), and the field constraints of the table spec
(`cet_spec.sql` / `cei_spec.sql`: NotEmpty, CedarsValueList, CedarsNumRange, ...). They ship unchanged in `data/cet_rules/<set>_<effective date>/`, one folder
per rule set and version, and this module reads them as they are, so updating a rule set means adding the newer download as a new folder:

* claims        (Quarterly Claims, https://cedars.cpuc.ca.gov/cet_ui/spec/)
* budget_filing (Budget & Application Filings, https://cedars.cpuc.ca.gov/filings/spec/)

Each rule has a stable ID built from its content (`V` validation rule, `W` warning rule, `S` spec constraint, then six hex digits), so a rule that is in both
sets, or in two versions of one set, keeps its ID. `check_frame` applies a set's rules to a table and returns, per rule, the rows that fail it; preflight
reports them and the run lists the failing records in its filing_validation table. Nothing here stops a run: ReCET prices every record and says which ones
CEDARS would reject.

How the rules are read (CEDARS' own code is not published, so these are choices, kept consistent):
* a rule applies to the rows where its Where conditions hold (all of them, or any for Where_connector = any); it fails where its own test then does not hold;
* a blank value makes a comparison unknown, as in SQL: a condition on it does not hold, a test on it fails (blank required fields are a NotEmpty failure);
* text compares case-insensitively; a value that names a column (EUL_Yrs, ClaimYearQuarter) is that row's value, and "EUL_Yrs /2.75" is arithmetic on it;
* a value list is checked for the record's claim year (active codes only), as the input editor does (editor.code_status), with any admin changes applied;
* CET's AvoidedCostCombo list (22 MB) does not ship with ReCET: the combo rule is checked against the CEDARS impact-profile library instead (an exact
  end use + IOU + target sector match), and is not checked when the library is not installed;
* the messages follow CET's upload feedback ("When UnitIncentiveToOthers equals 0 and ..., UnitMeaCost1stBaseline should be different from 0, but is 0.0");
  only that one wording has been seen, the others are written the same way.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .fieldnames import recet_name

SETS = {"claims": "Quarterly Claims", "budget_filing": "Budget & Application Filings"}
CYQ = re.compile(r"^\d{4}Q[1-4]$")
# CET value-list name -> the list shipped in data/value_lists (renamed after the AC_ fields they fill, see its SOURCE.txt)
VALUE_LIST_FILES = {"e3climatezone": "ac_climate_zone", "e3gassavprofile": "ac_gas_impact_profile", "e3gassector": "ac_gas_target_sector",
                    "e3meaelecenduseshape": "ac_elec_impact_profile", "e3targetsector": "ac_elec_target_sector"}
_CHECK_PHRASES = {
    "equals": "equal", "does not equal": "be different from", "is less than": "be less than", "is less than or equal to": "be less than or equal to",
    "greater than": "be greater than", "is greater than": "be greater than", "is greater than or equal to": "be greater than or equal to",
    "not in": "not be one of", "in": "be one of", "is not null": "not be blank", "ends with string": "end with", "matches_year_quarter": "match the year of",
    "combo_value_list": "be a combination in",
}


@dataclass
class Rule:
    id: str
    severity: str                  # "reject" | "warning"
    table: str                     # "Measure" | "ProgramCost"
    field: str                     # CET's name for the field tested (a "+" combination for the combo rule)
    text: str                      # CET-style description, without the "but is ..." part
    kind: str                      # "rule" (validation / warning rule) | "spec" (a field constraint of the spec)
    source: str                    # the file it comes from
    spec: dict = field(default_factory=dict)


@dataclass
class RuleSet:
    name: str                      # "claims" | "budget_filing"
    version: str                   # the effective date of the CEDARS files (the folder's date), e.g. "2026-03-12"
    folder: Path
    rules: list[Rule]

    @property
    def label(self) -> str:
        return f"{SETS[self.name]} rules of {self.version}"

    def describe(self) -> dict:
        """For run_settings.json (BR-03.3: the rule set used and its effective date)."""
        return {"rule_set": self.name, "label": SETS[self.name], "effective_date": self.version, "rules": len(self.rules),
                "rejects": sum(r.severity == "reject" for r in self.rules), "warnings": sum(r.severity == "warning" for r in self.rules),
                "files": sorted(p.name for p in self.folder.iterdir() if p.is_file())}


@dataclass
class Context:
    """What a check needs beyond the table: the filing year (budget filings: PrgYear and CEInputID must name it) and whether the profile library is there."""
    filing_year: int | None = None
    library: bool = False


@dataclass
class Result:
    rule: Rule
    fail: np.ndarray               # bool per row
    actual: pd.Series | None = None

    def message(self) -> str:
        return f"{self.rule.id}: {self.rule.text}"


# ------------------------------------------------------------------ the library

def _root() -> Path:
    from . import paths
    return Path(paths.package_data("cet_rules", ""))


def versions(name: str) -> list[str]:
    """The effective dates of the shipped versions of rule set `name`, oldest first."""
    if name not in SETS:
        raise ValueError(f"unknown rule set {name!r}; choose from {', '.join(SETS)}")
    return sorted(p.name.rpartition("_")[2] for p in _root().iterdir() if p.is_dir() and p.name.rpartition("_")[0] == name)


@lru_cache(maxsize=None)
def rule_set(name: str, as_of: str | None = None) -> RuleSet:
    """Rule set `name` in effect on `as_of` (YYYY-MM-DD; default: the newest shipped version)."""
    vs = versions(name)
    usable = [v for v in vs if as_of is None or v <= as_of]
    if not usable:
        raise ValueError(f"no {name} rule set in effect on {as_of} (versions: {', '.join(vs)})")
    v = usable[-1]
    folder = _root() / f"{name}_{v}"
    rules: list[Rule] = []
    for f in sorted(folder.glob("validation_rules*.csv")):
        rules += _read_rule_file(f, "reject")
    for f in sorted(folder.glob("warning_rules*.csv")):
        rules += _read_rule_file(f, "warning")
    for f in sorted(folder.glob("*_spec*.sql")):
        rules += _read_spec(f)
    seen: dict[str, Rule] = {}
    for r in rules:                 # the same rule twice in one set (the claims file lists one twice): keep one
        seen.setdefault(r.id, r)
    return RuleSet(name, v, folder, list(seen.values()))


def _rule_id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p).strip().lower() for p in parts).encode()).hexdigest()[:6]


def _read_rule_file(path: Path, severity: str) -> list[Rule]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    out = []
    for row in df.to_dict("records"):
        row = {k.strip(): str(v).strip() for k, v in row.items()}
        conds = [(row.get(f"Where_table{i}", ""), row.get(f"Where_field{i}", ""), row.get(f"Where_rule{i}", ""), row.get(f"Where_value{i}", ""))
                 for i in range(1, 6)]
        conds = [c for c in conds if c[1]]
        connector = (row.get("Where_connector") or "all").lower()
        spec = {"field": row["Field"], "rule": row["Rule"].lower(), "value": row["Value"], "connector": connector, "conds": conds}
        rid = _rule_id("V" if severity == "reject" else "W", row["Table"], row["Field"], row["Rule"], row["Value"], connector if len(conds) > 1 else "",
                       *(x for c in sorted(conds) for x in c))
        out.append(Rule(rid, severity, row["Table"], row["Field"], _describe(spec), "rule", path.name, spec))
    return out


def _describe(spec: dict) -> str:
    join = " or " if spec["connector"] == "any" else " and "
    when = join.join(f"{f} {r} {v}".strip() for _t, f, r, v in spec["conds"])
    check = f"{spec['field']} should {_CHECK_PHRASES.get(spec['rule'], spec['rule'])} {spec['value']}".strip()
    return f"When {when}, {check}" if when else check[:1].upper() + check[1:]


_SPEC_LINE = re.compile(r"^\s*(\w+)\s+(nvarchar\((\d+)\)|numeric|bit|CedarsYearQuarter)(?=[\s,)]|$)(.*?)[,)]*;?\s*$", re.I)


def _read_spec(path: Path) -> list[Rule]:
    out: list[Rule] = []
    table = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*create table (\w+)", line, re.I)
        if m:
            table = m.group(1)
            continue
        pk = re.match(r"\s*primary key\s*\(([^)]*)\)", line, re.I)
        if pk and table:
            cols = [c.strip() for c in pk.group(1).split(",")]
            if len(cols) > 1:       # one-column keys are the CEInputID check preflight always makes (duplicate_id)
                out.append(_spec_rule(path, table, " + ".join(cols), "unique", {"columns": cols}, f"{' + '.join(cols)} should be unique"))
            continue
        m = _SPEC_LINE.match(line)
        if not m or not table:
            continue
        col, typ, length, rest = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if typ.startswith("nvarchar"):
            out.append(_spec_rule(path, table, col, "max_length", {"n": int(length)}, f"{col} should be at most {length} characters"))
        elif typ == "numeric":
            out.append(_spec_rule(path, table, col, "numeric", {}, f"{col} should be a number"))
        elif typ == "bit":
            out.append(_spec_rule(path, table, col, "bit", {}, f"{col} should be 0, 1, True or False"))
        elif typ == "cedarsyearquarter":
            out.append(_spec_rule(path, table, col, "year_quarter", {}, f"{col} should be a year and quarter such as 2028Q1"))
        if re.search(r"\bNotEmpty\b", rest, re.I):
            out.append(_spec_rule(path, table, col, "not_empty", {}, f"{col} should not be empty"))
        v = re.search(r"CedarsValueList\s+(\w+)", rest, re.I)
        if v:
            out.append(_spec_rule(path, table, col, "value_list", {"list": v.group(1)}, f"{col} should be in the {v.group(1)} value list"))
        r = re.search(r"CedarsNumRange\s*([\[(])\s*([-\d.]*)\s*,\s*([-\d.]*)\s*([\])])", rest, re.I)
        if r:
            lo, hi = (float(x) if x else None for x in (r.group(2), r.group(3)))
            out.append(_spec_rule(path, table, col, "range", {"lo": lo, "hi": hi, "lo_open": r.group(1) == "(", "hi_open": r.group(4) == ")"},
                                  f"{col} should be in {r.group(1)}{r.group(2)}, {r.group(3)}{r.group(4)}"))
        g = re.search(r"GreaterThanOrEqualTo\s+([-\d.]+)", rest, re.I)
        if g:
            out.append(_spec_rule(path, table, col, "range", {"lo": float(g.group(1)), "hi": None, "lo_open": False, "hi_open": False},
                                  f"{col} should be greater than or equal to {g.group(1)}"))
        s = re.search(r"StartsWith\s+(\S+)", rest, re.I)
        if s:
            out.append(_spec_rule(path, table, col, "starts_with", {"pattern": s.group(1)}, f"{col} should start with {s.group(1)}"))
    return out


def _spec_rule(path: Path, table: str, col: str, kind: str, params: dict, text: str) -> Rule:
    rid = _rule_id("S", table, col, kind, *sorted(f"{k}={v}" for k, v in params.items()))
    return Rule(rid, "reject", table, col, text, "spec", path.name, {"check": kind, **params})


def library_frame(name: str, as_of: str | None = None) -> pd.DataFrame:
    """The rule set as a table (for the docs and the run's outputs)."""
    rs = rule_set(name, as_of)
    return pd.DataFrame([{"rule_id": r.id, "severity": r.severity, "table": r.table, "field": r.field, "rule": r.text, "kind": r.kind, "source": r.source}
                         for r in rs.rules])


# ------------------------------------------------------------------ applying rules

class _Table:
    """A table's raw text columns, looked up by CET name or ReCET name: stripped, blanks as NA and numbers parsed once per column."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.n = len(df)
        self._text: dict[str, pd.Series] = {}
        self._num: dict[str, pd.Series] = {}
        self._combo: dict[tuple, np.ndarray] = {}

    def has(self, col: str) -> bool:
        return recet_name(col) in self.df.columns

    def text(self, col: str) -> pd.Series:
        c = recet_name(col)
        if c not in self._text:
            if c not in self.df.columns:
                self._text[c] = pd.Series(pd.NA, index=self.df.index, dtype="string")
            else:
                s = self.df[c].astype("string").str.strip()
                self._text[c] = s.mask(s == "")
        return self._text[c]

    def num(self, col: str) -> pd.Series:
        c = recet_name(col)
        if c not in self._num:
            s = self.text(col)
            try:                            # the usual case, every value a number or blank: one vectorised cast (to_numeric is slow on text)
                import pyarrow as pa
                import pyarrow.compute as pc
                v = pc.cast(pa.array(s, type=pa.string(), from_pandas=True), pa.float64()).to_numpy(zero_copy_only=False)
                self._num[c] = pd.Series(v, index=s.index)
            except Exception:               # some value is not a number: it becomes NaN
                self._num[c] = pd.to_numeric(s, errors="coerce").astype(float)
        return self._num[c]


def _operand(raw: str, t: _Table, ctx: Context):
    """A rule's Value: a number, a quoted text, a list, FilingYear, a column of the table, or arithmetic on one ("EUL_Yrs /2.75")."""
    v = str(raw).strip()
    if v.startswith("["):
        return ("list", [str(x).strip().lower() for x in ast.literal_eval(v)])
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return ("text", v[1:-1])
    try:
        return ("num", float(v))
    except ValueError:
        pass
    if v.lower() == "filingyear":
        return ("num", float(ctx.filing_year)) if ctx.filing_year is not None else ("missing", "FilingYear")
    m = re.match(r"^(\w+)\s*([*/+-])\s*([\d.]+)$", v)
    if m and t.has(m.group(1)):
        a, op, b = t.num(m.group(1)), m.group(2), float(m.group(3))
        return ("col", {"*": a * b, "/": a / b, "+": a + b, "-": a - b}[op], v)
    if t.has(v):
        return ("col", None, v)
    return ("text", v)


def _compare(t: _Table, col: str, rule: str, value: str, ctx: Context) -> tuple[np.ndarray, np.ndarray]:
    """(holds, known) for `col rule value` on every row. `known` is False where a blank made the comparison unknown."""
    kind, *rest = _operand(value, t, ctx)
    if rule == "is not null":
        s = t.text(col)
        return s.notna().to_numpy(), np.ones(t.n, bool)
    if rule == "is null":
        s = t.text(col)
        return s.isna().to_numpy(), np.ones(t.n, bool)
    if kind == "missing":
        raise KeyError(rest[0])
    if rule in ("not in", "in"):
        s = t.text(col)
        known = s.notna().to_numpy()
        inside = s.str.lower().isin(rest[0]).fillna(False).to_numpy(bool)
        return (inside if rule == "in" else ~inside) & known, known
    if rule in ("ends with string", "starts with string"):
        s = t.text(col).str.lower()
        v = str(rest[0]).lower()
        hit = (s.str.endswith(v) if rule.startswith("ends") else s.str.startswith(v)).fillna(False).to_numpy(bool)
        return hit, s.notna().to_numpy()
    if rule == "matches_year_quarter":
        q = t.text(value).str[:4]
        s = t.text(col)
        known = (s.notna() & q.notna()).to_numpy()
        return (s == q).fillna(False).to_numpy(bool) & known, known
    numeric = kind == "num" or (kind == "col" and rule not in ("equals", "does not equal"))
    if kind == "col" and rule in ("equals", "does not equal"):
        numeric = t.num(rest[1]).notna().any() or t.num(col).notna().any()
    if numeric:
        left = t.num(col).to_numpy(float)
        right = rest[0] if kind == "num" else (rest[0] if rest[0] is not None else t.num(rest[1])).to_numpy(float)
        known = ~np.isnan(left) & ~np.isnan(np.broadcast_to(right, left.shape))
        with np.errstate(invalid="ignore"):
            out = {"equals": left == right, "does not equal": left != right, "is less than": left < right, "is less than or equal to": left <= right,
                   "greater than": left > right, "is greater than": left > right, "is greater than or equal to": left >= right,
                   "greater than or equal to": left >= right, "less than": left < right}.get(rule)
        if out is None:
            raise ValueError(f"unknown rule {rule!r}")
        return out & known, known
    left = t.text(col).str.lower()
    right = str(rest[0]).lower() if kind == "text" else t.text(rest[1]).str.lower()
    known = (left.notna() & (pd.Series(True, index=left.index) if kind == "text" else right.notna())).to_numpy()
    eq = (left == right).fillna(False).to_numpy(bool)
    if rule == "equals":
        return eq & known, known
    if rule == "does not equal":
        return ~eq & known, known
    raise ValueError(f"rule {rule!r} on text")


def _combo_ok(t: _Table, fields: list[str]) -> np.ndarray:
    """IOU_AC_Territory + E3TargetSector + E3MeaElecEndUseShape (+ E3ClimateZone) against the profile library (exact match), once per distinct combination."""
    key = tuple(fields[:3])
    if key not in t._combo:
        from .pipeline import real_impact_profiles
        keys = pd.DataFrame({f: t.text(f).fillna("") for f in fields[:3]})
        codes, uniq = pd.MultiIndex.from_frame(keys).factorize()
        ok = np.fromiter((real_impact_profiles.match_profile_id(e, i, ts)[1] for i, ts, e in uniq), bool, count=len(uniq))
        t._combo[key] = ok[codes]
    return t._combo[key]


def _apply_rule(r: Rule, t: _Table, ctx: Context) -> Result | None:
    s = r.spec
    applies = np.ones(t.n, bool) if s["connector"] != "any" or not s["conds"] else np.zeros(t.n, bool)
    for _tb, f, rule, v in s["conds"]:
        held, _k = _compare(t, f, rule.lower(), v, ctx)
        applies = (applies & held) if s["connector"] != "any" else (applies | held)
    if s["rule"] == "combo_value_list":
        if not ctx.library:
            return None
        fields = [x.strip() for x in s["field"].split("+")]
        return Result(r, applies & ~_combo_ok(t, fields),
                      t.text(fields[0]).fillna("") + " | " + t.text(fields[1]).fillna("") + " | " + t.text(fields[2]).fillna(""))
    held, _known = _compare(t, s["field"], s["rule"], s["value"], ctx)
    return Result(r, applies & ~held, t.text(s["field"]))


def _value_list_ok(name: str, values: pd.Series, years: pd.Series | None) -> np.ndarray:
    """Whether each value is an active code of CET value list `name` in its row's year (blank: True, NotEmpty checks it). Worked out once per distinct
    (value, year)."""
    if name.lower() == "prgid":
        from . import programs
        meta = programs.lookup(values.fillna(""))
        return (values.isna().to_numpy() | (meta["Prg_MatchedOn"].to_numpy() != "(not found)"))
    from . import editor
    df = editor.value_list(VALUE_LIST_FILES.get(name.lower(), name.lower()))
    spans: dict[str, list] = {}
    for code, a, b in zip(df["code"].astype(str).str.strip().str.lower(), df["start_year"], df["end_year"]):
        spans.setdefault(code, []).append((None if pd.isna(a) else int(a), None if pd.isna(b) else int(b)))
    vcode, vuniq = pd.factorize(values.str.lower().fillna(""))
    y = (years if years is not None else pd.Series(np.nan, index=values.index)).fillna(-1).to_numpy().astype(np.int64)
    key, inverse = np.unique(vcode.astype(np.int64) * 100_000 + (y + 1), return_inverse=True)     # one entry per distinct (value, year)
    ok = np.empty(len(key), bool)
    for j, k in enumerate(key):
        v, yr = vuniq[k // 100_000], int(k % 100_000) - 1
        sp = spans.get(v)
        ok[j] = v == "" or (bool(sp) and (yr < 0 or any((a is None or a <= yr) and (b is None or b >= yr) for a, b in sp)))
    return ok[inverse.reshape(-1)]


def _value_list_available(name: str) -> bool:
    if name.lower() == "prgid":
        return True
    from . import paths
    return Path(paths.package_data("value_lists", f"{VALUE_LIST_FILES.get(name.lower(), name.lower())}.csv")).exists()


def _apply_spec(r: Rule, t: _Table, ctx: Context, years: pd.Series | None) -> Result | None:
    s, col = r.spec, r.field
    kind = s["check"]
    if kind == "unique":
        cols = [t.text(c).fillna("") for c in s["columns"]]
        key = pd.Series(list(zip(*cols)), index=t.df.index)
        return Result(r, key.duplicated(keep=False).to_numpy(), key.map(lambda k: " + ".join(k)))
    if not t.has(col):
        return Result(r, np.ones(t.n, bool), None) if kind == "not_empty" else None
    txt = t.text(col)
    blank = txt.isna().to_numpy()
    if kind == "not_empty":
        return Result(r, blank, txt)
    if kind == "max_length":
        return Result(r, (txt.str.len() > s["n"]).fillna(False).to_numpy(bool), txt)
    if kind == "numeric":
        return Result(r, ~blank & t.num(col).isna().to_numpy(), txt)
    if kind == "bit":
        return Result(r, ~blank & ~txt.str.lower().isin(["0", "1", "true", "false"]).fillna(False).to_numpy(bool), txt)
    if kind == "year_quarter":
        return Result(r, ~blank & ~txt.str.match(CYQ).fillna(False).to_numpy(bool), txt)
    if kind == "range":
        v = t.num(col).to_numpy(float)
        lo, hi = s["lo"], s["hi"]
        with np.errstate(invalid="ignore"):
            bad = np.zeros(t.n, bool)
            if lo is not None:
                bad |= (v <= lo) if s["lo_open"] else (v < lo)
            if hi is not None:
                bad |= (v >= hi) if s["hi_open"] else (v > hi)
        return Result(r, bad & ~np.isnan(v), txt)
    if kind == "value_list":
        if not _value_list_available(s["list"]):
            return None
        return Result(r, ~_value_list_ok(s["list"], txt, years), txt)
    if kind == "starts_with":                   # UploadPA-YYYY: a plain prefix, a PA code, a dash and the filing year (CET accepts "PGE-2028Q4-...")
        from . import editor
        pas = sorted(set(editor.value_list("pa")["code"].astype(str).str.strip().str.lower()))
        want = (pd.Series(float(ctx.filing_year), index=txt.index) if ctx.filing_year is not None
                else (years if years is not None else pd.Series(np.nan, index=txt.index)).astype(float))
        low = txt.str.lower()
        ok = np.zeros(t.n, bool)
        for y in want.dropna().unique():
            rows = (want == y).to_numpy()
            heads = tuple(f"{pa}-{int(y)}" for pa in pas)
            ok[rows] = low[rows].map(lambda x: isinstance(x, str) and x.startswith(heads)).to_numpy(bool)
        return Result(r, ~blank & ~ok, txt)
    raise ValueError(f"unknown spec check {kind!r}")


def check_frame(rs: RuleSet, table: str, df: pd.DataFrame, ctx: Context) -> tuple[list[Result], list[str]]:
    """Apply rule set `rs`'s rules for `table` to `df` (the table's raw text, as read with dtype=str). Returns the results with at least one failing row, and
    notes on the rules that could not be checked (with why)."""
    t = _Table(df)
    years = None
    if table == "Measure":
        years = pd.to_numeric(t.text("ClaimYearQuarter").str[:4], errors="coerce")
    elif table == "ProgramCost":
        years = pd.to_numeric(t.text("PrgYear"), errors="coerce")
    results, notes = [], []
    for r in rs.rules:
        if r.table != table:
            continue
        try:
            res = _apply_spec(r, t, ctx, years) if r.kind == "spec" else _apply_rule(r, t, ctx)
        except KeyError as exc:
            notes.append(f"{r.id} not checked: no {exc.args[0]} given ({r.text})")
            continue
        if res is None:
            why = ("the CEDARS impact-profile library is not installed" if r.spec.get("rule") == "combo_value_list"
                   else f"value list {r.spec.get('list')} does not ship with ReCET" if r.spec.get("check") == "value_list" else "not applicable")
            notes.append(f"{r.id} not checked: {why} ({r.text})")
            continue
        if res.fail.any():
            results.append(res)
    return results, notes


def flags(results: list[Result], ids: pd.Series) -> pd.DataFrame:
    """One row per failing record: its id, FilingValidation (reject / warning) and FilingRules (the failing rule IDs, rejects first)."""
    results = sorted(results, key=lambda r: r.rule.severity != "reject")        # rejects first, in rule order
    if not results:
        return pd.DataFrame(columns=["id", "FilingValidation", "FilingRules"])
    fail = np.vstack([r.fail for r in results])                                 # rules x rows
    names = np.array([r.rule.id for r in results], dtype=object)
    reject = np.array([r.rule.severity == "reject" for r in results])
    rows = np.flatnonzero(fail.any(axis=0))
    sub = fail[:, rows]
    status = np.where(sub[reject].any(axis=0), "reject", "warning") if reject.any() else np.full(len(rows), "warning", dtype=object)
    patterns, inverse = np.unique(sub.T, axis=0, return_inverse=True)            # few distinct combinations of failing rules
    text = np.array([", ".join(names[p]) for p in patterns], dtype=object)
    return pd.DataFrame({"id": ids.astype(str).to_numpy()[rows], "FilingValidation": status, "FilingRules": text[inverse.reshape(-1)]})
