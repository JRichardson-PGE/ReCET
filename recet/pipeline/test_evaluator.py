"""The cost tests priced from a definition set (recet/cost_test_sets.py, recet/docs/COST_TESTS.md): one evaluator for every test and aggregate a set
defines, in place of the test arithmetic that used to be written out in cost_tests.compute_cost_tests (kept, frozen, as tests/reference_cost_tests.py;
on EE-2026 every column equals it to 1e-9).

What it does, for a frame that already holds the priced streams (the engine's columns, their gross forms, cost_tests._with_gross) and each measure's
program sums (`_SumCostsNPV`, `_SumCostsNPV_SCT`):

  share  per test with `program_costs: {share: positive_benefits}`: the positive parts of the test's own benefits in its pricing, over the program's
         (1 / the program's measure count when they sum to 0; `totals` gives the whole job's when the frame is one part of it), and the same on the
         gross forms when the test has a Gross variant. `share: <test>` reuses that test's.
  <T>Ben          = the benefits + the numerator adders, in the test's pricing
  <T>CostNoAdmin  = the costs                                   (with the NoAdmin variant)
  <T>Cost         = share x the program costs at the pricing's annual rate + the costs
  <T>CostGross    = gross share x the program costs + the costs' gross forms           (with the Gross variant)
  <T>Ratio        = ratio(<T>Ben, <T>Cost)        <T>RatioNoAdmin = ratio(<T>Ben, <T>CostNoAdmin)        ratio(n, d) = n / d, 0 when d = 0
  <T>LifecycleNetBen = ElecBen + GasBen - <T>Cost                                                   (lifecycle_net_benefit; the test's pricing)
  Lev<T>Cost<v> = ratio(<T>Cost<v> x WeightedElecAlloc, DiscountedSavingsNetkWh), LevNetBen<T>Elec<v> = ratio(ElecBen - <T>Cost<v> x WeightedElecAlloc,
  DiscountedSavingsNetkWh), LevNetBen<T>Gas<v> = ratio(GasBen - <T>Cost<v> x (1 - WeightedElecAlloc), DiscountedSavingsNetThm)          (levelized)
  aggregate = the plus streams - the minus streams (and its Gross form on the gross streams)

The program-cost NPV, the share mechanics, the Weighted* columns and the pricing of every stream stay in cost_tests.py and the engine."""
from __future__ import annotations

import numpy as np
import pandas as pd

# the program-cost sums (cost_tests.program_cost_sums) a pricing's annual rate selects
PROGRAM_SUMS = {"DiscountRateAnnual": "_SumCostsNPV", "DiscountRateAnnual_SCT": "_SumCostsNPV_SCT"}
NTG_OF_FUEL = {"elec": "NTGRkWh_plus_MEBens", "water": "NTGRkWh_plus_MEBens", "gas": "NTGRTherm_plus_MEBens"}


def ratio(n: pd.Series, d: pd.Series) -> pd.Series:
    return (n / d.replace(0, np.nan)).fillna(0.0) + 0.0          # + 0.0: a zero over a negative denominator is 0, not -0


def _pos(s: pd.Series) -> pd.Series:
    return s.where(s > 0, 0.0)


def _sum(d: pd.DataFrame, cols: list[str]) -> pd.Series:
    out = None
    for c in cols:
        out = d[c] if out is None else out + d[c]
    return out if out is not None else pd.Series(0.0, index=d.index)


def share_bases(cset) -> dict[str, list[str]]:
    """{share key: the columns whose positive parts are summed}: `<T>` for each test that computes its own share, `<T>Gross` for its Gross variant."""
    out = {}
    for t in cset.tests.values():
        if t.share != "positive_benefits":
            continue
        out[t.id] = [cset.column(s, t.pricing) for s in t.benefits]
        if "Gross" in t.variants:
            out[f"{t.id}Gross"] = [cset.column(s, t.pricing, gross=True) for s in t.benefits]
    return out


def share_key(cset, test, gross: bool = False) -> str | None:
    """The share a test's program costs are divided by (None: the test has no program costs)."""
    if test.share is None:
        return None
    own = test.id if test.share == "positive_benefits" else test.share
    return f"{own}Gross" if gross else own


def headline_share(cset) -> str:
    """The share CET's program-cost components (WeightedBenefits, Wtd<field>, TotalExpenditures) are written on: the first headline test that computes
    its own share (TRC in EE-2026), else the first test that does."""
    own = [t for t in cset.tests.values() if t.share == "positive_benefits"]
    if not own:
        raise ValueError(f"cost-test set {cset.id} has no test that shares the program costs")
    return next((t.id for t in own if t.headline), own[0].id)


