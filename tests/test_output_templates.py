"""Output templates and the key-metrics summary (BRD BR-06.10, TIER2_PLAN step 3): a saved template returns only the selected fields, in the saved order,
each linked to its definition; the key-metrics summary gives the headline results and their main drivers."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from recet import field_docs, key_metrics, outputs, output_templates as ot
from recet.jobs import RunConfig, run_job
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def _job(tmp_path, n=6):
    tmp_path.mkdir(parents=True, exist_ok=True)
    lines = make_measure_csv(tmp_path / "one.csv").read_text(encoding="utf-8").splitlines()
    head = lines[0].split("|")
    rows = []
    for i in range(n):
        cells = lines[1].split("|")
        cells[head.index("CEInputID")] = f"M{i}"
        cells[head.index("PrgID")] = "PRG1" if i < 4 else "PRG2"
        cells[head.index("UnitkWh1stBaseline")] = str(1000 * (i + 1))
        cells[head.index("UnitMeaCost1stBaseline")] = "500"
        cells[head.index("UnitIncentiveToOthers")] = "100"
        if "UseCategory" in head:
            cells[head.index("UseCategory")] = "Lighting" if i % 2 else "HVAC"
        rows.append("|".join(cells))
    path = tmp_path / "Measure.csv"
    path.write_text("\n".join([lines[0], *rows]) + "\n", encoding="utf-8")
    ref = tmp_path / "ref"
    (ref / "acc" / "2026").mkdir(parents=True)
    make_electric_acc(ref / "acc" / "2026" / "electric_hourly.parquet")
    return path, ref


def _run(tmp_path, fmt="csv", chunk_size=0, **kw):
    path, ref = _job(tmp_path)
    return run_job(RunConfig(measures=str(path), out_dir=str(tmp_path / "out"), run_name=f"r{fmt.replace('.', '')}{chunk_size}", acc_version="2026",
                             reference_dir=str(ref), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False,
                             output_format=fmt, chunk_size=chunk_size, **kw), log=lambda s: None)


TEMPLATE = ["TRCRatio", "CEInputID", "TotalSystemBenefit", "ElecBen", "NumUnits"]


@pytest.mark.parametrize("fmt", ["csv", "csv.zip", "parquet", "xlsx"])
def test_a_template_writes_only_its_columns_in_its_order(tmp_path, fmt):
    r = _run(tmp_path, fmt, output_template=TEMPLATE, output_template_name="mine")
    written = outputs.read_table(r.files["measure_level"])
    assert list(written.columns) == TEMPLATE and len(written) == 6
    full = outputs.read_table(outputs.results_source(r.run_dir))             # the Results tables still read every column
    assert len(full.columns) > 100 and {"ElecBen", "PACRatio"} <= set(full.columns)
    assert written["TotalSystemBenefit"].tolist() == pytest.approx(full["TotalSystemBenefit"].tolist())
    defs = outputs.read_table(r.files["field_definitions"])                 # each column linked to its definition
    assert defs["column"].tolist() == TEMPLATE and defs["definition"].str.len().gt(10).all()
    assert defs.set_index("column").loc["ElecBen", "documentation"] == "OUTPUT_FIELDS.md#elecben"
    settings = json.loads((r.run_dir / "run_settings.json").read_text(encoding="utf-8"))
    assert settings["config"]["output_template"] == TEMPLATE and settings["config"]["output_template_name"] == "mine"


def test_a_template_works_the_same_in_parts(tmp_path):
    whole = _run(tmp_path / "w", "csv", output_template=TEMPLATE)
    parts = _run(tmp_path / "p", "csv", chunk_size=4, output_template=TEMPLATE)
    a, b = outputs.read_table(whole.files["measure_level"]), outputs.read_table(parts.files["measure_level"])
    assert list(b.columns) == TEMPLATE
    pd.testing.assert_frame_equal(a, b)
    ka, kb = outputs.read_table(whole.files["key_metrics"]), outputs.read_table(parts.files["key_metrics"])
    pd.testing.assert_frame_equal(ka, kb)


def test_a_column_the_run_did_not_produce_is_left_out_with_a_note(tmp_path):
    r = _run(tmp_path, "csv", output_template=["CEInputID", "FirstYearNetElecCO2", "TotalSystemBenefit"])          # emissions are off
    assert list(outputs.read_table(r.files["measure_level"]).columns) == ["CEInputID", "TotalSystemBenefit"]
    assert any("FirstYearNetElecCO2" in n for n in r.notes)


def test_without_a_template_every_column_is_written_and_defined(tmp_path):
    r = _run(tmp_path, "csv")
    written = outputs.read_table(r.files["measure_level"])
    defs = outputs.read_table(r.files["field_definitions"])
    assert defs["column"].tolist() == list(written.columns)
    undefined = defs.loc[defs["definition"].fillna("") == "", "column"].tolist()
    assert not undefined, f"columns without a definition: {undefined}"


def test_the_key_metrics_summary_names_the_headline_results_and_drivers(tmp_path):
    r = _run(tmp_path, "csv")
    km = outputs.read_table(r.files["key_metrics"])
    port = km[km["section"] == "Portfolio"].set_index("item")
    assert port.loc["Total system benefit ($)", "value"] == pytest.approx(r.totals["TotalSystemBenefit"])
    summary = outputs.read_table(r.files["portfolio_summary"]).iloc[0]
    assert port.loc["TRC ratio", "value"] == pytest.approx(summary["TRC_ratio"]) and port.loc["PAC ratio", "value"] == pytest.approx(summary["PAC_ratio"])
    assert port["definition"].str.len().gt(10).all()
    progs = km[km["section"] == "Top programs"]
    assert set(progs["measures"]) == {4, 2} and progs["share_of_TSB"].sum() == pytest.approx(1.0)
    assert progs.iloc[0]["share_of_TSB"] >= progs.iloc[1]["share_of_TSB"]
    ends = km[km["section"] == "Top end uses"]
    assert set(ends["item"]) == {"HVAC", "Lighting"} and ends["share_of_TSB"].sum() == pytest.approx(1.0)


def test_templates_check_names_and_read_files(tmp_path):
    ok, bad = ot.check(["CEInputID", "ElecBen", "ElecBen", "NotAColumn", "ElecValue_energy"])
    assert ok == ["CEInputID", "ElecBen", "ElecValue_energy"] and bad == ["NotAColumn"]
    for name, cols in ot.BUILTIN.items():
        assert not ot.check(cols)[1], name
    (tmp_path / "t.txt").write_text("CEInputID\n# a comment\nElecBen, GasBen\n", encoding="utf-8")
    assert ot.read_file(tmp_path / "t.txt") == ("t", ["CEInputID", "ElecBen", "GasBen"])
    (tmp_path / "t.json").write_text(json.dumps({"name": "Mine", "columns": ["TRCRatio"]}), encoding="utf-8")
    assert ot.read_file(tmp_path / "t.json") == ("Mine", ["TRCRatio"])


def test_templates_are_saved_in_the_config(tmp_path):
    ot.save("Mine", ["CEInputID", "ElecBen"])
    assert ot.load_saved()["Mine"] == ["CEInputID", "ElecBen"] and "Key results" in ot.load_saved()
    with pytest.raises(ValueError):
        ot.save("Key results", ["CEInputID"])
    ot.save("Mine", None)
    assert "Mine" not in ot.load_saved()


def test_every_catalog_column_has_a_definition_and_a_link_that_resolves():
    slugs = {}
    for doc in ("OUTPUT_FIELDS.md", "INPUT_FIELDS.md"):
        import re
        text = (field_docs.DOCS / doc).read_text(encoding="utf-8")
        slugs[doc] = {field_docs.slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, flags=re.M)}
    for group, col, text, link in ot.catalog():
        assert text, col
        doc, _, anchor = link.partition("#")
        assert anchor in slugs[doc], (col, link)
