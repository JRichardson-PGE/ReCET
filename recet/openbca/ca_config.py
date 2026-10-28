"""California's jurisdiction tables for OpenBCA, built from ReCET's ACC files (step 2 of the OpenBCA module, comparison C2).

OpenBCA reads a jurisdiction from its Configuration workbook into four `openbca_input` tables. For California these are written here instead, as
parquet, and the run's staged copy of OpenBCA reads them through small replacement models (`run.stage(..., jurisdiction=)`); OpenBCA's `core/`
is unchanged.

    avoided_costs_ts        the ACC's hourly electric avoided cost, $/kWh, one value stream (`ELECTRIC_STREAM`, the ACC's `total_avoided_cost`, which
                            is the sum of its components), one subset per IOU and climate zone ("PG&E CZ12"), only the subsets and years asked for
    value_stream_groups     that stream (electric, hourly) plus OpenBCA's cost and program rows, as its parser writes them
    global_parameters       nominal dollars (inflation 0; the ACC is nominal), quarterly discounting, line losses 0 (the ACC already includes losses),
                            the cost treatment ("CA TRC" or "USI" for PAC); a discount rate (a PA's own goes on each measure instead)
    first_year_avoided_costs_by_id   empty (no NEIs in a CET job)

Gas is not built yet: the first comparisons (C3) are electric only. The ACC's hours are numbered 1..8760 in ReCET's files and 0..8759 in OpenBCA's.

OpenBCA rounds every avoided cost to $0.001/kWh: core_layer0_base.avoided_costs_ts casts the value `::NUMERIC`, which DuckDB reads as DECIMAL(18,3).
Annual sums move by up to 0.03% on the 2024 ACC (C2, 2026-10-28). That is OpenBCA's own behaviour and is left as it is (its outputs are reported
as they stand); `check_against_acc` checks against the ACC rounded the same way.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ELECTRIC_STREAM = "CA ACC Electric (E)"
# OpenBCA's own rows for costs and program-level values, as its value-stream parser writes them (excel_input_parsing/models/
# openbca_input_value_stream_groups.py: config_measure_cost_fields_map_dict and repeating_annual_costs), every one in the test.
COST_ROWS = [
    ("admin_cost_upfront_dollar", "ADMIN", "first_year"),
    ("admin_cost_annual_dollar_per_year", "ADMIN", "annual"),
    ("program_admin_costs_dollar", "ADMIN", "first_year"),
    ("utility_direct_investment_in_ders_upfront_dollar", "ADMIN", "first_year"),
    ("utility_incentive_upfront_dollar", "UTILITY INCENTIVE", "first_year"),
    ("utility_incentive_annual_dollar_per_year", "UTILITY INCENTIVE", "annual"),
    ("program_utility_incentive_dollar", "UTILITY INCENTIVE", "first_year"),
    ("incremental_cost_upfront_dollar", "MEASURE COST", "first_year"),
    ("incremental_cost_annual_dollar_per_year", "MEASURE COST", "annual"),
    ("host_customer_transaction_cost_upfront_dollar", "MEASURE COST", "first_year"),
    ("host_customer_interconnection_cost_upfront_dollar", "MEASURE COST", "first_year"),
    ("host_customer_tax_incentive_upfront_dollar", "TAX INCENTIVE", "first_year"),
    ("program_performance_incentive_to_utility_dollar", "NON-SYSTEM", "first_year"),
    ("program_federal_incentive_dollar", "NON-SYSTEM", "first_year"),
]
COST_TREATMENTS = {"TRC": "CA TRC", "PAC": "USI"}
TABLES = ("avoided_costs_ts", "value_stream_groups", "global_parameters", "first_year_avoided_costs_by_id")


def subset_name(iou: str, climate_zone: str) -> str:
    return f"{iou} {climate_zone}"


def avoided_costs_ts(acc_file: str | Path, subsets: Iterable[tuple[str, str]], years: tuple[int, int], cost_test: str = "TRC") -> pd.DataFrame:
    """The ACC's hourly total for each (iou, climate zone) in `subsets` and each year in [years[0], years[1]], as OpenBCA's avoided_costs_ts ($/kWh)."""
    wanted = sorted(set(subsets))
    if not wanted:
        raise ValueError("no subsets asked for")
    t = pq.read_table(acc_file, columns=["iou", "climate_zone", "cost_test", "year", "hour_of_year", "month", "hour", "total_avoided_cost"],
                      filters=[("cost_test", "=", cost_test), ("year", ">=", years[0]), ("year", "<=", years[1]),
                               ("iou", "in", sorted({i for i, _ in wanted})), ("climate_zone", "in", sorted({z for _, z in wanted}))])
    df = t.to_pandas()
    for c in ("iou", "climate_zone"):
        df[c] = df[c].astype(str)
    df = df[pd.MultiIndex.from_frame(df[["iou", "climate_zone"]]).isin(wanted)]
    missing = sorted(set(wanted) - set(map(tuple, df[["iou", "climate_zone"]].drop_duplicates().to_numpy())))
    if missing:
        raise ValueError(f"{Path(acc_file).name} has no {cost_test} rows for {missing}")
    hours = df.groupby(["iou", "climate_zone", "year"]).size()
    if not (hours == 8760).all():
        raise ValueError(f"{Path(acc_file).name}: a year without 8,760 hours: {hours[hours != 8760].head().to_dict()}")
    out = pd.DataFrame({
        "avoided_cost": ELECTRIC_STREAM,
        "avoided_cost_subset": [subset_name(i, z) for i, z in zip(df["iou"], df["climate_zone"])],
        "year": df["year"].astype("int32").to_numpy(),
        "quarter": pd.array([pd.NA] * len(df), dtype="Int32"),
        "month": df["month"].astype("int32").to_numpy(),
        "day_of_year": pd.array([pd.NA] * len(df), dtype="Int32"),
        "hour_of_day": (df["hour"].astype("int32") + 1).to_numpy(),   # OpenBCA's hour of day runs 1..24; the ACC file's hour 0..23
        "hour_of_year": (df["hour_of_year"].astype("int32") - 1).to_numpy(),
        "avoided_cost_value": (df["total_avoided_cost"].to_numpy(np.float64) / 1000.0),   # $/MWh -> $/kWh, as ReCET's AccStore.series
    })
    return out.sort_values(["avoided_cost_subset", "year", "hour_of_year"], kind="stable").reset_index(drop=True)


def value_stream_groups() -> pd.DataFrame:
    rows = [(ELECTRIC_STREAM, "Electric", True, "Time Series - Hourly", np.nan, "electric", False)]
    rows += [(name, cat, True, "Single Value - First Year" if grp == "first_year" else "Time Series - Annual", np.nan, grp, False)
             for name, cat, grp in COST_ROWS]
    return pd.DataFrame(rows, columns=["avoided_cost", "impact_category", "include_in_test", "calc_type", "pct_adder", "value_stream_group", "marginal_ghg"])


def global_parameters(test: str, discount_rate: float, dollar_year: int) -> pd.DataFrame:
    if test not in COST_TREATMENTS:
        raise ValueError(f"test must be one of {sorted(COST_TREATMENTS)}")
    return pd.DataFrame([{"inflation_rate": 0.0, "dollar_year": int(dollar_year), "discount_rate": float(discount_rate), "discount_cadence": 4,
                          "electric_line_loss": 0.0, "peak_capacity_line_loss": 0.0, "natural_gas_line_loss": 0.0,
                          "cost_treatment": COST_TREATMENTS[test]}])


def first_year_avoided_costs_by_id() -> pd.DataFrame:
    return pd.DataFrame({"id": pd.Series(dtype=str), "year": pd.Series(dtype="int32"), "value_stream": pd.Series(dtype=str),
                         "gross_dollar_value": pd.Series(dtype="float64")})


def build(acc_file: str | Path, subsets: Iterable[tuple[str, str]], years: tuple[int, int], test: str, discount_rate: float,
          out_dir: str | Path) -> dict[str, Path]:
    """Write the four jurisdiction tables as <out_dir>/<table>.parquet. Returns the files by table."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frames = {"avoided_costs_ts": avoided_costs_ts(acc_file, subsets, years), "value_stream_groups": value_stream_groups(),
              "global_parameters": global_parameters(test, discount_rate, years[0]), "first_year_avoided_costs_by_id": first_year_avoided_costs_by_id()}
    files = {}
    for name, df in frames.items():
        files[name] = out / f"{name}.parquet"
        df.to_parquet(files[name], index=False)
    return files