def add_gross_forms(d: pd.DataFrame, cset) -> pd.DataFrame:
    """The gross columns the set reads that the engine has not written (a divide_ntg stream: the net value over its fuel's NTG + market effects, C10).
    cost_tests._with_gross writes EE-2026's; this covers a stream a new set adds."""
    new = {}
    for s in cset.streams.values():
        if s.source != "engine" or s.gross != "divide_ntg" or s.fuel not in NTG_OF_FUEL:
            continue
        for p in s.pricings:
            g = cset.column(s.name, p, gross=True)
            if g not in d and cset.column(s.name, p) in d:
                new[g] = (d[cset.column(s.name, p)] / d[NTG_OF_FUEL[s.fuel]].replace(0, np.nan)).fillna(0.0)
    return d.assign(**new) if new else d


def share_inputs(d: pd.DataFrame, cset) -> pd.DataFrame:
    """Per measure: the positive benefits each share of the set divides up (`d` with its gross forms)."""
    return pd.DataFrame({k: sum(_pos(d[c]) for c in cols) for k, cols in share_bases(cset).items()}, index=d.index)


def shares(d: pd.DataFrame, cset, PrgID: pd.Series, totals: dict[str, pd.DataFrame] | None = None) -> dict[str, pd.Series]:
    """{share key: each measure's share of its program's costs}."""
    from .cost_tests import _share
    return {k: _share(sum(_pos(d[c]) for c in cols), PrgID, None if totals is None else totals[k]) for k, cols in share_bases(cset).items()}


def evaluate(d: pd.DataFrame, cset, share: dict[str, pd.Series], weighted_elec_alloc: pd.Series | None = None) -> dict[str, pd.Series]:
    """Every aggregate and test column of `cset`, in the set's order: {column: values}. `d` holds the priced streams, their gross forms, the program
    sums (`_SumCostsNPV`, `_SumCostsNPV_SCT`) and, for the levelized columns, DiscountedSavingsNetkWh / Thm; `share` is `shares(...)`;
    `weighted_elec_alloc` is CalcCost's WeightedElecAlloc (cost_tests.py)."""
    out: dict[str, pd.Series] = {}
    for a in cset.aggregates.values():
        out[a.name] = _sum(d, [cset.column(s, a.pricing) for s in a.plus]) - _sum(d, [cset.column(s, a.pricing) for s in a.minus])
        if a.gross:
            out[f"{a.name}Gross"] = (_sum(d, [cset.column(s, a.pricing, gross=True) for s in a.plus])
                                     - _sum(d, [cset.column(s, a.pricing, gross=True) for s in a.minus]))
    for t in cset.tests.values():
        p = cset.pricings[t.pricing]
        cols = lambda names, gross=False: [cset.column(s, t.pricing, gross=gross) for s in names]
        npv = d[PROGRAM_SUMS[p.discount_rate_annual]] if t.share is not None else None
        ben = _sum(d, cols(t.benefits) + cols(t.numerator_adders))
        no_admin = _sum(d, cols(t.costs))
        cost = share[share_key(cset, t)] * npv + no_admin if t.share is not None else no_admin
        test_out = {f"{t.id}Ben": ben, f"{t.id}Cost": cost}
        if "NoAdmin" in t.variants:
            test_out[f"{t.id}CostNoAdmin"] = no_admin
        if "Gross" in t.variants:
            gross_costs = _sum(d, cols(t.costs, gross=True))
            test_out[f"{t.id}CostGross"] = share[share_key(cset, t, gross=True)] * npv + gross_costs if t.share is not None else gross_costs
        test_out[f"{t.id}Ratio"] = ratio(ben, cost)
        if "NoAdmin" in t.variants:
            test_out[f"{t.id}RatioNoAdmin"] = ratio(ben, no_admin)
        elec, gas = d[cset.column("ElecBen", t.pricing)] if (t.lifecycle_net_benefit or t.levelized) else None, \
            d[cset.column("GasBen", t.pricing)] if (t.lifecycle_net_benefit or t.levelized) else None
        if t.lifecycle_net_benefit:
            test_out[f"{t.id}LifecycleNetBen"] = elec + gas - cost
        for v in t.levelized:
            c = test_out[f"{t.id}Cost{v}"]
            test_out[f"Lev{t.id}Cost{v}"] = ratio(c * weighted_elec_alloc, d["DiscountedSavingsNetkWh"])
            test_out[f"LevNetBen{t.id}Elec{v}"] = ratio(elec - c * weighted_elec_alloc, d["DiscountedSavingsNetkWh"])
            test_out[f"LevNetBen{t.id}Gas{v}"] = ratio(gas - c * (1.0 - weighted_elec_alloc), d["DiscountedSavingsNetThm"])
        out.update({k: test_out[k] for k in t.outputs()})          # the set's column order (Test.outputs)
    return out


