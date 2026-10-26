"""The documentation stays true: every column, setting and control is documented, every cross-reference resolves, and the worked example's numbers are what a run gives."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import doc_examples as de
import reference_calc as ref
import test_calcs as tc
from recet import cli, gui, outputs, preflight, templates
from recet.jobs import RunConfig, run_job

DOCS = Path(gui.__file__).resolve().parent / "docs"
TEXT = {p.name: p.read_text(encoding="utf-8") for p in DOCS.glob("*.md")}


def first_cell_names(doc: str, start: str = "", end: str = "") -> set[str]:
    """Backticked names in the first cell of every table row of `doc`, between the headings `start` and `end` (whole file when blank)."""
    text = TEXT[doc]
    if start:
        text = text[text.index(start):]
    if end and end in text:
        text = text[:text.index(end)]
    names: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^\|\s*(.+?)\s*\|", line)
        if m:
            names |= set(re.findall(r"`([^`]+)`", m.group(1)))
    return names


def entry_names(doc: str, start: str, end: str) -> set[str]:
    """Backticked names in the headings and in the first cell of every table row of `doc`, between the headings `start` and `end` (the entries of OUTPUT_FIELDS.md)."""
    text = TEXT[doc]
    text = text[text.index(start):]
    if end in text:
        text = text[:text.index(end)]
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("#"):
            names |= set(re.findall(r"`([^`]+)`", line))
        m = re.match(r"^\|\s*(.+?)\s*\|", line)
        if m:
            names |= set(re.findall(r"`([^`]+)`", m.group(1)))
    return names


# ------------------------------------------------------------------------------------------------------------------------------------ the worked examples
# Example A is the one of CALCULATIONS.md C18; examples A and B are the ones of OUTPUT_FIELDS.md section 3. Both are defined and run by doc_examples.py.
E3 = de.E3


def _worked_example_run(tmp_path):
    return de.run_example(tmp_path, "A")


def _number(cell: str) -> tuple[float, float]:
    """(value, half a unit of its last printed digit)"""
    s = cell.strip().replace(",", "")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return float(s), 0.5 * 10 ** -decimals + 1e-12


def _block(text: str, name: str) -> list[list[str]]:
    body = text[text.index(f"<!-- {name}:start -->"): text.index(f"<!-- {name}:end -->")]
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in body.splitlines() if line.startswith("|") and not line.startswith("|---")][1:]


def test_worked_example_outputs_are_what_a_run_gives(tmp_path):
    f = _worked_example_run(tmp_path)
    rows = _block(TEXT["CALCULATIONS.md"], "worked-example-outputs")
    checked = 0
    for r in rows:
        for name_cell, value_cell in ((r[0], r[1]), (r[2], r[3])):
            name = name_cell.strip("`")
            want, tol = _number(value_cell)
            assert f[name] == pytest.approx(want, abs=tol), f"{name}: the document says {value_cell}, the tool gives {f[name]!r}"
            checked += 1
    assert checked == 40


def test_worked_example_quarter_table_follows_the_formulas(tmp_path):
    f = _worked_example_run(tmp_path)
    rqf = 1 + ref.settings("PGE", E3)["qtr"]
    rows = _block(TEXT["CALCULATIONS.md"], "worked-example-quarters")
    assert len(rows) == 10
    tot = np.zeros(3)
    for r in rows:
        k, quarter, hours, weight, df_, v_e, v_g, v_w = int(r[0]), r[1], int(r[2]), float(r[3]), r[4], r[5], r[6], r[7]
        year, q = int(quarter[:4]), int(quarter[-1]) - 1
        assert (year, q, hours) == (2028 + (2 + k) // 4, (2 + k) % 4, [2160, 2184, 2208, 2208][q])
        df = 1 / rqf ** ((year - 2028) * 4 + q)
        assert float(df_) == pytest.approx(df, abs=5e-7)
        e = f["poc_shape_scale"] * 0.080 * hours / 8760 * weight * df
        g = f["poc_annual_net_therms_1st"] * 1.0 * 3 / 12 * weight * df
        w = f["poc_water_scale_1st"] * 0.070 * hours / 8760 * weight * df
        for cell, want in ((v_e, e), (v_g, g), (v_w, w)):
            value, tol = _number(cell)
            assert want == pytest.approx(value, abs=tol)
        tot += (e, g, w)
    assert tot == pytest.approx([f["ElecBen"], f["GasBen"], f["WaterEnergyBen"]], rel=1e-12)


# ------------------------------------------------------------------------------------------------------------------------------------ every column is documented
def _full_frame(tmp_path):
    """A run with every optional group switched on, on a measure with a blank NTGRCost (so `poc_ntgr_cost` appears)."""
    tc.write_measures(tmp_path / "Measure.csv", {"EX1": tc.MEASURES["EX1"], "EX3": dict(tc.MEASURES["EX3"], NTGRCost="")})
    tc.write_program(tmp_path / "ProgramCost.csv")
    tc.make_acc(tmp_path / "acc.parquet")
    tc.make_gas_acc(tmp_path / "gas.csv")
    tc.make_profiles(tmp_path / "profiles.csv")
    cfg = RunConfig(measures=str(tmp_path / "Measure.csv"), programcost=str(tmp_path / "ProgramCost.csv"), out_dir=str(tmp_path / "out"), run_name="all", acc_version="2026",
                    acc_electric_file=str(tmp_path / "acc.parquet"), acc_gas_file=str(tmp_path / "gas.csv"), profiles_8760_file=str(tmp_path / "profiles.csv"),
                    components=True, portfolio_8760=True, market_effects=0.05, filing_validation="claims")
    return run_job(cfg, log=lambda s: None).frame


def test_every_measure_level_column_is_documented_and_every_documented_column_exists(tmp_path):
    frame = _full_frame(tmp_path)
    out_names = entry_names("OUTPUT_FIELDS.md", "## 1. The measure-level table", "## 2. Other files in a run folder")
    in_names = first_cell_names("INPUT_FIELDS.md", "## 1. `Measure.csv`", "## 2. `ProgramCost.csv`")
    raw = set(templates._CET_MEASURE_HEADER)
    patterns = {"ElecValue_<component>": "ElecValue_", "GasValue_<component>": "GasValue_"}

    undocumented = []
    for c in frame.columns:
        if c in out_names or (c in raw and c in in_names) or any(c.startswith(p) for p in patterns.values()):
            continue
        undocumented.append(c)
    assert not undocumented, f"columns the tool writes that OUTPUT_FIELDS.md does not describe: {undocumented}"

    missing_input = sorted(raw - in_names)
    assert not missing_input, f"Measure.csv columns INPUT_FIELDS.md does not describe: {missing_input}"

    stale = sorted(n for n in out_names if n not in frame.columns and n not in patterns and n != "the 74 `Measure.csv` columns (`CEInputID` ... `Comments`)"
                   and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n))
    assert not stale, f"OUTPUT_FIELDS.md describes columns the tool did not write: {stale}"
    assert {"ElecValue_energy", "GasValue_market", "poc_ntgr_cost"} <= set(frame.columns)      # the optional groups were really exercised


def test_secondary_output_files_are_documented(tmp_path):
    from recet import outputs
    text = TEXT["OUTPUT_FIELDS.md"]
    for name in ("measure_level_acc", "portfolio_summary_acc", "components_summary_acc", "portfolio_8760_acc", "portfolio_8760_unshaped_acc", "cet_comparison_measures_acc",
                 "cet_comparison_summary_acc", "comparison_acc", "comparison_summary_acc", "preflight.txt", "preflight_findings.xlsx", "run_settings.json", "filing_validation_acc"):
        assert name in text, name
    summary_cols = ["group", "value", "measures", *outputs.SUMMARY_TOTALS, "TRC_ratio", "PAC_ratio", "RIM_ratio", "SCB_ratio", "SCH_ratio", "TRC_NoAdmin_ratio", "PAC_NoAdmin_ratio"]
    documented = first_cell_names("OUTPUT_FIELDS.md", "### 2.2 `portfolio_summary_acc<version>`", "### 2.3")
    assert not [c for c in summary_cols if c not in documented], [c for c in summary_cols if c not in documented]
    for code in re.findall(r'rep\.add\("(?:error|warning|info)", "(\w+)"', Path(preflight.__file__).read_text(encoding="utf-8")):
        assert f"`{code}`" in text, f"preflight code {code} is not documented"


# ------------------------------------------------------------------------------------------------------------------------------------ every input, setting and control
def test_every_input_field_setting_and_supplied_column_is_documented():
    fields = TEXT["INPUT_FIELDS.md"]
    from dataclasses import fields as dc_fields
    from recet.jobs import RunConfig as RC
    settings = first_cell_names("INPUT_FIELDS.md", "## 3. Run settings", "## 4. Supplied input files")
    missing = [f.name for f in dc_fields(RC) if f.name not in settings]
    assert not missing, f"RunConfig settings not in INPUT_FIELDS.md section 3: {missing}"
    for col in ("PrgID", "PA", "PrgYear", "ClaimYearQuarter", *preflight_program_cost_fields()):
        assert f"`{col}`" in fields.split("## 2. `ProgramCost.csv`")[1].split("## 3. Run settings")[0], col
    section4 = fields.split("## 4. Supplied input files")[1].split("## 5. Reference data files")[0]
    from recet import overrides
    for col in ("end_use_name", "pa", "target_sector", "climate_zone", "hour_of_year", "impact", "represents_year", "Profile", "Month", "Impact", *overrides.ELECTRIC_ACC_REQUIRED,
                *overrides.ELECTRIC_ACC_OPTIONAL, *overrides.ELECTRIC_COMPONENTS, "utility", "class", "cost_test", "scc", "nox", "co2", "upstream_methane", "behind_meter_methane",
                "air_quality_adder"):
        assert col in section4, f"supplied-file column {col} is not described in INPUT_FIELDS.md section 4"
    assert "market_jan" in section4 and "td_dec" in section4
    from recet import workbook
    for col in (*overrides.RUN_PARAMETER_COLUMNS, *overrides.RUN_PARAMETER_OPTIONAL, *overrides.SW_FACTOR_COLUMNS, "PercentElectric", *workbook.SHEETS,
                *[f"{i}WeightedFactor" for i in overrides.SW_IOUS]):
        assert f"`{col}`" in section4, f"run-parameters / SW-factor column or workbook sheet {col} is not described in INPUT_FIELDS.md section 4"


def preflight_program_cost_fields() -> list[str]:
    from recet.pipeline import cost_tests
    return [*cost_tests._COST_FIELDS, "OnBillFinancing"]


def test_every_command_line_flag_is_documented():
    import argparse
    parser = cli.build_parser()
    run = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["run"]
    flags = {s for act in run._actions for s in act.option_strings if s.startswith("--") and s not in ("--help",)}
    text = TEXT["INPUT_FIELDS.md"]
    missing = sorted(f for f in flags if f not in text)
    assert not missing, f"`recet run` flags missing from INPUT_FIELDS.md section 3: {missing}"


def test_every_gui_control_is_described_in_the_user_guide():
    src = Path(gui.__file__).read_text(encoding="utf-8")
    ed_src = Path(gui.__file__).with_name("gui_editor.py").read_text(encoding="utf-8")
    ed_src += Path(gui.__file__).with_name("gui_results.py").read_text(encoding="utf-8")
    labels = set(re.findall(r'ttk\.(?:Button|Checkbutton)\([^)]*text="([^"]+)"', ed_src)) | {x for pair in re.findall(r'\("\w+", "([^"]+)"\), \("\w+", "([^"]+)"\)\):', ed_src) for x in pair}
    labels |= set(re.findall(r'_file_row\([^,]+,\s*\d+,\s*"([^"]+)"', src)) | set(re.findall(r'check\("\w+",\s*"([^"]+)"', src))
    labels |= set(re.findall(r'add_command\(label="([^"]+)"', src)) | set(re.findall(r'LabelFrame\([^)]*text="([^"]+)"', src))
    labels |= set(re.findall(r'ttk\.Button\([^)]*text="([^"]+)"', src)) | set(re.findall(r'ttk\.Label\(\w+, text="([^"]+)"', src))
    guide = TEXT["USER_GUIDE.md"].replace("&nbsp;", "")
    norm = lambda s: s.replace("…", "").replace("...", "").strip().rstrip(":").strip().strip(".").strip()
    skip = {"Ready", "Browse", "Running", "Not used", "Save as default", "Folder", "Run", "Stop", "Template", "(or type a new label and supply its files below)",
            "Reference data folder (the large ACC and profile files)"}
    missing = sorted(l for l in labels if norm(l) not in {norm(x) for x in skip} and norm(l) not in guide and norm(l).lstrip("1234. ") not in guide)
    assert not missing, f"GUI controls the user guide does not mention: {missing}"
    for control in ("Save as default", "Stop", "Open results folder", "Template", "Reference data folder", "Run against", "First Year", "Year(s)", "Compare with",
                    "Output format", "Run name", "Output folder"):
        assert control in guide, control


# ------------------------------------------------------------------------------------------------------------------------------------ cross-references
def test_every_section_reference_resolves():
    calc = TEXT["CALCULATIONS.md"]
    sections = set(re.findall(r"^## (C\d+)\b", calc, re.M)) | set(re.findall(r"\*\*(C\d+\.\d+)\b", calc))
    for name, text in TEXT.items():
        cited = set(re.findall(r"\bC(\d{1,2}(?:\.\d)?)(?![\d.]*[A-Za-z])", text))
        bad = sorted(c for c in cited if f"C{c}" not in sections and name != "CALCULATIONS.md")
        assert not bad, f"{name} cites sections that CALCULATIONS.md does not have: {bad}"
    cited_in_calc = set(re.findall(r"\bC(\d{1,2}\.\d)\b", calc))
    assert not sorted(c for c in cited_in_calc if f"C{c}" not in sections), sorted(c for c in cited_in_calc if f"C{c}" not in sections)


def test_document_links_point_at_files_that_exist():
    for name, text in TEXT.items():
        for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
            assert (DOCS / target).exists(), f"{name} links to {target}"


def test_the_reference_implementation_names_the_documented_sections():
    src = (Path(__file__).parent / "reference_calc.py").read_text(encoding="utf-8")
    calc = TEXT["CALCULATIONS.md"]
    sections = set(re.findall(r"^## (C\d+)\b", calc, re.M)) | set(re.findall(r"\*\*(C\d+\.\d+)\b", calc))
    for cited in set(re.findall(r"\b(C\d{1,2}(?:\.\d)?)\b", src)):
        assert cited in sections, cited


def test_every_markdown_table_row_has_as_many_cells_as_its_header():
    """GitHub splits a table row on every unescaped pipe, even inside a code span, so a bare `|` in a formula silently breaks the table."""
    pipe = re.compile(r"(?<!\\)\|")
    problems = []
    for name, text in TEXT.items():
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            if not lines[i].startswith("|"):
                i += 1
                continue
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            width = len(pipe.split(lines[i].strip())[1:-1])
            problems += [f"{name}:{i + k + 1}" for k, line in enumerate(lines[i:j]) if len(pipe.split(line.strip())[1:-1]) != width]
            i = j
    assert not problems, f"table rows with the wrong number of cells: {problems}"


def test_documents_are_packaged_and_listed_in_the_window():
    for _title, fname in gui.DOCUMENTS:
        assert (DOCS / fname).exists(), fname
    assert "docs/*.md" in (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
