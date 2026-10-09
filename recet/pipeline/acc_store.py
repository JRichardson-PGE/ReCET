"""A dense, memory-mapped copy of an electric ACC parquet.

The ACC files are ~17 million rows (one per iou x climate zone x cost test x year x hour). Reading one into a pandas frame and filtering it for every
(iou, zone, cost test) that a job needs costs seconds, gigabytes and a re-read for every component. This module reads the parquet's key columns once, works out
the block structure (every (iou, zone, test, year) is exactly 8,760 consecutive hours once sorted) and stores each value column as a `(blocks, 8760)` float64
array. Those arrays are cached on disk as .npy files (see `paths.cache_dir()`; `recet data clear-cache` removes them) and memory-mapped, so a second
process starts in milliseconds and only the pages a job touches are read.

Nothing is approximated: a column is the parquet's own values, reordered into blocks. A cache entry is keyed by the parquet's path, size and modification time,
so a replaced or edited ACC file is rebuilt, never served stale. If the cache directory cannot be written the arrays are simply held in memory.

Value columns: any numeric column of the parquet, plus `td` = transmission + distribution (added in that order, as the pricing has always done).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .. import paths

HOURS = 8760
CACHE_VERSION = 1
KEY_COLUMNS = ["iou", "climate_zone", "cost_test", "year", "hour_of_year"]


def signature(path: Path) -> str:
    """Identifies one version of one file: its resolved path, size and modification time."""
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|v{CACHE_VERSION}".encode("utf-8")).hexdigest()[:16]


def _codes(table: pa.Table, name: str) -> tuple[np.ndarray, list[str]]:
    col = table[name]
    if pa.types.is_dictionary(col.type):
        col = col.cast(col.type.value_type)
    uniq = pc.unique(col)
    return np.asarray(pc.index_in(col, value_set=uniq).to_numpy()).astype(np.int64), [str(v) for v in uniq.to_pylist()]


def _atomic_save(path: Path, array: np.ndarray) -> bool:
    """Write `array` to `path` so that another process never sees a half-written file. False if it could not be written (the caller keeps the array in memory)."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp.npy")
    try:
        np.save(tmp, array)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return path.exists()          # another process may have won the race with an identical file


class AccStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sig = signature(self.path)
        root = paths.cache_dir()
        self.dir: Path | None = None
        if root is not None:
            try:
                (root / f"acc-{self.sig}").mkdir(parents=True, exist_ok=True)
                self.dir = root / f"acc-{self.sig}"
            except OSError:
                self.dir = None
        self._columns: dict[str, np.ndarray] = {}
        self._order: np.ndarray | None = None
        self._load_or_build_index()
        self.blocks: dict[tuple[str, str, str], dict[int, int]] = {}
        for b in range(len(self.block_year)):
            key = (self.ious[self.block_iou[b]], self.zones[self.block_zone[b]], self.tests[self.block_test[b]])
            self.blocks.setdefault(key, {})[int(self.block_year[b])] = b

    # ------------------------------------------------------------------------------------------------------------------------- index
    def _load_or_build_index(self) -> None:
        f = self.dir / "index.npz" if self.dir else None
        if f is not None and f.exists():
            try:
                z = np.load(f, allow_pickle=False)
                self.ious, self.zones, self.tests = z["ious"].tolist(), z["zones"].tolist(), z["tests"].tolist()
                self.block_iou, self.block_zone, self.block_test, self.block_year = z["block_iou"], z["block_zone"], z["block_test"], z["block_year"]
                return
            except (OSError, ValueError, KeyError):
                pass                                                  # unreadable cache entry: rebuild it
        order = self._compute_order()
        if f is not None:
            tmp = f.with_name(f"index.{os.getpid()}.tmp.npz")
            try:
                np.savez(tmp, ious=np.array(self.ious), zones=np.array(self.zones), tests=np.array(self.tests), block_iou=self.block_iou, block_zone=self.block_zone,
                         block_test=self.block_test, block_year=self.block_year)
                os.replace(tmp, f)
            except OSError:
                pass
            _atomic_save(self.dir / "order.npy", order.astype(np.int32))
        self._order = order

    def _compute_order(self) -> np.ndarray:
        """Read the key columns, derive the block structure and return the permutation that puts the rows in (iou, zone, test, year, hour) order."""
        tab = pq.read_table(str(self.path), columns=KEY_COLUMNS)
        ic, self.ious = _codes(tab, "iou")
        zc, self.zones = _codes(tab, "climate_zone")
        tc, self.tests = _codes(tab, "cost_test")
        year = np.asarray(tab["year"].to_numpy()).astype(np.int64)
        hour = np.asarray(tab["hour_of_year"].to_numpy()).astype(np.int64)
        n = len(year)
        if n == 0 or n % HOURS:
            raise ValueError(f"{self.path.name}: {n:,} rows is not a whole number of {HOURS}-hour blocks")
        y0 = int(year.min())
        n_years = int(year.max()) - y0 + 1
        nz, nt = len(self.zones), len(self.tests)
        block = ((ic * nz + zc) * nt + tc) * n_years + (year - y0)
        key = block * (HOURS + 1) + hour
        order = np.argsort(key, kind="stable")
        ks = key[order].reshape(-1, HOURS)
        if not (hour[order].reshape(-1, HOURS) == np.arange(1, HOURS + 1)).all():
            raise ValueError(f"{self.path.name}: every (iou, climate_zone, cost_test, year) must have hours 1..{HOURS} exactly once")
        bs = ks[:, 0] // (HOURS + 1)
        self.block_year = (bs % n_years + y0).astype(np.int32)
        rest = bs // n_years
        self.block_test = (rest % nt).astype(np.int32)
        rest //= nt
        self.block_zone = (rest % nz).astype(np.int32)
        self.block_iou = (rest // nz).astype(np.int32)
        return order

    def _get_order(self) -> np.ndarray:
        if self._order is None:
            f = self.dir / "order.npy" if self.dir else None
            if f is not None and f.exists():
                try:
                    self._order = np.load(f)
                except (OSError, ValueError):
                    self._order = None
            if self._order is None:
                self._order = self._compute_order()
        return self._order

    # ------------------------------------------------------------------------------------------------------------------------- columns
    def column(self, name: str) -> np.ndarray:
        """`(blocks, 8760)` float64 of one value column (a memory map when the cache is on)."""
        if name in self._columns:
            return self._columns[name]
        f = self.dir / f"col-{name}.npy" if self.dir else None
        arr = None
        if f is not None and f.exists():
            try:
                arr = np.load(f, mmap_mode="r")
            except (OSError, ValueError):
                arr = None
        if arr is None:
            arr = self._build_column(name)
            if f is not None and _atomic_save(f, arr):
                try:
                    arr = np.load(f, mmap_mode="r")
                except (OSError, ValueError):
                    pass
        self._columns[name] = arr
        return arr

    def _read(self, name: str) -> np.ndarray:
        names = pq.ParquetFile(str(self.path)).schema_arrow.names
        if name not in names:
            raise KeyError(f"{self.path.name} has no column {name!r}")
        return np.asarray(pq.read_table(str(self.path), columns=[name])[name].to_numpy()).astype(np.float64, copy=False)

    def _build_column(self, name: str) -> np.ndarray:
        values = self._read("transmission") + self._read("distribution") if name == "td" else self._read(name)
        return np.ascontiguousarray(values[self._get_order()].reshape(-1, HOURS))

    # ------------------------------------------------------------------------------------------------------------------------- queries
    def zones_by_iou(self) -> dict[str, list[str]]:
        out: dict[str, set[str]] = {}
        for iou, zone, _test in self.blocks:
            out.setdefault(iou, set()).add(zone)
        return {iou: sorted(z) for iou, z in out.items()}

    def series(self, iou: str, zone: str, test: str, column: str, divisor: float | None = 1000.0) -> dict[int, np.ndarray]:
        """{year: array(8760)} of `column` for one (iou, zone, test), in years order, as fresh copies divided by `divisor` (1000 turns $/MWh into $/kWh, the value the
        pricing has always used; None leaves the values as stored, e.g. the emission rate). Empty when the ACC has no such block."""
        blocks = self.blocks.get((iou, zone, test))
        if not blocks:
            return {}
        col = self.column(column)
        if divisor is None:
            return {y: np.array(col[blocks[y]], dtype=np.float64) for y in sorted(blocks)}
        return {y: np.array(col[blocks[y]], dtype=np.float64) / divisor for y in sorted(blocks)}
