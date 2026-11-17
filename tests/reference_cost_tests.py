"""The cost tests as ReCET 0.1.81 computed them (recet/pipeline/cost_tests.py's `compute_cost_tests`, and the PCT of run_poc.participant_cost_test), frozen
here as the reference the definition-set evaluator (recet/pipeline/test_evaluator.py) is accepted against: on the EE-2026 set every column must equal
this to 1e-9 (tests/test_cost_test_sets.py). Kept unchanged for as long as EE-2026 exists; tests/reference_calc.py's per-measure loop is a third opinion.
Only the imports and this docstring differ from the 0.1.81 source (and `pct`, which is participant_cost_test's arithmetic on its priced inputs)."""
from __future__ import annotations

import numpy as np
import pandas as pd

_COST_FIELDS = [
    "AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation",
    "DIHardwareAndMaterials", "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources",
]


def _share(positive_benefits: pd.Series, PrgID: pd.Series, totals: pd.DataFrame | None = None) -> pd.Series:
    """A measure's share of its program's positive benefits; 1/ClaimCount if the program has none. `totals` (index PrgID, columns `sum` and `count`, see
    share_totals) gives the programs' totals when `positive_benefits` is only part of the job (a chunked run); None = the totals of the measures given."""
    if totals is None:
        program_positive_benefits = positive_benefits.groupby(PrgID).transform("sum")
        program_measure_count = positive_benefits.groupby(PrgID).transform("size")            # CalcCE's ClaimCount
    else:
        program_positive_benefits = PrgID.map(totals["sum"]).astype(float)
        program_measure_count = PrgID.map(totals["count"]).astype(float)
    return pd.Series(np.where(program_positive_benefits != 0, positive_benefits / program_positive_benefits.replace(0, np.nan), 1.0 / program_measure_count),
                     index=positive_benefits.index)


# the program-level shares compute_cost_tests uses: name -> the four benefits whose positive parts are summed per measure
SHARES = {
    "trc": ("ElecBen", "GasBen", "WaterEnergyBen", "OtherBen"),
    "trc_gross": ("ElecBenGross", "GasBenGross", "WaterEnergyBenGross", "OtherBenGross"),
    "sct_base": ("ElecBen_SB", "GasBen_SB", "WaterEnergyBen_SB", "OtherBen"),
    "sct_high": ("ElecBen_SH", "GasBen_SH", "WaterEnergyBen_SH", "OtherBen"),
    "sct_base_gross": ("ElecBenGross_SB", "GasBenGross_SB", "WaterEnergyBenGross_SB", "OtherBenGross"),
    "sct_high_gross": ("ElecBenGross_SH", "GasBenGross_SH", "WaterEnergyBenGross_SH", "OtherBenGross"),
}


def _with_gross(d: pd.DataFrame) -> pd.DataFrame:
    """`d` (a copy) with the water-energy and discounted-savings columns filled in when absent and the Gross benefits and supply costs added."""
    d = d.copy()
    for c in ("WaterEnergyBen", "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
              "DiscountedSavingsNetkWh", "DiscountedSavingsNetThm"):
        if c not in d:
            d[c] = 0.0
    NTGRkWh_plus_MEBens = d["NTGRkWh_plus_MEBens"].replace(0, np.nan)          # what the net benefits carry: divided out for Gross
    NTGRTherm_plus_MEBens = d["NTGRTherm_plus_MEBens"].replace(0, np.nan)
    for sfx in ("", "_SB", "_SH"):
        d[f"ElecBenGross{sfx}"] = (d[f"ElecBen{sfx}"] / NTGRkWh_plus_MEBens).fillna(0.0)
        d[f"GasBenGross{sfx}"] = (d[f"GasBen{sfx}"] / NTGRTherm_plus_MEBens).fillna(0.0)
        d[f"ElecSupplyCostGross{sfx}"] = (d[f"ElecSupplyCost{sfx}"] / NTGRkWh_plus_MEBens).fillna(0.0)
        d[f"GasSupplyCostGross{sfx}"] = (d[f"GasSupplyCost{sfx}"] / NTGRTherm_plus_MEBens).fillna(0.0)
        d[f"WaterEnergyBenGross{sfx}"] = (d[f"WaterEnergyBen{sfx}"] / NTGRkWh_plus_MEBens).fillna(0.0)          # water energy is priced on the kWh NTG
        d[f"WaterEnergyCostGross{sfx}"] = (d[f"WaterEnergyCost{sfx}"] / NTGRkWh_plus_MEBens).fillna(0.0)
    return d


