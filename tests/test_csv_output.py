"""CSV output (outputs.write_csv): pyarrow formatting with compression on a second thread, ISA-L when installed, parquet first for runs in parts."""
from __future__ import annotations

import io
import sys
import time
import zipfile

import numpy as np
import pandas as pd
import pytest

from recet import outputs


def _frame():
    return pd.DataFrame({"i": np.arange(5), "f": [0.0, 1.5, np.nan, 7.04e-05, 1e20], "b": [True, False, True, False, True],
                         "s": ["x", "", None, "a,b \"q\"", "é"], "d": pd.date_range("2018-01-01", periods=5, freq="h")})


def test_csv_reads_back_as_pandas_csv_does(tmp_path):
    df = _frame()
    p = outputs.write_table(df, tmp_path / "t", "csv")
    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"                                            # BOM, so Excel reads UTF-8
    text = raw[3:].decode("utf-8")
    assert '"True"' in text and '"False"' in text and "true" not in text        # booleans as pandas writes them
    assert "2018-01-01 01:00:00" in text and "00:00:00.0" not in text           # timestamps to the second
    new = pd.read_csv(p, encoding="utf-8-sig")
    old = pd.read_csv(io.StringIO(df.to_csv(index=False)))
    for c in df:
        assert new[c].equals(old[c]), c


def test_zipped_csv_is_the_same_csv_dated_now(tmp_path):
    df = _frame()
    plain = outputs.write_table(df, tmp_path / "a", "csv").read_bytes()
    z = outputs.write_table(df, tmp_path / "b", "csv.zip")
    with zipfile.ZipFile(z) as f:
        info = f.infolist()[0]
        assert info.filename == "b.csv" and f.read("b.csv") == plain
        assert info.date_time[:3] == time.localtime()[:3]                       # not zipfile's default 1980-01-01


def test_zlib_is_used_when_isal_is_missing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "isal", None)                               # import isal -> ImportError
    assert outputs.deflater()[1] == "zlib level 6"
    z = outputs.write_table(_frame(), tmp_path / "z", "csv.zip")
    assert zipfile.ZipFile(z).testzip() is None


def test_isal_when_installed(tmp_path):
    pytest.importorskip("isal")
    assert outputs.deflater()[1] == "ISA-L level 1"
    z = outputs.write_table(_frame(), tmp_path / "z", "csv.zip")
    assert zipfile.ZipFile(z).testzip() is None                                  # ordinary deflate: the standard library reads and checks it


def test_parts_go_through_parquet_which_is_kept_for_the_results(tmp_path):
    df = pd.concat([_frame()] * 30, ignore_index=True)
    w = outputs.TableWriter(tmp_path / "measure_level_acc2026", "csv.zip", len(df), row_group_rows=40, keep_parquet=True)
    for i in range(0, len(df), 7):
        w.write(df.iloc[i:i + 7])
    p = w.close()
    kept = tmp_path / outputs.RESULTS_DIR / "measure_level_acc2026.parquet"
    assert kept.exists() and outputs.results_source(tmp_path) == kept
    pd.testing.assert_frame_equal(pd.read_parquet(kept), df)
    assert zipfile.ZipFile(p).read("measure_level_acc2026.csv") == outputs.write_table(df, tmp_path / "x", "csv").read_bytes()
    assert not list(tmp_path.glob(f"{outputs.RESULTS_DIR}/*.part*"))
    w = outputs.TableWriter(tmp_path / "other", "csv", len(df))                  # not kept: nothing is left beside the CSV
    w.write(df)
    w.close()
    assert not (tmp_path / outputs.RESULTS_DIR / "other.parquet").exists()


def test_a_failure_while_formatting_leaves_no_file(tmp_path):
    import pyarrow as pa

    def batches():
        yield pa.RecordBatch.from_pandas(_frame())
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        outputs.write_csv(batches(), tmp_path / "t.csv.zip", zipped=True)
    assert not (tmp_path / "t.csv.zip").exists()
