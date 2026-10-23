"""Reading the CET-format input files (Measure.csv, ProgramCost.csv, a CET outputs file).

CET itself takes pipe-delimited text. ReCET also accepts the same columns as an ordinary comma-delimited CSV (what Excel's "Save as CSV" writes) or a
tab-delimited file: the delimiter is taken from the header line, which never contains a comma or tab in a CET-format file. A file that is not UTF-8
(Excel's CSV export on Windows is often cp1252) is read as cp1252. Legacy CET column names (E3ClimateZone ...) are read under ReCET's names (recet.fieldnames).
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
    from ..fieldnames import recet_name
    return [recet_name(c.strip().strip('"')) for c in head.split(sep)] if sep else [recet_name(head.strip())]


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
    from ..fieldnames import to_recet
    return to_recet(df)


def file_encoding(path: str | Path) -> str:
    """'utf-8-sig' if the whole file decodes as UTF-8, else 'cp1252' (what read_cet_csv falls back to). Reads the file in blocks, never all at once."""
    import codecs
    dec = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        with open(path, "rb") as f:
            while block := f.read(1 << 22):
                dec.decode(block)
            dec.decode(b"", final=True)
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp1252"


def count_rows(path: str | Path) -> int:
    """Data rows in a delimited file (lines after the header, blank last line not counted), without parsing it. A quoted field with a line break in it would
    count twice; CET's own files have none (text is unquoted)."""
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            n += block.count(b"\n")
            last = block[-1:]
    if last != b"\n":
        n += 1                                   # a last line with no line break
    return max(n - 1, 0)


def split_file(path: str | Path, rows: int, out_dir: str | Path) -> list[tuple[Path, int, int]]:
    """Split a CET-format file into pipe-delimited UTF-8 files of at most `rows` data rows each, every value kept as written (read as text, blank stays blank).
    Columns get ReCET's names, as read_cet_csv gives them. Returns (file, index of its first row in the original, rows) per part; only one part is in memory
    at a time."""
    from ..fieldnames import to_recet
    sep = detect_sep(path) or "|"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts, start = [], 0
    reader = pd.read_csv(path, sep=sep, encoding=file_encoding(path), dtype=str, keep_default_na=False, chunksize=rows)
    for i, df in enumerate(reader):
        df.columns = [str(c).strip() for c in df.columns]
        df = to_recet(df)
        f = out_dir / f"part_{i:04d}.csv"
        df.to_csv(f, sep="|", index=False, encoding="utf-8")
        parts.append((f, start, len(df)))
        start += len(df)
    return parts


def part_dtypes(parts: list[tuple[Path, int, int]], values_of: tuple[str, ...] = ()) -> tuple[dict[str, str], dict[str, set]]:
    """The column types pandas would give the WHOLE file that `parts` (split_file) came from, worked out from each part's own types: a column that is blank
    throughout one part reads as float there, and as text in the whole file if any part has text in it. Returns (column -> dtype for read_cet_csv's
    `dtype`, column -> its distinct values for each column named in `values_of`, as read with the whole-file type). Reads one part at a time."""
    kinds: dict[str, list[str]] = {}
    for f, _start, _n in parts:
        df = read_cet_csv(f, low_memory=False)
        for c in df.columns:
            kinds.setdefault(c, []).append("empty" if df[c].isna().all() else str(df[c].dtype))
    dtypes = {}
    for c, ks in kinds.items():
        real = set(ks) - {"empty"}
        if not real or real <= {"int64", "float64"}:
            dtypes[c] = "int64" if real == {"int64"} and "empty" not in ks else "float64"      # blanks anywhere make the whole column float
        elif len(real) == 1 and "empty" not in ks:
            dtypes[c] = real.pop()
        elif len(real) == 1 and real != {"bool"}:
            dtypes[c] = real.pop()                                                            # text with blanks stays text
        else:
            dtypes[c] = "object"
    values: dict[str, set] = {c: set() for c in values_of if c in dtypes}
    if values:
        for f, _start, _n in parts:
            df = read_cet_csv(f, low_memory=False, usecols=list(values), dtype={c: dtypes[c] for c in values})
            for c in values:
                values[c].update(df[c].tolist())
    return dtypes, values