def share_inputs(d: pd.DataFrame) -> pd.DataFrame:
    """Per measure (aligned with `d`, the frame compute_cost_tests takes): the positive benefits each program-level share in SHARES divides up."""
    g = _with_gross(d)
    return pd.DataFrame({name: sum(_pos(g[c]) for c in cols) for name, cols in SHARES.items()}, index=d.index)


def share_totals(pos: pd.DataFrame, PrgID: pd.Series) -> dict[str, pd.DataFrame]:
    """Per share in `pos` (share_inputs, or any frame of per-measure positive amounts): each program's `sum` and measure `count`. Totals of separate parts of a job
    add up (combine_share_totals), which is how a chunked run gives every measure the share it would have in the whole job."""
    by = pos.groupby(PrgID.to_numpy())
    sums, counts = by.sum(), by.size()
    return {name: pd.DataFrame({"sum": sums[name], "count": counts}) for name in pos.columns}


def combine_share_totals(a: dict[str, pd.DataFrame] | None, b: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """The totals of two parts of a job together."""
    if a is None:
        return b
    return {k: a[k].add(b[k], fill_value=0.0) for k in b}


def _pos(s: pd.Series) -> pd.Series:
    return s.where(s > 0, 0.0)


def compute_cost_tests(d: pd.DataFrame, prog: pd.DataFrame, totals: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """
    `d` (one row per measure) must carry: CEInputID, PrgID, NTGRkWh_plus_MEBens, NTGRTherm_plus_MEBens,
    ElecBen, GasBen, ElecSupplyCost, GasSupplyCost, ElecBen_SB/_SH, GasBen_SB/_SH,
    ElecSupplyCost_SB/_SH, GasSupplyCost_SB/_SH, OtherBen/OtherCost/TaxCredits and
    their Gross forms, and the (primary and _SCT) participant PV columns
    RebatesandIncentsPV, NetParticipantCostPV, NetParticipantCostAdjustedPV (the one the TRC / SCT costs use: it includes the market-effects term),
    GrossParticipantCostPV (+ *_SCT),
    LifecycleBillReducElec/Gas, LifecycleBillIncrElec/Gas. `prog` is program_cost_sums(...).
    WaterEnergyBen / WaterEnergyCost (+ _SB/_SH) enter TSB, the program-cost weighting and every cost and ratio the way CalcCE has them (Calculations CTE 15148-15723);
    a frame without those columns is treated as having none.
    `totals` (share_totals over the whole job) gives the programs' benefit totals when `d` holds only part of the job (a chunked run, see recet/chunked.py);
    None = the programs' totals over `d` itself.
    Returns d plus every Gross / NoAdmin / weighting output column.
    """
    d = _with_gross(d)
    PrgID = d["PrgID"]

    d["TotalSystemBenefit"] = ((d.ElecBen + d.GasBen + d.WaterEnergyBen + d.OtherBen)
                               - (d.ElecSupplyCost + d.GasSupplyCost + d.WaterEnergyCost + d.OtherCost))
    d["TotalSystemBenefitGross"] = (
        (d.ElecBenGross + d.GasBenGross + d.WaterEnergyBenGross + d.OtherBenGross)
        - (d.ElecSupplyCostGross + d.GasSupplyCostGross + d.WaterEnergyCostGross + d.OtherCostGross))

    sums = ["SumCosts", "SumCostsNPV", "SumCostsNPV_SCT"] + [f"Sum{f}" for f in _COST_FIELDS]
    program_sums = prog.set_index("PrgID") if len(prog) else pd.DataFrame(columns=sums)
    for col in sums:
        d[f"_{col}"] = PrgID.map(program_sums[col]).fillna(0.0) if len(program_sums) and col in program_sums else 0.0   # a hand-made `prog` may lack the per-field sums

    def benefit_share(name):
        return _share(sum(_pos(d[c]) for c in SHARES[name]), PrgID, None if totals is None else totals[name])

    # each measure's share of its program's costs, per test (CalcCE's WeightedBenefits for TRC)
    share_trc, share_trc_gross, share_sct_base, share_sct_high, share_sct_base_gross, share_sct_high_gross = (
        benefit_share(k) for k in ("trc", "trc_gross", "sct_base", "sct_high", "sct_base_gross", "sct_high_gross"))
    d["WeightedBenefits"] = share_trc
    d["WeightedProgramCost"] = share_trc * d["_SumCosts"]
    for f in _COST_FIELDS:                                           # CalcCost's program-cost components, each the program's nominal total x WeightedBenefits
        d[f"Wtd{f}"] = share_trc * d[f"_Sum{f}"]
    positive_ElecBen = _pos(d.ElecBen)                               # CalcCE's ElecBenPos
    positive_benefits = positive_ElecBen + _pos(d.GasBen) + _pos(d.WaterEnergyBen) + _pos(d.OtherBen)   # SumBenPos
    d["WeightedElecAlloc"] = (positive_ElecBen / positive_benefits.replace(0, np.nan)).fillna(0.0)
    # CET's TotalExpenditures (the run receipt's "Budget"): nominal rebates and incentives plus the measure's share of nominal program costs. Matches CET jobs
    # 121219 and 121841 to the cent in total (CalcCost's ProgramCosts = WeightedProgramCost).
    d["TotalExpenditures"] = (d["RebatesandIncents"] if "RebatesandIncents" in d else 0.0) + d["WeightedProgramCost"]

    supply_and_other_costs = d.ElecSupplyCost + d.GasSupplyCost + d.WaterEnergyCost + d.OtherCost
    supply_and_other_costs_gross = d.ElecSupplyCostGross + d.GasSupplyCostGross + d.WaterEnergyCostGross + d.OtherCostGross
    RebatesandIncentsPV, RebatesandIncentsPV_SCT = d.RebatesandIncentsPV, d.RebatesandIncentsPV_SCT

    d["PACCostNoAdmin"] = RebatesandIncentsPV + supply_and_other_costs
    d["PACCost"] = share_trc * d["_SumCostsNPV"] + d["PACCostNoAdmin"]
    d["TRCCostNoAdmin"] = RebatesandIncentsPV + d.NetParticipantCostAdjustedPV + supply_and_other_costs
    d["TRCCost"] = share_trc * d["_SumCostsNPV"] + d["TRCCostNoAdmin"]
    d["TRCCostGross"] = share_trc_gross * d["_SumCostsNPV"] + RebatesandIncentsPV + d.GrossParticipantCostPV + supply_and_other_costs_gross
    d["RIMCostNoAdmin"] = d.LifecycleBillReducElec + d.LifecycleBillReducGas + d["PACCostNoAdmin"]
    d["RIMCost"] = d.LifecycleBillReducElec + d.LifecycleBillReducGas + d["PACCost"]

    for test, suffix, share, share_gross in (("SCB", "_SB", share_sct_base, share_sct_base_gross), ("SCH", "_SH", share_sct_high, share_sct_high_gross)):
        supply_and_other_costs_sct = d[f"ElecSupplyCost{suffix}"] + d[f"GasSupplyCost{suffix}"] + d[f"WaterEnergyCost{suffix}"] + d.OtherCost
        supply_and_other_costs_sct_gross = (d[f"ElecSupplyCostGross{suffix}"] + d[f"GasSupplyCostGross{suffix}"] + d[f"WaterEnergyCostGross{suffix}"]
                                            + d.OtherCostGross)
        d[f"{test}CostNoAdmin"] = RebatesandIncentsPV_SCT + d.NetParticipantCostAdjustedPV_SCT + supply_and_other_costs_sct
        d[f"{test}Cost"] = share * d["_SumCostsNPV_SCT"] + d[f"{test}CostNoAdmin"]
        d[f"{test}CostGross"] = share_gross * d["_SumCostsNPV_SCT"] + RebatesandIncentsPV_SCT + d.GrossParticipantCostPV_SCT + supply_and_other_costs_sct_gross

    def ratio(benefits, cost):
        return (benefits / cost.replace(0, np.nan)).fillna(0.0) + 0.0          # + 0.0: a zero over a negative denominator is 0, not -0

    # each test's numerator is written beside its ratios (<test>Ben), so ratio = <test>Ben / <test>Cost(NoAdmin) can be checked from the file
    total_benefits = d.ElecBen + d.GasBen + d.WaterEnergyBen + d.OtherBen
    d["TRCBen"] = total_benefits + d.TaxCredits
    d["TRCRatio"] = ratio(d.TRCBen, d.TRCCost)
    d["TRCRatioNoAdmin"] = ratio(d.TRCBen, d.TRCCostNoAdmin)
    d["PACBen"] = total_benefits
    d["PACRatio"] = ratio(d.PACBen, d.PACCost)
    d["PACRatioNoAdmin"] = ratio(d.PACBen, d.PACCostNoAdmin)
    d["RIMBen"] = total_benefits + d.LifecycleBillIncrElec + d.LifecycleBillIncrGas
    d["RIMRatio"] = ratio(d.RIMBen, d.RIMCost)
    d["RIMRatioNoAdmin"] = ratio(d.RIMBen, d.RIMCostNoAdmin)
    d["SCBBen"] = d.ElecBen_SB + d.GasBen_SB + d.WaterEnergyBen_SB + d.OtherBen
    d["SCBRatio"] = ratio(d.SCBBen, d.SCBCost)
    d["SCBRatioNoAdmin"] = ratio(d.SCBBen, d.SCBCostNoAdmin)
    d["SCHBen"] = d.ElecBen_SH + d.GasBen_SH + d.WaterEnergyBen_SH + d.OtherBen
    d["SCHRatio"] = ratio(d.SCHBen, d.SCHCost)
    d["SCHRatioNoAdmin"] = ratio(d.SCHBen, d.SCHCostNoAdmin)

    # CalcCost's lifecycle net benefits and levelized columns (see the module docstring); the benefits are ElecBen + GasBen only, as CET has them
    d["TRCLifecycleNetBen"] = d.ElecBen + d.GasBen - d.TRCCost
    d["PACLifecycleNetBen"] = d.ElecBen + d.GasBen - d.PACCost
    DiscountedSavingsNetkWh, DiscountedSavingsNetThm, WeightedElecAlloc = d.DiscountedSavingsNetkWh, d.DiscountedSavingsNetThm, d.WeightedElecAlloc
    d["LevBenElec"] = ratio(d.ElecBen, DiscountedSavingsNetkWh)
    d["LevBenGas"] = ratio(d.GasBen, DiscountedSavingsNetThm)
    for t in ("TRCCost", "TRCCostNoAdmin", "PACCost", "PACCostNoAdmin", "RIMCost"):
        d[f"Lev{t}"] = ratio(d[t] * WeightedElecAlloc, DiscountedSavingsNetkWh)
    for t in ("TRC", "PAC", "RIM"):
        for sfx in (("", "NoAdmin") if t != "RIM" else ("",)):
            cost = d[f"{t}Cost{sfx}"]
            d[f"LevNetBen{t}Elec{sfx}"] = ratio(d.ElecBen - cost * WeightedElecAlloc, DiscountedSavingsNetkWh)
            d[f"LevNetBen{t}Gas{sfx}"] = ratio(d.GasBen - cost * (1.0 - WeightedElecAlloc), DiscountedSavingsNetThm)
    return d.drop(columns=[c for c in d.columns if c.startswith("_")])


def pct(d: pd.DataFrame) -> pd.DataFrame:
    """PCTBen, PCTCost, PCTRatio as run_poc.participant_cost_test computed them in 0.1.81, from the gross bill impacts, RebatesandIncentsPV,
    GrossParticipantCostPV and TaxCreditsGross / PVF (here the column TaxCreditsGrossPV)."""
    out = pd.DataFrame(index=d.index)
    out["PCTBen"] = d["LifecycleBillReducElecGross"] + d["LifecycleBillReducGasGross"] + d["RebatesandIncentsPV"] + d["TaxCreditsGrossPV"]
    out["PCTCost"] = d["GrossParticipantCostPV"] + d["RebatesandIncentsPV"] + d["LifecycleBillIncrElecGross"] + d["LifecycleBillIncrGasGross"]
    out["PCTRatio"] = (out["PCTBen"] / out["PCTCost"].where(out["PCTCost"] != 0)).fillna(0.0)
    return out
