"""
The program-level half of the cost tests: program-cost sums and shares, the gross forms of the priced streams and CalcCost's weighting columns. The
tests themselves (every Ben / Cost / Ratio, the NoAdmin and Gross variants, lifecycle and levelized columns) are the run's cost-test set, priced by
pipeline/test_evaluator.py (0.1.84; the arithmetic this module wrote out before is frozen as tests/reference_cost_tests.py). The definitions below are
EE-2026's, which reproduces CalcCE except where noted. Ported from `CalcCE`
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
the program's nominal total of the field x WeightedBenefits; CET's ProgramCosts,
their sum, equals WeightedProgramCost and is not written), the lifecycle net benefits (TRC / PAC:
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

from . import discount_settings, test_evaluator
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
    df["Used_Year"] = df["ClaimYearQuarter"].astype(str).str[:4].astype(int)          # the year a row's costs are discounted from
    return df


def check_program_alignment(measures: pd.DataFrame, program: pd.DataFrame | None) -> dict:
    """The PrgIDs that do not line up between the measures and the program costs: a program cost row with no measures is never charged, and a
    measure whose program has no cost row gets none (preflight warns about both, BRD BR-06.16). After statewide allocation both sides carry the
    same `<IOU>_` prefix. Returns the two difference sets."""
    m_ids = set(measures["PrgID"].astype(str).str.strip())
    p_ids = set() if program is None else set(program["PrgID"])
    return {"program_costs_without_measures": sorted(p_ids - m_ids), "measure_programs_without_costs": sorted(m_ids - p_ids)}


def program_cost_sums(program: pd.DataFrame | None, first_year: int, acc_version: int = 2024) -> pd.DataFrame:
    """Per PrgID: SumCosts (nominal), SumCostsNPV (Raf), SumCostsNPV_SCT (Raf_SCT), and each cost field's nominal total (`Sum<field>`, for the Wtd columns)."""
    if program is None or program.empty:
        return pd.DataFrame(columns=["PrgID", "SumCosts", "SumCostsNPV", "SumCostsNPV_SCT"] + [f"Sum{f}" for f in _COST_FIELDS])
    p = program.copy()
    p["RowCosts"] = p[_COST_FIELDS].fillna(0.0).sum(axis=1)                                     # a ProgramCost row's ten cost fields, nominal
    annual_discount_factor = p["PA"].map(lambda pa: 1.0 + discount_settings.get_discount_rate_annual(pa, acc_version))                 # CalcCE's Raf
    annual_discount_factor_sct = p["PA"].map(lambda pa: 1.0 + discount_settings.get_settings(pa, acc_version)["DiscountRateAnnual_SCT"])
    years_from_first_year = p["Used_Year"] - first_year
    p["RowCostsNPV"] = p["RowCosts"] / annual_discount_factor ** years_from_first_year
    p["RowCostsNPV_SCT"] = p["RowCosts"] / annual_discount_factor_sct ** years_from_first_year
    p[_COST_FIELDS] = p[_COST_FIELDS].fillna(0.0)
    return p.groupby("PrgID", as_index=False).agg(
        SumCosts=("RowCosts", "sum"), SumCostsNPV=("RowCostsNPV", "sum"), SumCostsNPV_SCT=("RowCostsNPV_SCT", "sum"), **{f"Sum{f}": (f, "sum") for f in _COST_FIELDS})


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


def default_set():
    """The set a caller that names none is priced on: the newest published EE set (a run always passes the one it resolved, jobs.resolve_cost_tests)."""
    from .. import cost_test_sets
    return cost_test_sets.in_force(None)


def share_inputs(d: pd.DataFrame, cset=None) -> pd.DataFrame:
    """Per measure (aligned with `d`, the frame compute_cost_tests takes): the positive benefits each program-level share of the set divides up
    (test_evaluator.share_bases: `<T>` and `<T>Gross` for each test that shares program costs on its own benefits)."""
    cset = cset or default_set()
    return test_evaluator.share_inputs(test_evaluator.add_gross_forms(_with_gross(d), cset), cset)


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


