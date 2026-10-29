"""Replicas of prior CET releases: a job priced exactly as that release's RunCET prices it, on its own quarterly tables.

ReCET's own calculation (hourly, with its documented corrections) is the default and is unchanged by this package. A replica
is for evaluation and for re-running history: it copies each release's errors on purpose (Jake, 2026-10-29), and each copied
error is marked "CET error kept" where it is reproduced. A replica is accepted only when it equals that release's RunCET to
1e-9 on the reference runs (tests/test_cet_versions.py).

    from recet import cet_versions
    out = cet_versions.run(measures, programs, release="25.1", tables=QuarterlyTables.from_folder(...), first_year=2026)
    out.ce, out.savings, out.emissions, out.cost      # SavedCE, SavedSavings, SavedEmissions, SavedCost, CET's columns

A large job is priced in parts (`price`), so its memory stays about that of one part plus the release's tables: a measure's
quarters and years (benefits, bill terms, emissions) are expanded only for the part it is in, and the few program-level
sums CET takes over the whole job (each program's positive benefits and claim count, the savings weights' totals) are
added up in a first pass over the parts and applied in a second. `run` is `price` over an in-memory job.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable, Iterator

import pandas as pd

from . import calc_ce, calc_ce_2021, calc_cost, calc_emissions, calc_savings, inputs
from .releases import RELEASES, Release, get
from .tables import QuarterlyTables

__all__ = ["RELEASES", "Release", "QuarterlyTables", "ReplicaResult", "run", "price", "output_columns"]

TABLES = {"ce": "SavedCE", "savings": "SavedSavings", "emissions": "SavedEmissions", "cost": "SavedCost"}


@dataclass
class ReplicaResult:
    release: Release
    ce: pd.DataFrame
    savings: pd.DataFrame
    emissions: pd.DataFrame
    cost: pd.DataFrame

    def tables(self) -> dict[str, pd.DataFrame]:
        return {name: getattr(self, attr) for attr, name in TABLES.items()}


@lru_cache(maxsize=None)
def _columns() -> pd.DataFrame:
    with resources.files("recet.data").joinpath("cet_versions/output_columns.csv").open(encoding="utf-8") as f:
        return pd.read_csv(f, dtype={"release": str})


def output_columns(release: str, table: str) -> list[str]:
    """The release's SavedCE / SavedSavings / SavedEmissions / SavedCost columns (less ID and JobID), in CET's order."""
    c = _columns()
    c = c[(c.release == release) & (c.table == table)].sort_values("ordinal")
    return list(c.column)


def _order(df: pd.DataFrame, release: Release, table: str) -> pd.DataFrame:
    cols = output_columns(release.name, table)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise RuntimeError(f"CET {release.name} {table}: the replica did not produce {missing}")
    df = df[cols].sort_values("CET_ID", kind="stable").reset_index(drop=True)
    for t, c in release.saved_blank:
        if t == table:
            df[c] = float("nan")
    return df


CHUNK = 20_000           # measures per part for `run`; a part's quarterly rows take about 30 MB per 1,000 measures


def ids(raw: pd.DataFrame) -> pd.Series:
    """The CET_ID each Measure row gets (CEInputID; blank is NULL)."""
    s = raw["CEInputID"]
    return s.where(s.notna() & (s.astype(str) != ""), None)


def _chunks(raw: pd.DataFrame, size: int) -> list[pd.Index]:
    """Row positions of `raw` in parts of about `size` measures. Every row of a CET_ID is in the same part (CET groups and
    deduplicates per CET_ID), and parts follow the CET_IDs' first appearance, so the outputs keep their order."""
    codes, _ = pd.factorize(ids(raw), use_na_sentinel=False)
    group = codes // max(int(size), 1)
    if group.max(initial=0) == 0:
        return [raw.index]
    return [raw.index[group == g] for g in range(group.max() + 1) if (group == g).any()]


