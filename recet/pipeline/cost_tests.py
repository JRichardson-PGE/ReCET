"""
Gross and NoAdmin cost-effectiveness variants, plus the program-level cost
weighting that separates them from the regular tests. Ported from `CalcCE`
(CET_v25.1_documentation_extracted.txt: ProgramCosts 14750-14795,
BenefitsSum 14920-14990, Calculations 15148-15723, ratios 16122-16215).

DEFINITIONS (real CalcCE):
  Gross  = the same benefit/cost formulas with no NTG and no market-effects
           factor. With NTG applied as a per-measure multiplier, gross =
           net / NTGR (the POC prices no kW demand term, so this is exact).
  NoAdmin = the regular cost test minus its program-level term. That term is
           `share * SumCostsNPV`: the program's summed ProgramCost.csv lines
           (admin, marketing, DI, EMV, ...), each discounted ANNUALLY with
           `Raf ^ (Year - FirstYear)`, allocated to a measure by its share of
           its program's positive benefits (1/ClaimCount when the program has
           none). Everything else in the cost is identical.

VALIDATION STATUS: both real ground-truth jobs (SWWH025/121074 and the small
sample) have all-zero ProgramCost rows and NTGR == 1, so against real CET the
NoAdmin costs equal the regular costs and Gross equals Net -- checked, but
that does not exercise the admin allocation or any NTG scaling. Those
branches are implemented from the SQL and only internally checked (program
costs allocate to exactly their total).

SUSPECTED CET ERRORS, deliberately NOT copied (no ground truth exists to
settle them, since this data has NTG == 1 and no admin costs; logged in
CLAUDE.md finding 11):
  1. `TRC_CostGross`/`SCB_CostGross`/`SCH_CostGross` add the NET supply costs
     (`ElecSupplyCost`, `GasSupplyCost`) to GROSS participant cost. Here the
     gross supply costs are used.
  2. `GasSupplyCostGross` (and _SB/_SH) is assigned the NET `GasSupplyCost`
     (CalcCE line 14706). Here it is net / NTGRTherm.
  3. `SCB_Cost`/`SCH_Cost` discount program costs with the PRIMARY rate
     (`pcSum.SumCostsNPV`); `SumCostsNPV_SCT` is computed but never used.
     Here the societal-rate NPV is used for the societal tests.
  4. `BenPos.ElecBenPos_SH` tests `ElecBen_SB > 0` rather than `ElecBen_SH`,
     and `SumBenPos_SB/_SH` use the TRC `GasBen` rather than `GasBen_SB/_SH`.
     No exported column depends on them (WeightedElecAlloc uses the TRC
     ElecBenPos / SumBenPos), so there is nothing to reproduce.

LEVELIZED AND OTHER CalcCost COLUMNS (CET_v25.1_documentation_extracted.txt
16434-17484): the program-cost components each measure carries (Wtd<field> =
the program's nominal total of the field x WeightedBenefits; ProgramCosts =
their sum, = WeightedProgramCost), the lifecycle net benefits (TRC / PAC:
ElecBen + GasBen - cost) and the levelized columns, which divide a benefit or
an electric (gas) share of a cost by the discounted net kWh (therms):
    LevBenElec        = ElecBen / DSkWh           LevBenGas = GasBen / DSThm
    Lev<T>Cost        = <T>Cost x WeightedElecAlloc / DSkWh            (T = TRC, PAC, RIM; and NoAdmin)
    LevNetBen<T>Elec  = (ElecBen - <T>Cost x WeightedElecAlloc) / DSkWh
    LevNetBen<T>Gas   = (GasBen - <T>Cost x (1 - WeightedElecAlloc)) / DSThm
each 0 when its denominator is 0. WeightedElecAlloc = the measure's positive
ElecBen / its positive ElecBen + GasBen + WaterEnergyBen + OtherBen (0 if none).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import discount_settings
from .delimited import read_cet_csv

_COST_FIELDS = [
    "AdminCostsOverheadAndGA", "AdminCostsOther", "MarketingOutreach", "DIActivity", "DIInstallation",
    "DIHardwareAndMaterials", "DIRebateAndInspection", "EMV", "UserInputIncentive", "CostsRecoveredFromOtherSources",
]


# CET has no PA called "SW": run 121219 (2028 input, 18 statewide programs whose ProgramCost rows say PA = SW) returned all 18 rows
# with PA = SCE, and discounts them at that PA's rate. Applied here for UNALLOCATED statewide program costs. It made no numerical
# difference in that job (every cost sits in the First Year, so the discount exponent is 0), so which PA CET really uses for
# future-year statewide costs is unverified. Allocating first (statewide_allocation.allocate_program_costs) avoids the question.
UNALLOCATED_SW_PA = "SCE"


def load_program_costs(path: Path, allocate_statewide: bool = False) -> pd.DataFrame:
    """A real ProgramCost.csv (pipe-delimited, or comma-delimited: pipeline/delimited.py).

    allocate_statewide=True splits each statewide program's costs across the IOUs first
    (pipeline/statewide_allocation.allocate_program_costs): PrgID gets `<IOU>_` prepended and PA is set to the IOU, so each IOU's
    program costs discount at its own rate and are shared only among that IOU's own measures. Use it together with
    data_loading.load_measures_from_csv(..., allocate_statewide=True) so PrgIDs line up (see load_job).

    Without allocation, rows with PA = SW are given PA = UNALLOCATED_SW_PA (see above) so they can be discounted."""
    df = read_cet_csv(path)
    if allocate_statewide:
        from .statewide_allocation import allocate_program_costs, warn_unmatched
        df, report = allocate_program_costs(df)
        warn_unmatched(report, "ProgramCost.csv")
    df["PrgID"] = df["PrgID"].astype(str).str.strip()
    df["PA"] = df["PA"].astype(str).str.strip().replace({"SW": UNALLOCATED_SW_PA})
    df["Year"] = df["ClaimYearQuarter"].astype(str).str[:4].astype(int)
    return df


def check_program_alignment(measures: pd.DataFrame, program: pd.DataFrame | None) -> dict:
    """PrgIDs must line up between the measures and the program costs, or costs silently drop out of the cost tests (a program
    cost row with no measures is never charged; a measure whose program has no cost row gets none). After statewide allocation both
    sides carry the same `<IOU>_` prefix; a mismatch means only one of the two files was allocated. Returns the two difference sets."""
    m_ids = set(measures["prg_id"].astype(str).str.strip())
    p_ids = set() if program is None else set(program["PrgID"])
    return {"program_costs_without_measures": sorted(p_ids - m_ids), "measure_programs_without_costs": sorted(m_ids - p_ids)}


def program_cost_sums(program: pd.DataFrame | None, first_year: int, acc_version: int = 2024) -> pd.DataFrame:
    """Per PrgID: SumCosts (nominal), SumCostsNPV (Raf), SumCostsNPV_SCT (Raf_SCT), and each cost field's nominal total (`Sum<field>`, for the Wtd columns)."""
    if program is None or program.empty:
        return pd.DataFrame(columns=["PrgID", "SumCosts", "SumCostsNPV", "SumCostsNPV_SCT"] + [f"Sum{f}" for f in _COST_FIELDS])
    p = program.copy()
    p["total"] = p[_COST_FIELDS].fillna(0.0).sum(axis=1)
    raf = p["PA"].map(lambda pa: 1.0 + discount_settings.get_discount_rate_annual(pa, acc_version))
    raf_sct = p["PA"].map(lambda pa: 1.0 + discount_settings.get_settings(pa, acc_version)["discount_rate_annual_sct"])
    n = p["Year"] - first_year
    p["npv"] = p["total"] / raf ** n
    p["npv_sct"] = p["total"] / raf_sct ** n
    p[_COST_FIELDS] = p[_COST_FIELDS].fillna(0.0)
    return p.groupby("PrgID", as_index=False).agg(
        SumCosts=("total", "sum"), SumCostsNPV=("npv", "sum"), SumCostsNPV_SCT=("npv_sct", "sum"), **{f"Sum{f}": (f, "sum") for f in _COST_FIELDS})