def compute_cost_tests(d: pd.DataFrame, prog: pd.DataFrame, totals: dict[str, pd.DataFrame] | None = None, cset=None) -> pd.DataFrame:
    """
    Every aggregate and test of the cost-test set `cset` (cost_test_sets.CostTestSet; None = default_set()), priced by pipeline/test_evaluator.py, plus
    CalcCost's program-cost weighting columns. `d` (one row per measure) must carry CEInputID, PrgID, NTGRkWh_plus_MEBens, NTGRTherm_plus_MEBens and every
    stream column the set reads (EE-2026: the benefits, supply costs, OtherBen / OtherCost / TaxCredits and their Gross forms, the primary and _SCT
    participant PVs, the lifecycle bill impacts and their Gross forms, TaxCreditsGrossPV, DiscountedSavingsNetkWh / Thm). `prog` is program_cost_sums(...).
    WaterEnergyBen / WaterEnergyCost (+ _SB/_SH) enter TSB, the program-cost weighting and every cost and ratio the way CalcCE has them (Calculations CTE
    15148-15723); a frame without those columns is treated as having none.
    `totals` (share_totals over the whole job) gives the programs' benefit totals when `d` holds only part of the job (a chunked run, see recet/chunked.py);
    None = the programs' totals over `d` itself.
    Returns d plus the gross forms, the set's columns (aggregates, then each test's in Test.outputs order) and the weighting columns; the streams the set
    marks internal are dropped.
    """
    cset = cset or default_set()
    d = test_evaluator.add_gross_forms(_with_gross(d), cset)
    missing = [c for c in cset.stream_columns() if c not in d]
    if missing:
        raise KeyError(f"cost-test set {cset.id} reads column(s) the priced frame lacks: {', '.join(missing)}")
    PrgID = d["PrgID"]

    sums = ["SumCosts", "SumCostsNPV", "SumCostsNPV_SCT"] + [f"Sum{f}" for f in _COST_FIELDS]
    program_sums = prog.set_index("PrgID") if len(prog) else pd.DataFrame(columns=sums)
    new = {f"_{col}": (PrgID.map(program_sums[col]).fillna(0.0) if len(program_sums) and col in program_sums else pd.Series(0.0, index=d.index))
           for col in sums}                                          # a hand-made `prog` may lack the per-field sums
    d = d.assign(**new)

    share = test_evaluator.shares(d, cset, PrgID, totals)            # each measure's share of its program's costs, per test (CalcCE's WeightedBenefits for TRC)
    headline = cset.tests[test_evaluator.headline_share(cset)]
    share_headline = share[headline.id]
    weighting = {"WeightedBenefits": share_headline, "WeightedProgramCost": share_headline * d["_SumCosts"]}
    for f in _COST_FIELDS:                                           # CalcCost's program-cost components, each the program's nominal total x WeightedBenefits
        weighting[f"Wtd{f}"] = share_headline * d[f"_Sum{f}"]
    positive_ElecBen = _pos(d[cset.column("ElecBen", headline.pricing)])                                # CalcCE's ElecBenPos
    positive_benefits = sum(_pos(d[cset.column(s, headline.pricing)]) for s in headline.benefits)       # SumBenPos
    weighting["WeightedElecAlloc"] = (positive_ElecBen / positive_benefits.replace(0, np.nan)).fillna(0.0)
    # CET's TotalExpenditures (the run receipt's "Budget"): nominal rebates and incentives plus the measure's share of nominal program costs. Matches CET jobs
    # 121219 and 121841 to the cent in total (CalcCost's ProgramCosts = WeightedProgramCost).
    weighting["TotalExpenditures"] = (d["RebatesandIncents"] if "RebatesandIncents" in d else 0.0) + weighting["WeightedProgramCost"]

    tests = test_evaluator.evaluate(d, cset, share, weighting["WeightedElecAlloc"])
    aggregates = {a.name: None for a in cset.aggregates.values()} | {f"{a.name}Gross": None for a in cset.aggregates.values() if a.gross}
    columns = {k: v for k, v in tests.items() if k in aggregates}
    columns |= weighting
    columns |= {k: v for k, v in tests.items() if k not in aggregates}
    # CalcCost's levelized benefits (the benefits are ElecBen and GasBen in the primary pricing, as CET has them)
    columns["LevBenElec"] = test_evaluator.ratio(d["ElecBen"], d["DiscountedSavingsNetkWh"])
    columns["LevBenGas"] = test_evaluator.ratio(d["GasBen"], d["DiscountedSavingsNetThm"])
    d = d.drop(columns=[c for c in columns if c in d])               # a set's column replaces one of the same name the pricing wrote
    d = pd.concat([d, pd.DataFrame(columns, index=d.index)], axis=1)
    drop = [c for c in d.columns if c.startswith("_")] + [c for c in test_evaluator.internal_columns(cset) if c in d]
    return d.drop(columns=drop)
