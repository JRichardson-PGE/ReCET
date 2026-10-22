"""Reading the CET-format input files (Measure.csv, ProgramCost.csv, a CET outputs file).

CET itself takes pipe-delimited text. ReCET also accepts the same columns as an ordinary comma-delimited CSV (what Excel's "Save as CSV" writes) or a
tab-delimited file: the delimiter is taken from the header line, which never contains a comma or tab in a CET-format file. A file that is not UTF-8
(Excel's CSV export on Windows is often cp1252) is read as cp1252.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

SEPARATORS = {"|": "pipe", ",": "comma", "\t": "tab"}


def _first_line(path: str | Path) -> str:
    raw = Path(path).read_bytes()[:65536]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc).splitlines()[0] if raw else ""
        except UnicodeDecodeError:
            continue
    return ""


def detect_sep(path: str | Path) -> str | None:
    """'|', ',' or a tab, from the header line; None if the header has none of them (a one-column file, or not a CET-format file)."""
    head = _first_line(path)
    for sep in SEPARATORS:
        if sep in head:
            return sep
    return None


def describe(sep: str | None) -> str:
    return SEPARATORS.get(sep, "unknown") + "-delimited" if sep else "not delimited"


def header_columns(path: str | Path) -> list[str]:
    sep = detect_sep(path)
    head = _first_line(path)
    return [c.strip().strip('"') for c in head.split(sep)] if sep else [head.strip()]


def read_cet_csv(path: str | Path, **kw) -> pd.DataFrame:
    """pd.read_csv with the file's own delimiter (pipe if it cannot be told). Tries UTF-8 first, then cp1252. Column names are stripped."""
    sep = detect_sep(path) or "|"
    kw.pop("sep", None)
    enc = kw.pop("encoding", "utf-8-sig")
    try:
        df = pd.read_csv(path, sep=sep, encoding=enc, **kw)
    except UnicodeDecodeError:
        df = pd.read_csv(path, sep=sep, encoding="cp1252", **kw)
    df.columns = [str(c).strip() for c in df.columns]
    return df