def _share(pos_by_measure: pd.Series, prg: pd.Series) -> pd.Series:
    """A measure's share of its program's positive benefits; 1/ClaimCount if the program has none."""
    total = pos_by_measure.groupby(prg).transform("sum")
    count = pos_by_measure.groupby(prg).transform("size")
    return pd.Series(np.where(total != 0, pos_by_measure / total.replace(0, np.nan), 1.0 / count), index=pos_by_measure.index)


def _pos(s: pd.Series) -> pd.Series:
    return s.where(s > 0, 0.0)


def compute_cost_tests(d: pd.DataFrame, prog: pd.DataFrame) -> pd.DataFrame:
    """
    `d` (one row per measure) must carry: measure_id, prg_id, NTGRkWh, ntgr_therm,
    ElecBen, GasBen, ElecSupplyCost, GasSupplyCost, ElecBen_SB/_SH, GasBen_SB/_SH,
    ElecSupplyCost_SB/_SH, GasSupplyCost_SB/_SH, OtherBen/OtherCost/TaxCredits and
    their Gross forms, and the (primary and _sct) participant PV columns
    RebatesandIncentsPV, NetParticipantCostPV, NetParticipantCostAdjustedPV (the one the TRC / SCT costs use: it includes the market-effects term),
    GrossParticipantCostPV (+ *_sct),
    BillReducElec/Gas, BillIncrElec/Gas. `prog` is program_cost_sums(...).
    WaterEnergyBen / WaterEnergyCost (+ _SB/_SH) enter TSB, the program-cost weighting and every cost and ratio the way CalcCE has them (Calculations CTE 15148-15723);
    a frame without those columns is treated as having none.
    Returns d plus every Gross / NoAdmin / weighting output column.
    """
    d = d.copy()
    for c in ("WaterEnergyBen", "WaterEnergyBen_SB", "WaterEnergyBen_SH", "WaterEnergyCost", "WaterEnergyCost_SB", "WaterEnergyCost_SH",
              "DiscountedSavingsNetkWh", "DiscountedSavingsNetThm"):
        if c not in d:
            d[c] = 0.0
    prg = d["prg_id"]
    ntg_e = d["NTGRkWh"].replace(0, np.nan)
    ntg_t = d["ntgr_therm"].replace(0, np.nan)

    for sfx in ("", "_SB", "_SH"):
        d[f"ElecBenGross{sfx}"] = (d[f"ElecBen{sfx}"] / ntg_e).fillna(0.0)
        d[f"GasBenGross{sfx}"] = (d[f"GasBen{sfx}"] / ntg_t).fillna(0.0)
        d[f"ElecSupplyCostGross{sfx}"] = (d[f"ElecSupplyCost{sfx}"] / ntg_e).fillna(0.0)
        d[f"GasSupplyCostGross{sfx}"] = (d[f"GasSupplyCost{sfx}"] / ntg_t).fillna(0.0)
        d[f"WaterEnergyBenGross{sfx}"] = (d[f"WaterEnergyBen{sfx}"] / ntg_e).fillna(0.0)          # water energy is priced on the kWh NTG
        d[f"WaterEnergyCostGross{sfx}"] = (d[f"WaterEnergyCost{sfx}"] / ntg_e).fillna(0.0)

    d["TotalSystemBenefit"] = ((d.ElecBen + d.GasBen + d.WaterEnergyBen + d.OtherBen)
                               - (d.ElecSupplyCost + d.GasSupplyCost + d.WaterEnergyCost + d.OtherCost))
    d["TotalSystemBenefitGross"] = (
        (d.ElecBenGross + d.GasBenGross + d.WaterEnergyBenGross + d.OtherBenGross)
        - (d.ElecSupplyCostGross + d.GasSupplyCostGross + d.WaterEnergyCostGross + d.OtherCostGross))

    sums = ["SumCosts", "SumCostsNPV", "SumCostsNPV_SCT"] + [f"Sum{f}" for f in _COST_FIELDS]
    ps = prog.set_index("PrgID") if len(prog) else pd.DataFrame(columns=sums)
    for col in sums:
        d[f"_{col}"] = prg.map(ps[col]).fillna(0.0) if len(ps) and col in ps else 0.0          # a hand-made `prog` may lack the per-field sums

    def ben_share(elec, gas, water, other):
        return _share(_pos(d[elec]) + _pos(d[gas]) + _pos(d[water]) + _pos(d[other]), prg)

    sh_trc = ben_share("ElecBen", "GasBen", "WaterEnergyBen", "OtherBen")
    sh_trc_g = ben_share("ElecBenGross", "GasBenGross", "WaterEnergyBenGross", "OtherBenGross")
    sh_sb = ben_share("ElecBen_SB", "GasBen_SB", "WaterEnergyBen_SB", "OtherBen")
    sh_sh = ben_share("ElecBen_SH", "GasBen_SH", "WaterEnergyBen_SH", "OtherBen")
    sh_sb_g = ben_share("ElecBenGross_SB", "GasBenGross_SB", "WaterEnergyBenGross_SB", "OtherBenGross")
    sh_sh_g = ben_share("ElecBenGross_SH", "GasBenGross_SH", "WaterEnergyBenGross_SH", "OtherBenGross")
    d["WeightedBenefits"] = sh_trc
    d["WeightedProgramCost"] = sh_trc * d["_SumCosts"]
    for f in _COST_FIELDS:                                           # CalcCost's program-cost components, each the program's nominal total x WeightedBenefits
        d[f"Wtd{f}"] = sh_trc * d[f"_Sum{f}"]
    d["ProgramCosts"] = d["WeightedProgramCost"]                     # CalcCost's own name for the same figure
    pos_e = _pos(d.ElecBen)
    pos_all = pos_e + _pos(d.GasBen) + _pos(d.WaterEnergyBen) + _pos(d.OtherBen)
    d["WeightedElecAlloc"] = (pos_e / pos_all.replace(0, np.nan)).fillna(0.0)
    # CET's TotalExpenditures (the run receipt's "Budget"): nominal rebates and incentives plus the measure's share of nominal program costs. Matches CET jobs
    # 121219 and 121841 to the cent in total (CalcCost's ProgramCosts = WeightedProgramCost).
    d["TotalExpenditures"] = (d["RebatesandIncents"] if "RebatesandIncents" in d else 0.0) + d["WeightedProgramCost"]

    sup = d.ElecSupplyCost + d.GasSupplyCost + d.WaterEnergyCost + d.OtherCost
    sup_g = d.ElecSupplyCostGross + d.GasSupplyCostGross + d.WaterEnergyCostGross + d.OtherCostGross
    inc, inc_s = d.RebatesandIncentsPV, d.RebatesandIncentsPV_sct

    d["PACCostNoAdmin"] = inc + sup
    d["PACCost"] = sh_trc * d["_SumCostsNPV"] + d["PACCostNoAdmin"]
    d["TRCCostNoAdmin"] = inc + d.NetParticipantCostAdjustedPV + sup
    d["TRCCost"] = sh_trc * d["_SumCostsNPV"] + d["TRCCostNoAdmin"]
    d["TRCCostGross"] = sh_trc_g * d["_SumCostsNPV"] + inc + d.GrossParticipantCostPV + sup_g
    d["RIMCostNoAdmin"] = d.BillReducElec + d.BillReducGas + d["PACCostNoAdmin"]
    d["RIMCost"] = d.BillReducElec + d.BillReducGas + d["PACCost"]

    for sfx, sh, sh_g in (("SB", sh_sb, sh_sb_g), ("SH", sh_sh, sh_sh_g)):
        s = f"_{sfx}"
        sup_s = d[f"ElecSupplyCost{s}"] + d[f"GasSupplyCost{s}"] + d[f"WaterEnergyCost{s}"] + d.OtherCost
        sup_sg = d[f"ElecSupplyCostGross{s}"] + d[f"GasSupplyCostGross{s}"] + d[f"WaterEnergyCostGross{s}"] + d.OtherCostGross
        name = "SCB" if sfx == "SB" else "SCH"
        d[f"{name}CostNoAdmin"] = inc_s + d.NetParticipantCostAdjustedPV_sct + sup_s
        d[f"{name}Cost"] = sh * d["_SumCostsNPV_SCT"] + d[f"{name}CostNoAdmin"]
        d[f"{name}CostGross"] = sh_g * d["_SumCostsNPV_SCT"] + inc_s + d.GrossParticipantCostPV_sct + sup_sg

    def ratio(num, den):
        return (num / den.replace(0, np.nan)).fillna(0.0) + 0.0          # + 0.0: a zero over a negative denominator is 0, not -0

    b = d.ElecBen + d.GasBen + d.WaterEnergyBen + d.OtherBen
    d["TRCRatio"] = ratio(b + d.TaxCredits, d.TRCCost)
    d["TRCRatioNoAdmin"] = ratio(b + d.TaxCredits, d.TRCCostNoAdmin)
    d["PACRatio"] = ratio(b, d.PACCost)
    d["PACRatioNoAdmin"] = ratio(b, d.PACCostNoAdmin)
    rim_b = b + d.BillIncrElec + d.BillIncrGas
    d["RIMRatio"] = ratio(rim_b, d.RIMCost)
    d["RIMRatioNoAdmin"] = ratio(rim_b, d.RIMCostNoAdmin)
    b_sb = d.ElecBen_SB + d.GasBen_SB + d.WaterEnergyBen_SB + d.OtherBen
    b_sh = d.ElecBen_SH + d.GasBen_SH + d.WaterEnergyBen_SH + d.OtherBen
    d["SCBRatio"] = ratio(b_sb, d.SCBCost)
    d["SCHRatio"] = ratio(b_sh, d.SCHCost)
    d["SCBRatioNoAdmin"] = ratio(b_sb, d.SCBCostNoAdmin)
    d["SCHRatioNoAdmin"] = ratio(b_sh, d.SCHCostNoAdmin)

    # CalcCost's lifecycle net benefits and levelized columns (see the module docstring); the benefits are ElecBen + GasBen only, as CET has them
    d["TRCLifecycleNetBen"] = d.ElecBen + d.GasBen - d.TRCCost
    d["PACLifecycleNetBen"] = d.ElecBen + d.GasBen - d.PACCost
    ds_e, ds_g, wea = d.DiscountedSavingsNetkWh, d.DiscountedSavingsNetThm, d.WeightedElecAlloc
    d["LevBenElec"] = ratio(d.ElecBen, ds_e)
    d["LevBenGas"] = ratio(d.GasBen, ds_g)
    for t in ("TRCCost", "TRCCostNoAdmin", "PACCost", "PACCostNoAdmin", "RIMCost"):
        d[f"Lev{t}"] = ratio(d[t] * wea, ds_e)
    for t in ("TRC", "PAC", "RIM"):
        for sfx in (("", "NoAdmin") if t != "RIM" else ("",)):
            cost = d[f"{t}Cost{sfx}"]
            d[f"LevNetBen{t}Elec{sfx}"] = ratio(d.ElecBen - cost * wea, ds_e)
            d[f"LevNetBen{t}Gas{sfx}"] = ratio(d.GasBen - cost * (1.0 - wea), ds_g)
    return d.drop(columns=[c for c in d.columns if c.startswith("_")])
