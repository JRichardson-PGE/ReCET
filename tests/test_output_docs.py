"""docs/OUTPUT_FIELDS.md stays true: every `Example` line gives what a real run gives and its arithmetic works out, the quarter-by-quarter and every-column tables are what the tool
writes, every entry of section 1 has a formula and an example, and every input field named in a formula exists. The two examples are defined in doc_examples.py."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

import doc_examples as de
import reference_calc as ref
from recet import outputs, templates
from recet.pipeline import cost_tests
from test_docs import TEXT, _block, _number, entry_names

DOC = "OUTPUT_FIELDS.md"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """For each example: the measure-level row, the one-row portfolio summary, the components-mode row and the first row of the combined 8,760 profile."""
    out = {}
    for key in "AB":
        base = de.run_example_result(tmp_path_factory.mktemp(f"ex{key}"), key)
        comp = de.run_example_result(tmp_path_factory.mktemp(f"ex{key}c"), key, components=True)
        prof = de.run_example_result(tmp_path_factory.mktemp(f"ex{key}p"), key, portfolio_8760=True, profile_years=[2028, 2029, 2030])
        out[key] = {"frame": base.frame.iloc[0], "summary": outputs.summarize(base.frame).iloc[0], "components": comp.frame.iloc[0],
                    "profile": pd.read_csv(prof.files["portfolio_8760"]).iloc[0]}
    return out


# ------------------------------------------------------------------------------------------------------------------------------------ Example and Check lines
def _bullets(doc: str) -> list[str]:
    """Every '- ' bullet of the document as one line (continuation lines indented under it are joined)."""
    out: list[str] = []
    cur = None
    for line in TEXT[doc].splitlines():
        if line.startswith("- "):
            if cur:
                out.append(cur)
            cur = line
        elif line.startswith("  ") and cur and line.strip():
            cur += " " + line.strip()
        else:
            if cur:
                out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


EXAMPLE = re.compile(r"^- Example ([AB])\b[^`]*`([^`]+)`: (.*)$")
CHECK = re.compile(r"^- Check\b[^:]*: (.*)$")
RESULT = re.compile(r"^(?P<expr>.*?)= \*\*(?P<val>[^*]+)\*\*")
ARITHMETIC = re.compile(r"^[\d\s.+\-*/^(),]*$")


def _evaluate(expr: str) -> float | None:
    """The value of a purely arithmetic expression (numbers, + - * / ^ and parentheses, and max), else None."""
    e = expr.strip()
    if not any(ch.isdigit() for ch in e) or not re.search(r"[+\-*/^]", e) or not ARITHMETIC.match(e.replace("max", "")):
        return None
    return float(eval(e.replace("^", "**"), {"__builtins__": {}}, {"max": max}))     # noqa: S307 (the text is this repository's own documentation)


def _close(got: float, printed: str, wide: bool = False) -> bool:
    want, tol = _number(printed)
    return abs(got - want) <= (max(1.5 * tol, 2e-4 * abs(want)) if wide else tol + 1e-9)


def test_every_example_line_gives_what_a_run_gives(runs):
    checked = evaluated = 0
    for line in _bullets(DOC):
        m = EXAMPLE.match(line)
        if not m:
            continue
        key, name, rest = m.groups()
        r = RESULT.match(rest)
        assert r, f"an Example line without a bold result: {line[:120]}"
        run = runs[key]
        source = next((run[k] for k in ("frame", "summary", "components", "profile") if name in run[k].index), None)
        assert source is not None, f"Example {key}: `{name}` is not a column of the run, its summary, its components or its profile: {line[:100]}"
        actual, printed = source[name], r["val"].strip()
        try:
            _number(printed)
        except ValueError:
            assert str(actual) == printed, f"Example {key} `{name}`: the document says {printed}, the tool gives {actual}"
        else:
            assert _close(float(actual), printed), f"Example {key} `{name}`: the document says {printed}, the tool gives {actual!r}"
            value = _evaluate(r["expr"])
            if value is not None:
                assert _close(value, printed, wide=True), f"Example {key} `{name}`: the arithmetic on the line gives {value}, the line says {printed}: {line[:160]}"
                evaluated += 1
        checked += 1
    assert checked >= 150 and evaluated >= 80, (checked, evaluated)


def test_every_check_line_adds_up():
    n = 0
    for line in _bullets(DOC):
        m = CHECK.match(line)
        if not m:
            continue
        r = RESULT.match(m.group(1))
        assert r, line[:120]
        value = _evaluate(r["expr"])
        assert value is not None, f"a Check line whose left side is not arithmetic: {line[:120]}"
        assert _close(value, r["val"].strip(), wide=True), f"the arithmetic gives {value}, the line says {r['val']}: {line[:160]}"
        n += 1
    assert n >= 3


# ------------------------------------------------------------------------------------------------------------------------------------ the section 3 tables
GAS_SHARE = {"Annual": [1 / 12] * 12, "Winter Only": [1 / 6] * 3 + [0.0] * 6 + [1 / 6] * 3}
HOURS = [2160, 2184, 2208, 2208]
MONTHS = [(0, 3), (3, 6), (6, 9), (9, 12)]


def _terms(key: str) -> list[dict]:
    """Every quarter of an example from the formulas of section 0 (loops; reference_calc for the schedule and the phasing)."""
    ex = de.EXAMPLES[key]
    raw, meb = ex.measure, ex.market_effects
    d = ref.derive(raw)
    st = ref.settings("PGE", de.E3)
    adj_e, adj_t = (d["ntg_kwh"] + meb) / d["ntg_kwh"], (d["ntg_therm"] + meb) / d["ntg_therm"]
    rq = d["rul"] * 4.0
    share = GAS_SHARE[raw["E3GasSavProfile"]]
    rows = []
    for k, year, q, w in ref.quarter_schedule(d["claim_year"], d["claim_quarter"], d["eul"]):
        row = {"k": k, "year": year, "q": q, "hours": HOURS[q], "w": w, "months": sum(share[m] for m in range(*MONTHS[q]))}
        row["phase"] = "1st" if (rq <= 0 or k + 1 <= rq) else ("2nd" if k >= rq else f"blend {rq - k:.3f}")
        row["elec"] = ref.phased(k, rq, d["shape_scale_1st"] * adj_e, d["shape_scale_2nd"] * adj_e)
        row["gas"] = ref.phased(k, rq, d["therms_1st"] * adj_t, d["therms_2nd"] * adj_t)
        row["water"] = ref.phased(k, rq, d["water_scale_1st"] * adj_e, d["water_scale_2nd"] * adj_e)
        for sfx, test, qtr, co2 in (("", "TRC", st["qtr"], 0.10), ("_SB", "SCT Base", st["qtr_sct"], 0.30), ("_SH", "SCT High", st["qtr_sct"], 0.50)):
            df = 1 / (1 + qtr) ** ((year - 2028) * 4 + q)
            energy = de.ENERGY[test]
            acg = de.GAS_MARKET[raw["E3GasSector"]] + 0.20 + 0.05 + co2 + 0.03 + 0.02
            row[f"df{sfx}"] = df
            row[f"ve{sfx}"] = row["elec"] * w * df * (energy + 10.0) / 1000 * row["hours"] / 8760
            row[f"vg{sfx}"] = row["gas"] * w * df * acg * row["months"]
            row[f"vw{sfx}"] = row["water"] * w * df * energy / 1000 * row["hours"] / 8760
        rows.append(row)
    return rows


@pytest.mark.parametrize("key", ["A", "B"])
def test_the_quarter_tables_follow_the_formulas(key, runs):
    f = runs[key]["frame"]
    terms = _terms(key)
    rows = _block(TEXT[DOC], f"oa-quarters-{key}")
    assert len(rows) == len(terms) == 10
    for cells, t in zip(rows, terms):
        k, quarter, hours, w, df, phase, elec, ve, gas, vg, wat, vw = cells
        assert (int(k), quarter, int(hours), phase) == (t["k"], f"{t['year']}Q{t['q'] + 1}", t["hours"], t["phase"])
        assert float(w) == pytest.approx(t["w"], abs=5e-7)
        assert float(df) == pytest.approx(t["df"], abs=5e-7)
        for cell, want in ((elec, t["elec"]), (ve, t["ve"]), (gas, t["gas"]), (vg, t["vg"]), (wat, t["water"]), (vw, t["vw"])):
            value, tol = _number(cell)
            assert want == pytest.approx(value, abs=tol + 1e-9), (key, k, cell, want)
    assert sum(t["ve"] for t in terms) == pytest.approx(f["ElecBen"] - f["ElecSupplyCost"], rel=1e-9)
    assert sum(t["vg"] for t in terms) == pytest.approx(f["GasBen"] - f["GasSupplyCost"], rel=1e-9)
    assert sum(t["vw"] for t in terms) == pytest.approx(f["WaterEnergyBen"] - f["WaterEnergyCost"], rel=1e-9)


@pytest.mark.parametrize("key", ["A", "B"])
def test_the_societal_tables_follow_the_formulas(key, runs):
    f = runs[key]["frame"]
    terms = _terms(key)
    rows = _block(TEXT[DOC], f"oa-societal-{key}")
    assert len(rows) == len(terms)
    for cells, t in zip(rows, terms):
        assert (int(cells[0]), cells[1]) == (t["k"], f"{t['year']}Q{t['q'] + 1}")
        assert float(cells[2]) == pytest.approx(t["df_SB"], abs=5e-7)
        for cell, want in zip(cells[3:], (t["ve_SB"], t["ve_SH"], t["vg_SB"], t["vg_SH"], t["vw_SB"], t["vw_SH"])):
            value, tol = _number(cell)
            assert want == pytest.approx(value, abs=tol + 1e-9)
    for s in ("SB", "SH"):
        assert sum(t[f"ve_{s}"] for t in terms) == pytest.approx(f[f"ElecBen_{s}"] - f[f"ElecSupplyCost_{s}"], rel=1e-9)
        assert sum(t[f"vg_{s}"] for t in terms) == pytest.approx(f[f"GasBen_{s}"] - f[f"GasSupplyCost_{s}"], rel=1e-9)
        assert sum(t[f"vw_{s}"] for t in terms) == pytest.approx(f[f"WaterEnergyBen_{s}"] - f[f"WaterEnergyCost_{s}"], rel=1e-9)


def test_the_every_column_table_lists_exactly_what_a_run_gives(runs):
    listed = {r[0].strip("`"): r[1:] for r in _block(TEXT[DOC], "oa-all")}
    raw = set(templates._CET_MEASURE_HEADER)
    cols = [c for c in runs["A"]["frame"].index if c not in raw]
    assert list(listed) == cols, "the table must list every non-input column, in the order the tool writes them"
    for i, key in enumerate("AB"):
        f = runs[key]["frame"]
        for c in cols:
            cell, actual = listed[c][i], f[c]
            if cell == "(blank)":
                assert pd.isna(actual), (key, c)
            elif cell in ("true", "false"):
                assert bool(actual) == (cell == "true"), (key, c)
            else:
                try:
                    want = float(cell)
                except ValueError:
                    assert str(actual) == cell, (key, c)
                    continue
                assert float(actual) == pytest.approx(want, rel=1e-5, abs=1e-12), (key, c, cell, actual)


# ------------------------------------------------------------------------------------------------------------------------------------ structure and vocabulary
def _entries() -> list[tuple[str, str]]:
    """(heading, body) of every '####' entry from section 1.4 to the end of section 1."""
    text = TEXT[DOC]
    text = text[text.index("### 1.4 "):text.index("## 2. Other files in a run folder")]
    parts = re.split(r"^#### ", text, flags=re.M)[1:]
    return [(p.splitlines()[0], p) for p in parts]


def test_every_entry_has_a_formula_and_a_worked_example():
    entries = _entries()
    assert len(entries) >= 30
    for heading, body in entries:
        assert "**Formula" in body, f"entry {heading!r} has no Formula"
        if "<component>" not in heading:                       # the component columns are exemplified in 2.3
            assert re.search(r"^- Example [AB]", body, re.M), f"entry {heading!r} has no worked example"


def test_every_entry_of_1_4_to_1_13_says_what_it_depends_on():
    for heading, body in _entries():
        assert "**Inputs" in body or "Inputs:**" in body or "those of" in body, f"entry {heading!r} does not say which inputs it depends on"


INPUT_FIELD = re.compile(r"^(Unit\w+|NTGR\w+|InstallationRate\w+|RealizationRate\w+|E3\w+|NumUnits|EUL_Yrs|RUL_Yrs|ClaimYearQuarter|IOU_AC_Territory|PrgID|FuelID|MeasImpactType|"
                         r"CombustionType|MeasInflation|Sector)$")


def test_every_input_field_named_in_the_document_exists():
    header = set(templates.MEASURE_COLUMNS)                       # CET's columns and the optional ReCET-only ones
    program = set(cost_tests._COST_FIELDS) | {"OnBillFinancing", "PrgID", "PA", "PrgYear", "ClaimYearQuarter"}
    text = TEXT[DOC]
    bad = sorted({t for t in re.findall(r"`([^`\n]+)`", text) if INPUT_FIELD.match(t) and t not in header and t not in program})
    assert not bad, f"names that look like Measure.csv fields but are not: {bad}"


def test_every_poc_field_named_in_the_document_exists(runs):
    cols = set(runs["A"]["frame"].index) | set(runs["B"]["frame"].index)
    bad = sorted({t for t in re.findall(r"`(poc_[A-Za-z0-9_]+)`", TEXT[DOC]) if t not in cols and t not in ("poc_ntgr_cost", "poc_num_units")})     # named as fields that appear only when they differ from the input column
    assert not bad, f"poc_ fields named in OUTPUT_FIELDS.md that a run does not write: {bad}"


def test_the_output_reference_is_plain_ascii():
    """The window shows it as monospaced text, so no typographic characters."""
    bad = sorted({ch for ch in TEXT[DOC] if ord(ch) > 127})
    assert not bad, bad
