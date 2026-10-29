"""Replicas of prior CET releases: a job priced exactly as that release's RunCET prices it, on its own quarterly tables.

ReCET's own calculation (hourly, with its documented corrections) is the default and is unchanged by this package. A replica
is for evaluation and for re-running history: it copies each release's errors on purpose (Jake, 2026-10-29), and each copied
error is marked "CET error kept" where it is reproduced. A replica is accepted only when it equals that release's RunCET to
1e-9 on the reference runs (tests/test_cet_versions.py).

    from recet import cet_versions
    out = cet_versions.run(measures, programs, release="25.1", tables=QuarterlyTables.from_folder(...), first_year=2026)
    out.ce, out.savings, out.emissions, out.cost      # SavedCE, SavedSavings, SavedEmissions, SavedCost, CET's columns
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import pandas as pd

from . import calc_ce, calc_ce_2021, calc_cost, calc_emissions, calc_savings, inputs
from .releases import RELEASES, Release, get
from .tables import QuarterlyTables

__all__ = ["RELEASES", "Release", "QuarterlyTables", "ReplicaResult", "run", "output_columns"]

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


def run(measures: pd.DataFrame, programs: pd.DataFrame, release: str, tables: QuarterlyTables, first_year: int,
        me_bens: float = 0.0, me_cost: float = 0.0) -> ReplicaResult:
    """Price a job as CET `release` does: `measures` / `programs` in CET's CEDARS input names (InputMeasureCEDARS /
    InputProgramCEDARS), `tables` the release's quarterly tables for the avoided-cost Version, `first_year` RunCET's
    @FirstYear, `me_bens` / `me_cost` its market effects (0.05 for claims, 0 for filings)."""
    rel = get(release)
    avc = str(tables.version)
    e = inputs.input_measure(measures, rel, first_year, avc)
    s = inputs.settings(tables.settings, avc)
    p = inputs.input_program(programs, rel)
    if rel.calc_ce == "2021":
        ce = calc_ce_2021.calc_ce(e, s, p, tables, rel, first_year, me_bens, me_cost, measures.reset_index(drop=True))
    else:
        ce = calc_ce.calc_ce(e, s, p, tables, rel, first_year, me_bens, me_cost)
    sv = calc_savings.calc_savings(e, rel, me_bens)
    em = calc_emissions.calc_emissions(e, s, tables, rel, avc, me_bens)
    co = calc_cost.calc_cost(e, s, p, ce, rel, me_bens, me_cost)
    return ReplicaResult(rel, _order(ce, rel, "SavedCE"), _order(sv, rel, "SavedSavings"),
                         _order(em, rel, "SavedEmissions"), _order(co, rel, "SavedCost"))
