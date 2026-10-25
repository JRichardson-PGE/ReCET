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


def _pandas_split(path, rows, out_dir):
    """split_file's pandas route (what every file went through before the line-copy route), for comparison."""
    from recet.fieldnames import to_recet
    out_dir.mkdir(parents=True, exist_ok=True)
    parts, start = [], 0
    for i, df in enumerate(pd.read_csv(path, sep="|", encoding=delimited.file_encoding(path), dtype=str, keep_default_na=False, chunksize=rows)):
        df.columns = [str(c).strip() for c in df.columns]
        df = to_recet(df)
        f = out_dir / f"part_{i:04d}.csv"
        df.to_csv(f, sep="|", index=False, encoding="utf-8")
        parts.append((f, start, len(df)))
        start += len(df)
    return parts


@pytest.mark.parametrize("encoding,bom", [("utf-8", True), ("utf-8", False), ("cp1252", False)])
def test_line_split_reads_like_the_pandas_split(tmp_path, encoding, bom):
    """A pipe-delimited file with no quotes is split by copying lines; every part reads the same as the pandas route's part: a legacy column name renamed,
    names stripped, blank lines dropped, a missing final line break, a short row, a BOM, a cp1252 character."""
    lines = [" CEInputID |E3ClimateZone|UnitkWh1stBaseline|Note", "A1|CZ12|1.5|café", "", "A2|CZ3A|-2|", "A3|CZ1||x", "", "A4|CZ5|7e-3", "A5|CZ12|8|end"]
    raw = ("\r\n".join(lines)).encode(encoding)
    p = tmp_path / "m.csv"
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)
    assert not delimited._has_quote(p)
    fast = delimited.split_file(p, 2, tmp_path / "fast")
    slow = _pandas_split(p, 2, tmp_path / "slow")
    assert [(s, n) for _f, s, n in fast] == [(s, n) for _f, s, n in slow] == [(0, 2), (2, 2), (4, 1)]
    for (ff, *_), (sf, *_) in zip(fast, slow):
        a, b = delimited.read_cet_csv(ff, low_memory=False), delimited.read_cet_csv(sf, low_memory=False)
        pd.testing.assert_frame_equal(a, b)
        assert list(a.columns) == ["CEInputID", "AC_Climate_Zone", "UnitkWh1stBaseline", "Note"]
    assert delimited.part_dtypes(fast)[0] == delimited.part_dtypes(slow)[0]


def test_a_file_with_quotes_still_goes_through_pandas(tmp_path):
    p = tmp_path / "q.csv"
    p.write_text('CEInputID|Note\nA1|"a|b"\nA2|plain\n', encoding="utf-8")
    assert delimited._has_quote(p)
    parts = delimited.split_file(p, 5, tmp_path / "parts")
    assert delimited.read_cet_csv(parts[0][0], dtype=str)["Note"].tolist() == ["a|b", "plain"]


def test_first_line_reads_only_the_header(tmp_path):
    p = tmp_path / "wide.csv"
    p.write_bytes("A|Bé|C\n".encode("utf-8") + b"x" * 200_000)
    assert delimited._first_line(p) == "A|Bé|C" and delimited.header_columns(p)[1] == "Bé"


def test_reads_of_a_part_are_kept_and_reused(tmp_path):
    """A part written by split_file is parsed once per distinct read; the next identical read loads the kept pickle and gives the same frame. Files
    outside a parts folder are always parsed."""
    p = tmp_path / "m.csv"
    p.write_text("CEInputID|UnitkWh1stBaseline|Note\nA1|1.5|x\nA2||\n", encoding="utf-8")
    parts = delimited.split_file(p, 10, tmp_path / "parts")
    f = parts[0][0]
    typed = delimited.read_cet_csv(f, low_memory=False, dtype={"CEInputID": "object", "UnitkWh1stBaseline": "float64", "Note": "object"})
    raw = delimited.read_cet_csv(f, dtype=str, keep_default_na=False, low_memory=False)
    assert len(list(f.parent.glob(f"{f.stem}.*.pkl"))) == 1 and len(list(f.parent.glob(f"{f.stem}.*.parquet"))) == 1   # typed read: pickle; text read: parquet
    pd.testing.assert_frame_equal(delimited.read_cet_csv(f, low_memory=False, dtype={"CEInputID": "object", "UnitkWh1stBaseline": "float64", "Note": "object"}), typed)
    pd.testing.assert_frame_equal(delimited.read_cet_csv(f, dtype=str, keep_default_na=False, low_memory=False), raw)
    assert raw["Note"].tolist() == ["x", ""] and typed["UnitkWh1stBaseline"].isna().tolist() == [False, True]
    delimited.read_cet_csv(p, dtype=str)
    assert not list(tmp_path.glob("*.pkl")) and not list(tmp_path.glob("*.parquet"))
