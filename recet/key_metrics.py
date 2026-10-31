"""The key-metrics summary (BRD BR-06.10): the portfolio's headline results, each with its definition, and the main drivers of the result -- the programs,
program sectors and end uses that carry the most total system benefit and cost.

    sums(frame)      a part's totals by program, sector and end use (a run in parts adds its parts' up: combine)
    build(totals)    the key_metrics table a run writes

Rows of the table (`section`):
  Portfolio        one row per headline metric (TSB, budget, the ratio of each test of the run's cost-test set, the headline tests' costs, first-year
                   savings, lifecycle CO2): `value`, its `definition` and the
                   `documentation` entry it comes from (field_docs).
  Top programs / Top program sectors / Top end uses
                   up to TOP_N rows each, largest share of total system benefit first: the item's total system benefit and its first headline test's
                   cost (TRC), their shares of the portfolio's, and its headline tests' ratios (TRC and PAC; recomputed from its sums, outputs.group_ratios). A program is its PrgID and name; a program sector
                   the program's PrimarySector (Programs list); an end use the measure's UseCategory. Shares are of the portfolio total, so a negative item
                   (a load-increasing fuel substitution, say) shows a negative share; items are ordered by the size of their share.
"""
from __future__ import annotations

import pandas as pd

TOP_N = 10
DIMENSIONS = {"Top programs": ("PrgID", "Prg_ProgramName"), "Top program sectors": ("Prg_PrimarySector",), "Top end uses": ("UseCategory",)}
# the columns summed besides the tests' (sum_columns adds each test's Ben, Cost and CostNoAdmin): TSB, the budget and the headline metrics
BASE_SUM_COLUMNS = ["TotalSystemBenefit", "TotalExpenditures"]
ENERGY_SUM_COLUMNS = ["FirstYearNetKWh", "FirstYearNetKW", "FirstYearNetThm", "LifecycleNetElecCO2", "LifecycleNetGasCO2"]
# the Portfolio rows around the tests' (headlines adds a ratio row per test and a cost row per headline test): (label, column or ratio, documented column)
FIRST_HEADLINES = [("Total system benefit ($)", "TotalSystemBenefit", "TotalSystemBenefit"), ("Budget / expenditures ($)", "TotalExpenditures", "TotalExpenditures")]
LAST_HEADLINES = [("First-year net kWh", "FirstYearNetKWh", "FirstYearNetKWh"), ("First-year net kW", "FirstYearNetKW", "FirstYearNetKW"),
                  ("First-year net therms", "FirstYearNetThm", "FirstYearNetThm"),
                  ("CO2 avoided, lifecycle (metric tons)", "CO2", "LifecycleNetElecCO2")]


def _cset(cset=None):
    from . import outputs
    return outputs._cset(cset)


def sum_columns(cset=None) -> list[str]:
    """The columns sums() adds up for the run's cost-test set: TSB, the budget, each test's Ben / Cost / CostNoAdmin, the first-year savings and CO2."""
    s = _cset(cset)
    tests = [c for t in s.tests.values() for c in (f"{t.id}Ben", f"{t.id}Cost") + ((f"{t.id}CostNoAdmin",) if "NoAdmin" in t.variants else ())]
    return BASE_SUM_COLUMNS + tests + ENERGY_SUM_COLUMNS


def headlines(cset=None) -> list[tuple[str, str, str]]:
    """The Portfolio rows: TSB and the budget, a ratio row per test of the set, a cost row per headline test, then the savings and CO2."""
    from .cost_test_sets import short_label
    s = _cset(cset)
    ratios = [(f"{short_label(t)} ratio", t, f"{t}Ratio") for t in s.tests]
    costs = [(f"{short_label(t)} cost ($)", f"{t}Cost", f"{t}Cost") for t in s.headline_tests()]
    return FIRST_HEADLINES + ratios + costs + LAST_HEADLINES


def columns(cset=None) -> list[str]:
    """The key_metrics table's columns: the drivers carry the first headline test's cost and share and each headline test's ratio."""
    head = _cset(cset).headline_tests()
    return (["section", "item", "measures", "value", "TotalSystemBenefit", "share_of_TSB", f"{head[0]}Cost", f"share_of_{head[0]}_cost"]
            + [f"{t}_ratio" for t in head] + ["definition", "documentation"])