def price(chunks: Callable[[], Iterable[pd.DataFrame]], programs: pd.DataFrame, release: str, tables: QuarterlyTables,
          first_year: int, me_bens: float = 0.0, me_cost: float = 0.0, spill: str | Path | None = None
          ) -> Iterator[tuple[pd.DataFrame, ReplicaResult]]:
    """Price a job part by part. `chunks()` gives the job's Measure rows (CET's CEDARS input names) as DataFrames, the same
    parts in the same order each time it is called (it is called twice), with every row of a CEInputID in one part. Yields
    each part's rows and its four tables (CET's columns, in CET_ID order within the part).

    Pass 1 prices each part's benefits and adds up the program-level sums; pass 2 finishes each part with the whole job's
    sums (program-cost shares, savings weights) and yields it. Between the passes the benefits of a job of more than one
    part are kept as parquet in `spill` (default: a temporary folder, deleted at the end), not in memory."""
    rel = get(release)
    avc = str(tables.version)
    s = inputs.settings(tables.settings, avc)
    p = inputs.input_program(programs, rel)
    is21 = rel.calc_ce == "2021"
    folder = None
    held: list = []                       # each part's benefits: a DataFrame while there is one part, else a parquet path
    ce_sums, sv_sums = [], []
    try:
        for i, raw in enumerate(chunks()):
            raw = raw.reset_index(drop=True)
            e = inputs.input_measure(raw, rel, first_year, avc)
            if is21:
                ce = calc_ce_2021.benefits(e, s, tables, raw, first_year, me_bens)
                ce_sums.append(calc_ce_2021.program_sums(ce))
            else:
                ce = calc_ce.benefits(e, s, tables, rel, first_year, me_bens, me_cost)[0]
                ce_sums.append(calc_ce.program_sums(ce))
            sv_sums.append(calc_savings.program_sums(calc_savings.calc_savings(e, rel, me_bens), rel))
            if i == 1:                    # a second part: spill from here on, the first part too
                folder = Path(spill) if spill else Path(tempfile.mkdtemp(prefix="recet_cet_"))
                folder.mkdir(parents=True, exist_ok=True)
                held[0].to_parquet(folder / "ce_0000.parquet", index=False)
                held[0] = folder / "ce_0000.parquet"
            if folder is not None:
                ce.to_parquet(folder / f"ce_{i:04d}.parquet", index=False)
                held.append(folder / f"ce_{i:04d}.parquet")
            else:
                held.append(ce)
            del raw, e, ce
        ce_tot, sv_tot = calc_ce.combine_sums(ce_sums), calc_ce.combine_sums(sv_sums)
        for i, raw in enumerate(chunks()):
            raw = raw.reset_index(drop=True)
            e = inputs.input_measure(raw, rel, first_year, avc)
            ce = held[i] if isinstance(held[i], pd.DataFrame) else pd.read_parquet(held[i])
            held[i] = None
            if is21:
                ce = calc_ce_2021.ratios(calc_ce.bills(calc_ce_2021.costs(ce, e, s, p, first_year, me_cost, ce_tot), e, s, tables, rel, me_bens))
            else:
                ce = calc_ce.ratios(calc_ce.bills(calc_ce.costs(ce, e, s, p, rel, first_year, me_cost, ce_tot), e, s, tables, rel, me_bens), rel)
            sv = calc_savings.calc_savings(e, rel, me_bens, sv_tot)
            em = calc_emissions.calc_emissions(e, s, tables, rel, avc, me_bens)
            co = calc_cost.calc_cost(e, s, p, ce, rel, me_bens, me_cost)
            yield raw, ReplicaResult(rel, _order(ce, rel, "SavedCE"), _order(sv, rel, "SavedSavings"),
                                     _order(em, rel, "SavedEmissions"), _order(co, rel, "SavedCost"))
    finally:
        if folder is not None and not spill:
            shutil.rmtree(folder, ignore_errors=True)


def run(measures: pd.DataFrame, programs: pd.DataFrame, release: str, tables: QuarterlyTables, first_year: int,
        me_bens: float = 0.0, me_cost: float = 0.0, chunk_size: int = CHUNK) -> ReplicaResult:
    """Price a job as CET `release` does: `measures` / `programs` in CET's CEDARS input names (InputMeasureCEDARS /
    InputProgramCEDARS), `tables` the release's quarterly tables for the avoided-cost Version, `first_year` RunCET's
    @FirstYear, `me_bens` / `me_cost` its market effects (0.05 for claims, 0 for filings). Priced `chunk_size` measures
    at a time (`price`); the tables are in CET_ID order."""
    raw = measures.reset_index(drop=True)
    parts = _chunks(raw, chunk_size)
    out: dict[str, list[pd.DataFrame]] = {k: [] for k in TABLES}
    for _, r in price(lambda: (raw.loc[ix] for ix in parts), programs, release, tables, first_year, me_bens, me_cost):
        for k in TABLES:
            out[k].append(getattr(r, k))

    def whole(k):
        df = out[k][0] if len(out[k]) == 1 else pd.concat(out[k], ignore_index=True)
        return df.sort_values("CET_ID", kind="stable").reset_index(drop=True) if len(out[k]) > 1 else df

    return ReplicaResult(get(release), whole("ce"), whole("savings"), whole("emissions"), whole("cost"))
