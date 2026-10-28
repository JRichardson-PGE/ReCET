"""The data-dictionary attributes of a measure-level column beyond its definition (BRD BR-08.1), as the field_definitions table carries them:

    unit          the unit its entry gives ("Dollars (PV)", "kWh", "Metric Tons"), read from the definition's "Unit:" / "Units:" text
    source        where the value comes from: the Measure.csv / ProgramCost.csv input, the CEDARS Programs list, or a ReCET calculation (and on what)
    version       the version that produced it: the ReCET version, and for anything priced on the ACC, the run's ACC
    cost_tests    how the cost tests treat it: a benefit or a cost of which tests, a test's result, or not used in a cost test
    time_basis    per unit or per measure, first year, annual, lifecycle (undiscounted), present value to the First Year, nominal, or a ratio of PVs
    aggregation   how a group total (the portfolio summary, the Results levels) combines it: summed, recomputed from summed parts, or not combined
    rounding      ReCET never rounds a written value; formats only change how a number is shown
    years_in_use  which ACCs fill it (the SCT columns are blank on the TRC-only 2021 and 2022 ACCs) and, for a column renamed since, its earlier names

The rules work on the column's name and its documented unit, so a new column gets attributes without an entry here; `tests/test_field_attributes.py`
checks them against cost_tests.py's own formulas.
"""
from __future__ import annotations

import re

from . import __version__

# how cost_tests.compute_cost_tests uses each input of the tests (C11): column -> the tests it is a benefit or a cost of
_TESTS = ("TRC", "PAC", "RIM", "SCB", "SCH", "PCT")
BENEFIT_OF = {
    **{c: "TRC, PAC, RIM" for c in ("ElecBen", "GasBen", "WaterEnergyBen")},
    "OtherBen": "TRC, PAC, RIM, SCB, SCH",
    "TaxCredits": "TRC",
    **{c: "RIM" for c in ("LifecycleBillIncrElec", "LifecycleBillIncrGas")},
    **{f"{c}_SB": "SCB" for c in ("ElecBen", "GasBen", "WaterEnergyBen")},
    **{f"{c}_SH": "SCH" for c in ("ElecBen", "GasBen", "WaterEnergyBen")},
}
COST_OF = {
    "RebatesandIncentsPV": "TRC, PAC, RIM",
    "NetParticipantCostAdjustedPV": "TRC",
    **{c: "TRC, PAC, RIM" for c in ("ElecSupplyCost", "GasSupplyCost", "WaterEnergyCost")},
    "OtherCost": "TRC, PAC, RIM, SCB, SCH",
    **{c: "RIM" for c in ("LifecycleBillReducElec", "LifecycleBillReducGas")},
    **{c: "SCB, SCH" for c in ("RebatesandIncentsPV_SCT", "NetParticipantCostAdjustedPV_SCT")},
    **{f"{c}_SB": "SCB" for c in ("ElecSupplyCost", "GasSupplyCost", "WaterEnergyCost")},
    **{f"{c}_SH": "SCH" for c in ("ElecSupplyCost", "GasSupplyCost", "WaterEnergyCost")},
}
_ACC_PRICED = ("ElecBen", "ElecSupplyCost", "GasBen", "GasSupplyCost", "WaterEnergy", "ElecValue_", "GasValue_")
SCT_ONLY = re.compile(r"(_SB|_SH|_SCT)$|^SC[BH]")          # filled only when the ACC has SCT avoided costs (2024 ACC on)


def unit(definition: str, column: str) -> str:
    """The unit an entry gives: "Unit: X." or, for an entry over several columns, "Units: Col X; Col2 Y"."""
    m = re.search(rf"Units: (?:[^.;]*?;\s*)*?{re.escape(column)} ([^;.]+)", definition)
    if m:
        return m.group(1).strip()
    m = re.search(r"Unit: (.+?)\.(?:\s|$)", definition)
    return m.group(1).strip() if m else ""


def _source(column: str, inputs: set[str]) -> str:
    if column in inputs:
        return "input: Measure.csv"
    if column.startswith("Prg_"):
        return "CEDARS Programs list (built in or supplied), matched on PrgID"
    if column.startswith("Wtd") or column in ("WeightedProgramCost", "TotalExpenditures"):
        return "calculated: ProgramCost.csv totals shared over the program's measures"
    if column.startswith(_ACC_PRICED):
        return "calculated: savings x 8,760 (gas: quarterly) profile x ACC avoided costs"
    if re.search(r"(CO2|NOx|PM10)$", column):
        return "calculated: savings x profile x emission factors (ACC and CET tables)"
    if column.startswith(("LifecycleBill",)):
        return "calculated: savings x retail rates (run parameters)"
    if column.startswith(("Used_", "AC_Net_", "Fuel_Sub", "Scaled_Profile", "Credited")) or column.endswith(("Status", "Source", "ZoneSource")):
        return "calculated from the Measure.csv inputs (how the record was read and priced)"
    return "calculated from the inputs and the outputs above it (OUTPUT_FIELDS.md)"