def sums(frame: pd.DataFrame, cset=None) -> pd.DataFrame:
    """The frame's totals of sum_columns(cset) for each item of each dimension, plus the whole portfolio (dimension "Portfolio"), with the measure count.
    Long format: dimension, key, measures, then the sums (a column the run did not write is left out)."""
    cols = [c for c in sum_columns(cset) if c in frame.columns]
    num = frame[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    out = [num.sum().to_frame().T.assign(dimension="Portfolio", key="All measures", measures=len(frame))]
    for dim, keys in DIMENSIONS.items():
        present = [k for k in keys if k in frame.columns]
        if not present:
            continue
        if len(present) > 1:
            # group on the key columns first and build the "PrgID - name" text only for the groups (a few hundred), not row by row: building it per
            # row took 0.9 s of a 50,000-row part. Groups whose texts come out the same are then added together.
            text = frame[present].fillna("").astype(str)
            g = num.assign(measures=1).groupby([text[c].to_numpy() for c in present]).sum()
            label = [" - ".join(v for v in vals if v.strip()) or "(blank)" for vals in g.index]
            g = g.groupby(label).sum()
        else:
            key = frame[present[0]].fillna("").astype(str).str.strip().replace("", "(blank)")
            g = num.assign(measures=1).groupby(key.to_numpy()).sum()
        out.append(g.rename_axis("key").reset_index().assign(dimension=dim))
    df = pd.concat(out, ignore_index=True)
    return df[["dimension", "key", "measures", *cols]]


def combine(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """The sums of a whole run from those of its parts."""
    df = pd.concat(parts, ignore_index=True)
    return df.groupby(["dimension", "key"], sort=False, as_index=False).sum(numeric_only=True)


def build(totals: pd.DataFrame, base_url: str = "", cset=None) -> pd.DataFrame:
    """The key_metrics table from `totals` (sums / combine). `base_url` goes before each documentation link (field_docs.Definition.link). `cset`: the
    run's cost-test set (its tests are the ratio rows; its headline tests the drivers' cost and ratio columns)."""
    from . import field_docs, outputs
    cset = _cset(cset)
    head = cset.headline_tests()
    port = totals[totals["dimension"] == "Portfolio"].iloc[0]

    def get_from(row):
        return lambda c: float(row[c]) if c in row.index and pd.notna(row[c]) else None

    pratios = {name: (None if num is None or not den else num / den) for name, num, den in outputs._ratio_parts(get_from(port), cset)}
    rows = []
    for label, key, doc_col in headlines(cset):
        if key in pratios:
            value = pratios[key]
        elif key == "CO2":
            parts = [port.get(c) for c in ("LifecycleNetElecCO2", "LifecycleNetGasCO2") if c in port.index]
            value = float(sum(parts)) if parts else None
        else:
            value = float(port[key]) if key in port.index else None
        if value is None and key not in pratios:          # a column the run did not write; a ratio whose cost is 0 stays, blank, as in the summary
            continue
        d = field_docs.definition(doc_col)
        rows.append({"section": "Portfolio", "item": label, "measures": int(port["measures"]), "value": value, "definition": d.text if d else "",
                     "documentation": f"{base_url}{d.link}" if d else ""})
    first_cost = f"{head[0]}Cost"
    tsb, trc = float(port.get("TotalSystemBenefit", 0.0) or 0.0), float(port.get(first_cost, 0.0) or 0.0)
    for dim in DIMENSIONS:
        g = totals[totals["dimension"] == dim].copy()
        if g.empty:
            continue
        g["share_of_TSB"] = g["TotalSystemBenefit"] / tsb if tsb else None
        g = g.reindex(g["TotalSystemBenefit"].abs().sort_values(ascending=False, kind="stable").index).head(TOP_N)
        for _, r in g.iterrows():
            ratios = {name: (None if num is None or not den else num / den) for name, num, den in outputs._ratio_parts(get_from(r), cset)}
            rows.append({"section": dim, "item": r["key"], "measures": int(r["measures"]), "TotalSystemBenefit": float(r["TotalSystemBenefit"]),
                         "share_of_TSB": (float(r["TotalSystemBenefit"]) / tsb) if tsb else None,
                         first_cost: float(r.get(first_cost, 0.0) or 0.0),
                         f"share_of_{head[0]}_cost": (float(r.get(first_cost, 0.0) or 0.0) / trc) if trc else None,
                         **{f"{t}_ratio": ratios.get(t) for t in head}})
    return pd.DataFrame(rows, columns=columns(cset))
