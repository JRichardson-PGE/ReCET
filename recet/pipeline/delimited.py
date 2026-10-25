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
    with open(path, "rb") as f:                         # the first 64 KB only (it read the whole file: 600 MB for a 1M-row Measure.csv, on every call)
        raw = f.read(65536)
    raw = raw.splitlines()[0] if raw else b""           # the header line alone, so a multi-byte character cut at 64 KB cannot spoil the decode
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
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


PARTS_MARKER = ".recet_parts"     # in a folder of parts written by split_file: reads of its files are kept (see read_cet_csv)


def read_cet_csv(path: str | Path, **kw) -> pd.DataFrame:
    """pd.read_csv with the file's own delimiter (pipe if it cannot be told). Tries UTF-8 first, then cp1252. Column names are stripped.

    A part written by split_file is read several times in a run in parts, the same way each time (preflight, pass 1, pass 2). Each distinct read of such a
    file is kept beside it -- a text read (dtype=str) as parquet, any other as a pickle; both give back exactly the same frame -- and the next identical read
    loads that instead of parsing the CSV again. The parts folder, kept reads included, is deleted when the run ends. Files elsewhere are always parsed."""
    if (Path(path).parent / PARTS_MARKER).exists() and not any(callable(v) and not isinstance(v, type) for v in kw.values()):   # dtype=str is fine; a usecols function is not
        import hashlib
        key = hashlib.sha1(repr(sorted(kw.items())).encode()).hexdigest()[:12]
        text = kw.get("dtype") is str                   # all text: parquet is ~40x smaller and quick to read; typed: pickle reads ~6x faster than parquet
        cached = Path(path).with_name(f"{Path(path).stem}.{key}.{'parquet' if text else 'pkl'}")
        if cached.exists():
            return pd.read_parquet(cached) if text else pd.read_pickle(cached)
        df = _read_cet_csv(path, **kw)
        tmp = cached.with_name(cached.name + ".tmp")
        if text:
            df.to_parquet(tmp, index=False)
        else:
            df.to_pickle(tmp)
        tmp.replace(cached)
        return df
    return _read_cet_csv(path, **kw)


def _read_cet_csv(path: str | Path, **kw) -> pd.DataFrame:
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
    (out_dir / PARTS_MARKER).write_text("parts written by recet.pipeline.delimited.split_file; reads of them are kept here as .pkl\n", encoding="utf-8")
    enc = file_encoding(path)
    if sep == "|" and not _has_quote(path):
        return _split_lines(path, rows, out_dir, enc)
    parts, start = [], 0
    reader = pd.read_csv(path, sep=sep, encoding=enc, dtype=str, keep_default_na=False, chunksize=rows)
    for i, df in enumerate(reader):
        df.columns = [str(c).strip() for c in df.columns]
        df = to_recet(df)
        f = out_dir / f"part_{i:04d}.csv"
        df.to_csv(f, sep="|", index=False, encoding="utf-8")
        parts.append((f, start, len(df)))
        start += len(df)
    return parts


def _has_quote(path: str | Path) -> bool:
    """True if the file has a double quote anywhere (pandas would then read a quoted field, which may hold a delimiter or a line break)."""
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            if b'"' in block:
                return True
    return False


def _split_lines(path: str | Path, rows: int, out_dir: Path, enc: str) -> list[tuple[Path, int, int]]:
    """split_file for a pipe-delimited file with no quotes (CET's own format: text is unquoted), without parsing it: the header is rewritten with ReCET's
    column names (stripped, legacy names renamed), and the data lines are copied as they are, `rows` to a part; a blank line is dropped, as pandas drops it.
    A cp1252 file is re-encoded as UTF-8. Reading a part gives what reading the same rows of the original gives."""
    from ..fieldnames import to_recet
    parts: list[tuple[Path, int, int]] = []
    start, i, n, out = 0, 0, 0, None
    with open(path, "rb") as src:
        head = src.readline().decode(enc).rstrip("\r\n")
        cols = list(to_recet(pd.DataFrame(columns=[c.strip() for c in head.split("|")])).columns)
        header = ("|".join(cols) + "\n").encode("utf-8")
        for line in src:
            if not line.strip(b"\r\n"):
                continue
            if out is None:
                f = out_dir / f"part_{i:04d}.csv"
                out = open(f, "wb")
                out.write(header)
            if not line.endswith(b"\n"):
                line += b"\n"
            out.write(line if enc == "utf-8-sig" else line.decode(enc).encode("utf-8"))
            n += 1
            if n == rows:
                out.close()
                parts.append((f, start, n))
                start, i, n, out = start + n, i + 1, 0, None
        if out is not None:
            out.close()
            parts.append((f, start, n))
    return parts


def part_dtypes(parts: list[tuple[Path, int, int]], values_of: tuple[str, ...] = ()) -> tuple[dict[str, str], dict[str, set]]:
    """The column types pandas would give the WHOLE file that `parts` (split_file) came from, worked out from each part's own types: a column that is blank
    throughout one part reads as float there, and as text in the whole file if any part has text in it. Returns (column -> dtype for read_cet_csv's
    `dtype`, column -> its distinct values for each column named in `values_of`, as read with the whole-file type). Reads one part at a time."""
    kinds: dict[str, list[str]] = {}
    for f, _start, _n in parts:
        df = _read_cet_csv(f, low_memory=False)            # not kept (read_cet_csv): no later read uses these inferred types
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
            df = _read_cet_csv(f, low_memory=False, usecols=list(values), dtype={c: dtypes[c] for c in values})
            for c in values:
                values[c].update(df[c].tolist())
    return dtypes, values
