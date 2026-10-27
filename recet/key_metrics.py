"""The key-metrics summary (BRD BR-06.10): the portfolio's headline results, each with its definition, and the main drivers of the result -- the programs,
program sectors and end uses that carry the most total system benefit and cost.

    sums(frame)      a part's totals by program, sector and end use (a run in parts adds its parts' up: combine)
    build(totals)    the key_metrics table a run writes

Rows of the table (`section`):
  Portfolio        one row per headline metric (TSB, budget, the cost-test ratios, first-year savings, lifecycle CO2): `value`, its `definition` and the
                   `documentation` entry it comes from (field_docs).
  Top programs / Top program sectors / Top end uses
                   up to TOP_N rows each, largest share of total system benefit first: the item's total system benefit and TRC cost, their shares of the
                   portfolio's, and its TRC and PAC ratios (recomputed from its sums, outputs.group_ratios). A program is its PrgID and name; a program sector
                   the program's PrimarySector (Programs list); an end use the measure's UseCategory. Shares are of the portfolio total, so a negative item
                   (a load-increasing fuel substitution, say) shows a negative share; items are ordered by the size of their share.
"""
from __future__ import annotations

import pandas as pd

TOP_N = 10
DIMENSIONS = {"Top programs": ("PrgID", "Prg_ProgramName"), "Top program sectors": ("Prg_PrimarySector",), "Top end uses": ("UseCategory",)}
# the columns summed: what TSB, the TRC / PAC ratios (outputs.group_ratios) and the headline metrics need
SUM_COLUMNS = ["TotalSystemBenefit", "TotalExpenditures", "ElecBen", "GasBen", "WaterEnergyBen", "OtherBen", "TaxCredits", "LifecycleBillIncrElec", "LifecycleBillIncrGas",
               "ElecBen_SB", "GasBen_SB", "WaterEnergyBen_SB", "ElecBen_SH", "GasBen_SH", "WaterEnergyBen_SH",
               "TRCCost", "PACCost", "RIMCost", "SCBCost", "SCHCost", "TRCCostNoAdmin", "PACCostNoAdmin", "RIMCostNoAdmin", "SCBCostNoAdmin", "SCHCostNoAdmin",
               "FirstYearNetKWh", "FirstYearNetKW", "FirstYearNetThm", "LifecycleNetElecCO2", "LifecycleNetGasCO2"]
# the Portfolio rows: (label, column or ratio, documented column the definition comes from)
HEADLINES = [("Total system benefit ($)", "TotalSystemBenefit", "TotalSystemBenefit"), ("Budget / expenditures ($)", "TotalExpenditures", "TotalExpenditures"),
             ("TRC ratio", "TRC", "TRCRatio"), ("PAC ratio", "PAC", "PACRatio"), ("RIM ratio", "RIM", "RIMRatio"),
             ("SCT Base ratio", "SCB", "SCBRatio"), ("SCT High ratio", "SCH", "SCHRatio"),
             ("TRC cost ($)", "TRCCost", "TRCCost"), ("PAC cost ($)", "PACCost", "PACCost"),
             ("First-year net kWh", "FirstYearNetKWh", "FirstYearNetKWh"), ("First-year net kW", "FirstYearNetKW", "FirstYearNetKW"),
             ("First-year net therms", "FirstYearNetThm", "FirstYearNetThm"),
             ("CO2 avoided, lifecycle (metric tons)", "CO2", "LifecycleNetElecCO2")]
COLUMNS = ["section", "item", "measures", "value", "TotalSystemBenefit", "share_of_TSB", "TRCCost", "share_of_TRC_cost", "TRC_ratio", "PAC_ratio",
           "definition", "documentation"]


def sums(frame: pd.DataFrame) -> pd.DataFrame:
    """The frame's totals of SUM_COLUMNS for each item of each dimension, plus the whole portfolio (dimension "Portfolio"), with the measure count. Long
    format: dimension, key, measures, then the sums (a column the run did not write is left out)."""
    cols = [c for c in SUM_COLUMNS if c in frame.columns]
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


def build(totals: pd.DataFrame, base_url: str = "") -> pd.DataFrame:
    """The key_metrics table from `totals` (sums / combine). `base_url` goes before each documentation link (field_docs.Definition.link)."""
    from . import field_docs, outputs
    port = totals[totals["dimension"] == "Portfolio"].iloc[0]

    def get_from(row):
        return lambda c: float(row[c]) if c in row.index and pd.notna(row[c]) else None

    pratios = {name: (None if num is None or not den else num / den) for name, num, den in outputs._ratio_parts(get_from(port))}
    rows = []
    for label, key, doc_col in HEADLINES:
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
    tsb, trc = float(port.get("TotalSystemBenefit", 0.0) or 0.0), float(port.get("TRCCost", 0.0) or 0.0)
    for dim in DIMENSIONS:
        g = totals[totals["dimension"] == dim].copy()
        if g.empty:
            continue
        g["share_of_TSB"] = g["TotalSystemBenefit"] / tsb if tsb else None
        g = g.reindex(g["TotalSystemBenefit"].abs().sort_values(ascending=False, kind="stable").index).head(TOP_N)
        for _, r in g.iterrows():
            ratios = {name: (None if num is None or not den else num / den) for name, num, den in outputs._ratio_parts(get_from(r))}
            rows.append({"section": dim, "item": r["key"], "measures": int(r["measures"]), "TotalSystemBenefit": float(r["TotalSystemBenefit"]),
                         "share_of_TSB": (float(r["TotalSystemBenefit"]) / tsb) if tsb else None,
                         "TRCCost": float(r.get("TRCCost", 0.0) or 0.0), "share_of_TRC_cost": (float(r.get("TRCCost", 0.0) or 0.0) / trc) if trc else None,
                         "TRC_ratio": ratios.get("TRC"), "PAC_ratio": ratios.get("PAC")})
    return pd.DataFrame(rows, columns=COLUMNS)