def _cost_tests(column: str) -> str:
    if column in BENEFIT_OF:
        return f"benefit in {BENEFIT_OF[column]}"
    if column in COST_OF:
        return f"cost in {COST_OF[column]}"
    m = re.match(rf"^({'|'.join(_TESTS)})(Ratio|RatioNoAdmin|Ben|Cost|CostNoAdmin|CostGross)$", column)
    if m:
        what = {"Ratio": "result", "RatioNoAdmin": "result without program administrative costs", "Ben": "numerator (benefits)",
                "Cost": "denominator (costs)", "CostNoAdmin": "denominator without program administrative costs",
                "CostGross": "costs on gross savings (reported, not a ratio's denominator)"}[m.group(2)]
        return f"{m.group(1)} {what}"
    if column.startswith(("ElecValue_", "GasValue_")):
        return f"a component of {column.split('Value_')[0]}Ben (the ACC component's share); not an input of a ratio on its own"
    if column == "WeightedBenefits":
        return "spreads the program's PV costs (TRC, PAC, RIM; the SCT tests use their own shares) over its measures"
    if column.startswith("Lev") or column.endswith("LifecycleNetBen"):
        return "reported from the TRC / PAC / RIM terms; not an input of a ratio"
    if column == "TotalSystemBenefit" or column == "TotalSystemBenefitGross":
        return "reported (benefits less supply costs); not an input of a ratio"
    return "not used in a cost test"


def _time_basis(column: str, u: str) -> str:
    low = u.lower()
    if "Ratio" in column:
        return "ratio of lifecycle present values"
    if column.startswith("Lev"):
        return "levelized: lifecycle PV per discounted lifecycle kWh or therm"
    if column.startswith("FirstYear"):
        return "first year of the measure's life"
    if column.startswith("Annual"):
        return "average year of the measure's life"
    if column.startswith("DiscountedSavings"):
        return "lifecycle, each year discounted to the First Year"
    if column.startswith("Lifecycle") and not column.startswith("LifecycleBill"):
        return "lifecycle total, undiscounted"
    if "pv" in low or column.endswith(("PV", "PV_SCT")) or column.startswith("LifecycleBill"):
        return "lifecycle present value to the run's First Year" + (" (the other benefits and costs undiscounted)" if "other" in low else "")
    if "nominal" in low:
        return "nominal dollars, not discounted"
    if "undiscounted" in low:
        return "lifecycle total, undiscounted"
    if column.startswith(("Used_", "AC_Net_")) and "Year" not in column and "Quarter" not in column:
        return "per year, first-year values as priced"
    return ""


def _aggregation(column: str, u: str = "") -> str:
    from . import results_view as rv
    if u.lower() == "text" or column.endswith(("Status", "Source", "PA", "Flag", "MatchedOn")):
        return "not combined (an input, identifier or label)"
    if "Ratio" in column or column.startswith("Lev"):
        return "recomputed from the summed numerator and denominator (never summed or averaged)"
    if column in rv.NOT_ADDITIVE:
        return "not combined (a per-measure share)"
    if rv._summable(column):
        return "summed over the measures"
    return "not combined (an input, identifier or label)"


def attributes(column: str, definition: str = "", acc: str = "") -> dict:
    """The column's data-dictionary attributes (the module docstring)."""
    from . import fieldnames, templates
    inputs = set(templates.MEASURE_COLUMNS)
    u = unit(definition, column)
    priced = column.startswith(_ACC_PRICED + ("TotalSystemBenefit", "Lev")) or bool(re.search(r"(CO2|NOx|PM10)$", column)) \
        or bool(re.match(rf"^({'|'.join(_TESTS)})(Ratio|Ben|Cost|LifecycleNetBen)", column))
    version = f"ReCET {__version__}" + (f"; {acc}" if acc and priced and column not in inputs else "")
    if column in inputs:
        version = "as filed (CET input field)"
    former = sorted(old for old, new in fieldnames.OUTPUT_RENAMES.items() if new == column)
    former += sorted(old for old, new in fieldnames.LEGACY_TO_RECET.items() if new == column)
    years = ("2024 ACC on; blank on the TRC-only 2021 and 2022 ACCs" if SCT_ONLY.search(column) and column not in inputs else "every ACC (2021-2026)")
    if former:
        years += "; earlier name " + ", ".join(former)
    return {"unit": u, "source": _source(column, inputs), "version": version, "cost_tests": _cost_tests(column),
            "time_basis": _time_basis(column, u), "aggregation": _aggregation(column, u),
            "rounding": "not rounded (full precision as calculated)", "years_in_use": years}


ATTRIBUTE_COLUMNS = ["unit", "source", "version", "cost_tests", "time_basis", "aggregation", "rounding", "years_in_use"]
