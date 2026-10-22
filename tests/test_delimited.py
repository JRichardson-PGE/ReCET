"""Comma-delimited inputs (BRD BR-02.2): a Measure.csv / ProgramCost.csv saved as an ordinary CSV is read exactly like CET's pipe-delimited file."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import preflight, templates
from recet.jobs import RunConfig, run_job
from recet.pipeline import delimited
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def _to_comma(pipe_path, out_path, encoding="utf-8"):
    df = pd.read_csv(pipe_path, sep="|", dtype=str, keep_default_na=False)
    df.to_csv(out_path, index=False, encoding=encoding)
    return out_path


def _programcost(path, sep):
    row = {c: "0" for c in templates.PROGRAMCOST_COLUMNS}
    row.update({"PrgID": "PRG1", "PrgYear": "2028", "ClaimYearQuarter": "2028Q1", "AdminCostsOverheadAndGA": "1000", "PA": "PGE"})
    path.write_text(sep.join(row) + "\n" + sep.join(row.values()) + "\n", encoding="utf-8")
    return path


def _run(tmp_path, measures, programcost, name):
    return run_job(RunConfig(measures=str(measures), programcost=str(programcost), out_dir=str(tmp_path / "out"), run_name=name, acc_version="2026",
                             acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet")),
                             profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False), log=lambda s: None).frame


def test_detect_sep(tmp_path):
    pipe = make_measure_csv(tmp_path / "pipe.csv")
    assert delimited.detect_sep(pipe) == "|"
    assert delimited.detect_sep(_to_comma(pipe, tmp_path / "comma.csv")) == ","
    (tmp_path / "one.csv").write_text("CEInputID\nA\n", encoding="utf-8")
    assert delimited.detect_sep(tmp_path / "one.csv") is None


def test_comma_files_give_the_same_results_as_pipe_files(tmp_path):
    pipe = make_measure_csv(tmp_path / "pipe.csv", MeasDescription="LED lamp, 9 W")         # a comma inside a text field: quoted in the CSV
    comma = _to_comma(pipe, tmp_path / "comma.csv")
    a = _run(tmp_path, pipe, _programcost(tmp_path / "pc_pipe.csv", "|"), "pipe")
    b = _run(tmp_path, comma, _programcost(tmp_path / "pc_comma.csv", ","), "comma")
    for col in ("ElecBen", "TotalSystemBenefit", "TRCCost", "PACCost", "WeightedProgramCost"):
        assert b.iloc[0][col] == pytest.approx(a.iloc[0][col], rel=1e-12), col
    assert b.iloc[0]["MeasDescription"] == "LED lamp, 9 W"


def test_cp1252_csv_from_excel_is_read(tmp_path):
    pipe = make_measure_csv(tmp_path / "pipe.csv", MeasDescription="Owner’s lamp")
    comma = _to_comma(pipe, tmp_path / "comma.csv", encoding="cp1252")
    df = delimited.read_cet_csv(comma, dtype=str, keep_default_na=False)
    assert df.loc[0, "MeasDescription"] == "Owner’s lamp"


def test_preflight_reads_comma_files_and_rejects_undelimited_ones(tmp_path):
    comma = _to_comma(make_measure_csv(tmp_path / "pipe.csv"), tmp_path / "comma.csv")
    rep = preflight.check_inputs(comma)
    assert "not_pipe_delimited" in {i.code for i in rep.issues if i.severity == "info"}
    assert "no_delimiter" not in {i.code for i in rep.issues}
    (tmp_path / "one.csv").write_text("CEInputID\nA\n", encoding="utf-8")
    assert [i.code for i in preflight.check_inputs(tmp_path / "one.csv").errors] == ["no_delimiter"]