def check_against_acc(acc_file: str | Path, avoided: pd.DataFrame, cost_test: str = "TRC") -> pd.DataFrame:
    """Comparison C2: each (subset, year)'s annual sum in `avoided` (OpenBCA's core_layer0_base.avoided_costs_ts, as read back from its database)
    against the ACC file's own total_avoided_cost / 1000, both as is (`acc`) and with each hour rounded half-up to $0.001/kWh as OpenBCA's NUMERIC cast
    does (`acc_rounded`). Returns one row per (subset, year) with the sums and `difference` = openbca - acc_rounded."""
    t = pq.read_table(acc_file, columns=["iou", "climate_zone", "cost_test", "year", "total_avoided_cost"], filters=[("cost_test", "=", cost_test)])
    acc = t.to_pandas()
    acc["avoided_cost_subset"] = [subset_name(str(i), str(z)) for i, z in zip(acc["iou"], acc["climate_zone"])]
    acc["value"] = acc["total_avoided_cost"].to_numpy(np.float64) / 1000.0
    acc["rounded"] = rounded_like_openbca(acc["value"].to_numpy())
    acc = acc.groupby(["avoided_cost_subset", "year"], observed=True)[["value", "rounded"]].sum().rename(columns={"value": "acc", "rounded": "acc_rounded"})
    got = avoided[avoided["avoided_cost"] == ELECTRIC_STREAM].groupby(["avoided_cost_subset", "year"])["avoided_cost_value"].sum().rename("openbca")
    both = pd.concat([acc, got.astype(float)], axis=1, join="inner").reset_index()
    both["difference"] = both["openbca"] - both["acc_rounded"]
    return both


def rounded_like_openbca(values: np.ndarray) -> np.ndarray:
    """$/kWh values rounded half away from zero to 3 decimals, as DuckDB's cast to DECIMAL(18,3)."""
    v = np.asarray(values, dtype=np.float64)
    return np.sign(v) * np.floor(np.abs(v) * 1000.0 + 0.5) / 1000.0
