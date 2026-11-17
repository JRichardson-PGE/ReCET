"""A Measure.csv priced on a replica part by part (`cet_versions.price`), for jobs too large to hold whole.

The file is split once into parts of at most `size` rows (pipeline.delimited.split_file, as a chunked ReCET run splits it) and
scanned once for what pricing needs before it starts: the job's avoided-cost keys (to read only those rows of the release's
tables), its earliest claim year (RunCET's default FirstYear) and any CEInputID on more than one row. CET groups a CET_ID's
rows together, so such rows are taken out of their parts and priced together in one last part (they come last in the
outputs). Each part is then read when it is priced, in both of `price`'s passes, and again for the measure-level table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import pandas as pd

from . import ids

KEYS = ["PA", "E3MeaElecEndUseShape", "E3ClimateZone"]
PART_SIZE = 10_000       # a replica part's quarterly rows take about 30 MB per 1,000 measures


@dataclass
class Plan:
    files: list[Path]
    rows: int
    keys: pd.DataFrame                         # the job's distinct (PA, end-use shape, climate zone), for QuarterlyTables.from_release
    first_claim_year: int | None
    duplicated: set = field(default_factory=set)   # CEInputIDs on more than one row (None for a blank one)
    acc_version_set: bool = False              # some row fills ReCET's ACCVersion column (a replica prices every row on one Version)
    parent_profile_set: bool = False           # some row names a true-up's parent profile (a ReCET option a replica does not price)

    @property
    def parts(self) -> int:
        return len(self.files) + (1 if self.duplicated else 0)

    def frames(self, read: Callable[[Path], pd.DataFrame]) -> Iterator[pd.DataFrame]:
        """The job's rows part by part, read with `read` (rows of a duplicated CEInputID last, together)."""
        if not self.duplicated:
            for f in self.files:
                yield read(f)
            return
        held = []
        for f in self.files:
            m = read(f)
            dup = ids(m).isin(self.duplicated).to_numpy() | (ids(m).isna().to_numpy() & (None in self.duplicated))
            held.append(m[dup])
            yield m[~dup]
        yield pd.concat(held, ignore_index=True)


def plan(path: str | Path, size: int, read: Callable[[Path], pd.DataFrame], work_dir: str | Path) -> Plan:
    """Split `path` into parts of `size` rows in `work_dir` (one part, the file itself, when it has no more rows than that or
    `size` is 0) and scan it. `read` reads a file into CET's CEDARS input names."""
    from ..pipeline import delimited
    path = Path(path)
    n = delimited.count_rows(path)
    files = [path] if not size or n <= size else [f for f, _start, _n in delimited.split_file(path, size, Path(work_dir))]
    keys, years, seen, acc_set, parent_set = [], [], [], False, False
    for f in files:
        m = read(f)
        if "ACCVersion" in m:
            acc_set = acc_set or bool(m["ACCVersion"].fillna("").astype(str).str.strip().ne("").any())
        for c in ("AC_Elec_Impact_Profile_Parent", "AC_Elec_Impact_Profile_Parent_2nd_Baseline"):
            if c in m:
                parent_set = parent_set or bool(m[c].fillna("").astype(str).str.strip().ne("").any())
        keys.append(m.reindex(columns=KEYS).drop_duplicates())
        y = pd.to_numeric(m["ClaimYearQuarter"].astype("string").str[:4], errors="coerce")
        if y.notna().any():
            years.append(int(y.min()))
        seen.append(ids(m))
        del m
    all_ids = pd.concat(seen, ignore_index=True)
    dup = all_ids[all_ids.duplicated(keep=False)]
    duplicated = set(dup.dropna()) | ({None} if dup.isna().any() else set())
    return Plan(files, n, pd.concat(keys, ignore_index=True).drop_duplicates().reset_index(drop=True), min(years) if years else None,
                duplicated, acc_set, parent_set)