def internal_columns(cset) -> list[str]:
    """The stream columns the set marks internal (priced for a test, never written)."""
    return [cset.column(s.name, p) for s in cset.streams.values() if s.internal for p in s.pricings]


def societal_columns(cset, priced_rows: set[str]) -> list[str]:
    """The output columns of every test and aggregate priced on ACC rows the run did not price (SCB / SCH when the SCT is off)."""
    out = []
    for a in cset.aggregates.values():
        if cset.pricings[a.pricing].acc_rows not in priced_rows:
            out += [a.name] + ([f"{a.name}Gross"] if a.gross else [])
    for t in cset.tests.values():
        if cset.pricings[t.pricing].acc_rows not in priced_rows:
            out += t.outputs()
    return out


def formulas(cset) -> str:
    """What `evaluate` computes for `cset`, as the text block CALCULATIONS C12 carries for the set in force (tests/test_test_evaluator.py checks the doc
    against it, so the doc says what the code does)."""
    plus = lambda cols: " + ".join(cols) if cols else "0"
    lines = []
    for a in cset.aggregates.values():
        lines.append(f"{a.name} = {plus([cset.column(s, a.pricing) for s in a.plus])}"
                     + "".join(f" - {cset.column(s, a.pricing)}" for s in a.minus))
        if a.gross:
            lines.append(f"{a.name}Gross = {plus([cset.column(s, a.pricing, gross=True) for s in a.plus])}"
                         + "".join(f" - {cset.column(s, a.pricing, gross=True)}" for s in a.minus))
    for t in cset.tests.values():
        p = cset.pricings[t.pricing]
        cols = lambda names, gross=False: [cset.column(s, t.pricing, gross=gross) for s in names]
        npv = PROGRAM_SUMS[p.discount_rate_annual].lstrip("_")
        lines += ["", f"{t.id}  ({t.label}; {p.label}" + ("" if t.share is None else f"; program costs shared as share_{share_key(cset, t)}") + ")"]
        lines.append(f"  {t.id}Ben = {plus(cols(t.benefits) + cols(t.numerator_adders))}")
        costs = plus(cols(t.costs))
        if t.share is None:
            lines.append(f"  {t.id}Cost = {costs}")
        else:
            if "NoAdmin" in t.variants:
                lines.append(f"  {t.id}CostNoAdmin = {costs}")
            lines.append(f"  {t.id}Cost = share_{share_key(cset, t)} * {npv} + " + (f"{t.id}CostNoAdmin" if "NoAdmin" in t.variants else costs))
        if "Gross" in t.variants:
            gross = plus(cols(t.costs, gross=True))
            lines.append(f"  {t.id}CostGross = " + (gross if t.share is None else f"share_{share_key(cset, t, gross=True)} * {npv} + {gross}"))
        lines.append(f"  {t.id}Ratio = ratio({t.id}Ben, {t.id}Cost)" + (f"    {t.id}RatioNoAdmin = ratio({t.id}Ben, {t.id}CostNoAdmin)"
                                                                         if "NoAdmin" in t.variants else ""))
        elec, gas = cset.column("ElecBen", t.pricing), cset.column("GasBen", t.pricing)
        if t.lifecycle_net_benefit:
            lines.append(f"  {t.id}LifecycleNetBen = {elec} + {gas} - {t.id}Cost")
        for v in t.levelized:
            lines.append(f"  Lev{t.id}Cost{v} = ratio({t.id}Cost{v} * WeightedElecAlloc, DiscountedSavingsNetkWh)    "
                         f"LevNetBen{t.id}Elec{v}, LevNetBen{t.id}Gas{v}: C19.6")
    shares = [f"share_{k} = C11.2 over {plus(c)}" for k, c in share_bases(cset).items()]
    return "\n".join(lines + ["", *shares])
