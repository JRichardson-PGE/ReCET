"""ACC components priced separately ("calculate ACC components separately").

Every electric and gas benefit is a sum over the avoided-cost components in the ACC files. This prices each component on its own, through exactly the same
routes as the totals (the 8,760 hourly profile; gas monthly profile), so the columns add up to the total:

  ElecValue_<component>   for every component column in the electric ACC (2024 ACC: energy, generation_capacity, avoided_as_procurement, losses,
                          methane_leakage, ghg_cap_and_trade, ghg_adder, ghg_portfolio_rebalancing, air_quality_adder, transmission, distribution;
                          2026 ACC: ghg_cap_and_invest instead of cap_and_trade / portfolio_rebalancing)
  GasValue_<component>    market, td, nox, co2, upstream_methane, behind_meter_methane, air_quality_adder (each one the loaded gas ACC has)

Values are the discounted lifetime $ of TRC pricing, SIGNED, before the Ben / SupplyCost split:  sum(ElecValue_*) = ElecBen - ElecSupplyCost  and
sum(GasValue_*) = GasBen - GasSupplyCost.  (The split books a negative value of a fuel-substitution measure as a supply cost; a single component of such a measure
can be negative while the total is positive, so components are reported before that split so they always add up.)

TRC pricing only.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from . import run_poc
from .pipeline import gas_avoided_cost, real_avoided_cost_v2

GAS_COMPONENTS = gas_avoided_cost.ALL_COMPONENTS


def price_components(measures: pd.DataFrame, *, acc_version: int, base_year: int, market_effects_bens: float = 0.0,
                     log: Callable[[str], None] = print) -> pd.DataFrame:
    """One row per measure: CEInputID plus ElecValue_<c> for each electric component and GasValue_<c> for each gas component."""
    out = pd.DataFrame({"CEInputID": measures["CEInputID"].to_numpy()}).set_index("CEInputID")
    elec = real_avoided_cost_v2.available_components()
    for i, c in enumerate(elec, 1):
        log(f"  electric component {i}/{len(elec)}: {c}")
        priced = run_poc.run_real_hourly(measures, cost_test="TRC", acc_version=acc_version, base_year=base_year,
                                         market_effects_bens=market_effects_bens, component=c)
        s = priced.values.groupby("CEInputID")["LifetimeValuePV"].sum() if len(priced.values) else pd.Series(dtype=float)
        out[f"ElecValue_{c}"] = s.reindex(out.index).fillna(0.0)
    gas = gas_avoided_cost.available_components()          # the 2021/2022 Gas ACCs have no air quality adder: no column for it
    for i, c in enumerate(gas, 1):
        log(f"  gas component {i}/{len(gas)}: {c}")
        g, _n, _m = run_poc.run_gas_benefit(measures, cost_test="TRC", acc_version=acc_version, base_year=base_year,
                                            market_effects_bens=market_effects_bens, component=c)
        s = g.groupby("CEInputID")["LifetimeValuePV"].sum() if len(g) else pd.Series(dtype=float)
        out[f"GasValue_{c}"] = s.reindex(out.index).fillna(0.0)
    return out.reset_index()


def check_sums(result: pd.DataFrame, comps: pd.DataFrame, tolerance: float = 1e-6) -> dict:
    """Compare the components against the totals in `result` (ElecBen - ElecSupplyCost, GasBen - GasSupplyCost). Returns the worst absolute and relative
    residual per fuel and the number of measures beyond `tolerance` (relative to the portfolio's largest measure value)."""
    j = result[["CEInputID", "ElecBen", "ElecSupplyCost", "GasBen", "GasSupplyCost"]].merge(comps, on="CEInputID")
    report = {}
    for fuel, ben, sup in (("Elec", "ElecBen", "ElecSupplyCost"), ("Gas", "GasBen", "GasSupplyCost")):
        cols = [c for c in comps.columns if c.startswith(f"{fuel}Value_")]
        total = j[ben] - j[sup]
        resid = (total - j[cols].sum(axis=1)).abs()
        scale = max(float(total.abs().max()), 1.0)
        report[fuel] = {"components": len(cols), "max_abs_residual": float(resid.max()), "max_rel_to_largest": float(resid.max()) / scale,
                        "measures_over_tolerance": int((resid > tolerance * scale).sum()),
                        "portfolio_total": float(total.sum()), "portfolio_sum_of_components": float(j[cols].sum().sum())}
    return report


def portfolio_table(comps: pd.DataFrame) -> pd.DataFrame:
    """Portfolio total per component (one row per component), with its share of its fuel's total."""
    rows = []
    for c in comps.columns:
        if c.startswith(("ElecValue_", "GasValue_")):
            fuel = "Electric" if c.startswith("Elec") else "Gas"
            rows.append({"fuel": fuel, "component": c.split("_", 1)[1], "portfolio_value_usd": float(comps[c].sum())})
    df = pd.DataFrame(rows)
    df["share_of_fuel_total"] = df["portfolio_value_usd"] / df.groupby("fuel")["portfolio_value_usd"].transform("sum")
    return df
