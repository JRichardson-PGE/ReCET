"""The two worked examples the documentation quotes (docs/OUTPUT_FIELDS.md, docs/CALCULATIONS.md C18): small jobs on flat avoided costs and flat load shapes, so every number
can be checked with a calculator, run through the real tool. tests/test_docs.py compares the numbers printed in the documents with what these runs give.

  A   a residential efficiency measure: single baseline, fractional life, no market effects, no fuel substitution.
  B   a commercial gas-to-electric heat pump: fuel substitution (added electric load and water use, saved gas), a second baseline that starts part-way through a quarter,
      a monthly gas shape, electric NOx / PM10 / gas NOx rows, and 5% market effects on benefits and on costs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import test_calcs as tc
from recet.jobs import RunConfig, run_job

E3 = 2027                                                       # the Version of the E3 tables the 2026 ACC uses
FLAT = np.arange(8760)
ENERGY = {"TRC": 70.0, "SCT Base": 78.0, "SCT High": 86.0}      # $/MWh; transmission 5 and distribution 5 are added to every row (total 80 / 88 / 96)
GAS_CO2 = {("TRC", ""): 0.10, ("SCT", "Base"): 0.30, ("SCT", "High"): 0.50}
GAS_MARKET = {"Residential": 0.60, "Commercial": 0.50}          # $/therm, every month; td 0.20 is added in every month
YEARS = (2028, 2029, 2030)

A_MEASURE = dict(PrgID="PRG1", ClaimYearQuarter="2028Q3", Sector="Res", AC_Climate_Zone="13", AC_Gas_Impact_Profile="Annual", AC_Gas_Target_Sector="Residential", AC_Elec_Impact_Profile="W_SHAPE",
                 AC_Elec_Target_Sector="Residential", IOU_AC_Territory="PGE", EUL_Yrs="2.4", RUL_Yrs="0", NumUnits="100", UnitkWh1stBaseline="500", UnitkW1stBaseline="0.2",
                 UnitTherm1stBaseline="20", UnitkWhIOUWater1stBaseline="3", NTGRkWh="0.8", NTGRkW="0.8", NTGRTherm="0.9", NTGRCost="0.9", InstallationRatekWh="0.95",
                 RealizationRatekWh="0.9", InstallationRateTherm="1", RealizationRateTherm="0.9", UnitMeaCost1stBaseline="200", UnitIncentiveToOthers="30",
                 UnitEndUserRebate="20", UnitDirectInstallLab="5", UnitDirectInstallMat="5", UnitRefrigCosts="1", UnitMiscBens="2", UnitTaxCredits="5",
                 MeasDescription="worked example A")
A_PROGRAM = [dict(PrgID="PRG1", PA="PGE", PrgYear="2028", ClaimYearQuarter="2028Q1", AdminCostsOverheadAndGA="1500", MarketingOutreach="500"),
             dict(PrgID="PRG1", PA="PGE", PrgYear="2029", ClaimYearQuarter="2029Q1", EMV="1000")]

B_END_USE = "3 = Commercial HVAC"                               # a key that the Emissions table has PG&E commercial CZ13 NOx / PM10 rows for (0.000046 lb/kWh each)
B_MEASURE = dict(PrgID="PRG2", ClaimYearQuarter="2028Q2", Sector="Com", AC_Climate_Zone="13", AC_Gas_Impact_Profile="Winter Only", AC_Gas_Target_Sector="Commercial", AC_Elec_Impact_Profile=B_END_USE,
                 AC_Elec_Target_Sector="Commercial", IOU_AC_Territory="PGE", EUL_Yrs="2.5", RUL_Yrs="1.125", NumUnits="10", UnitkWh1stBaseline="-4000", UnitkWh2ndBaseline="-2500",
                 UnitTherm1stBaseline="600", UnitTherm2ndBaseline="400", UnitkWhIOUWater1stBaseline="-6", UnitkWhIOUWater2ndBaseline="-4", NTGRkWh="0.75", NTGRkW="0.75",
                 NTGRTherm="0.85", NTGRCost="0.8", InstallationRatekWh="1", RealizationRatekWh="0.9", InstallationRateTherm="1", RealizationRateTherm="0.95",
                 UnitMeaCost1stBaseline="9000", UnitMeaCost2ndBaseline="6000", MeasInflation="0.02", UnitIncentiveToOthers="800", UnitEndUserRebate="400",
                 UnitDirectInstallLab="100", UnitDirectInstallMat="50", UnitGasInfraBens="20", UnitRefrigCosts="10", UnitRefrigBens="8", UnitMiscCosts="5", UnitMiscBens="12",
                 UnitTaxCredits="100", FuelID="FuelSub-ToElec", MeasImpactType="Deem-FuelSub", CombustionType="Small Boilers (<100 MMBtu/hr Heat Input):Uncontrolled",
                 MeasDescription="worked example B")
B_PROGRAM = [dict(PrgID="PRG2", PA="PGE", PrgYear="2028", ClaimYearQuarter="2028Q1", EMV="900", DIActivity="300"),
             dict(PrgID="PRG2", PA="PGE", PrgYear="2029", ClaimYearQuarter="2029Q1", MarketingOutreach="600")]


# the program metadata both examples run with (a supplied programs file: PRG1 and PRG2 are not in the built-in CEDARS list)
_FLAGS = ("Resource_Flag", "NonResource_Flag", "Deemed_Flag", "Custom_Flag", "Upstream_Flag", "Midstream_Flag", "Downstream_Flag", "Audit_Flag", "Exclude_From_Budget",
          "Exclude_From_CE", "Exclude_From_Statewide_Flag")
PROGRAMS = [
    dict(PA="PGE", PrgID="PRG1", ProgramName="Example Residential Program", ProgramStatus="active", ProgramSegment="resource_acquisition", StatewideProgram="",
         PrimarySector="Residential", Sector="Residential (Res)", BPSector="Residential: 1.000000", ProgramImplementer="IOU Core", ProgramCategory="Other",
         ImplementationContractor="", StartYear="2024", EndYear="", DirectInstall="No", Financing="None", ParentProgram="Residential Programs",
         **{f: "True" if f in ("Resource_Flag", "Deemed_Flag", "Downstream_Flag") else "False" for f in _FLAGS}),
    dict(PA="PGE", PrgID="PRG2", ProgramName="Example Commercial Program", ProgramStatus="new", ProgramSegment="market_support", StatewideProgram="",
         PrimarySector="Commercial", Sector="Commercial (Com)", BPSector="Commercial: 1.000000", ProgramImplementer="Third-Party Solicited",
         ProgramCategory="Market Transformation", ImplementationContractor="Example Implementer", StartYear="2027", EndYear="2031", DirectInstall="Partial",
         Financing="OBF", ParentProgram="Commercial Third Party Programs",
         **{f: "True" if f in ("NonResource_Flag", "Custom_Flag", "Midstream_Flag", "Audit_Flag") else "False" for f in _FLAGS}),
]


@dataclass(frozen=True)
class Example:
    measure: dict
    program: list
    market_effects: float = 0.0


EXAMPLES = {"A": Example(A_MEASURE, A_PROGRAM), "B": Example(B_MEASURE, B_PROGRAM, market_effects=0.05)}


def write_inputs(folder: Path, ex: Example) -> None:
    tc.write_measures(folder / "Measure.csv", {"W1": ex.measure})
    (folder / "ProgramCost.csv").write_text("\n".join(["|".join(tc.PROGRAM_HEADER)] + ["|".join(r.get(c, "") for c in tc.PROGRAM_HEADER) for r in ex.program]) + "\n")
    frames = [pd.DataFrame({"iou": "PG&E", "climate_zone": "CZ13", "cost_test": t, "year": y, "hour_of_year": FLAT + 1, "energy": e, "transmission": 5.0, "distribution": 5.0,
                            "total_avoided_cost": e + 10.0, "emission_rate_tonnes_per_kwh": 0.0002}) for t, e in ENERGY.items() for y in YEARS]
    pd.concat(frames, ignore_index=True).to_parquet(folder / "acc.parquet", index=False)
    gas = []
    for (test, scc), co2 in GAS_CO2.items():
        for cls, market in GAS_MARKET.items():
            for y in YEARS:
                row = {"utility": "PG&E", "class": cls, "cost_test": test, "scc": scc, "year": y, "nox": 0.05, "co2": co2, "upstream_methane": 0.03, "behind_meter_methane": 0.0,
                       "air_quality_adder": 0.02}
                for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"):
                    row[f"market_{m}"], row[f"td_{m}"] = market, 0.20
                gas.append(row)
    pd.DataFrame(gas).to_csv(folder / "gas.csv", index=False)
    frames = [pd.DataFrame({"end_use_name": eu, "pa": "PGE", "target_sector": sector, "climate_zone": "ALL", "hour_of_year": FLAT + 1, "impact": 1 / 8760})
              for eu, sector in (("W_SHAPE", "Residential"), (B_END_USE, "Commercial"))]
    pd.concat(frames, ignore_index=True).to_csv(folder / "p.csv", index=False)
    pd.DataFrame(PROGRAMS).to_csv(folder / "programs.csv", index=False)


def run_example_result(folder: Path, key: str, **extra):
    """The whole job result of example `key` (`.frame`, `.files`); `extra` are further run settings (components=True, portfolio_8760=True, profile_years=[...])."""
    ex = EXAMPLES[key]
    write_inputs(folder, ex)
    cfg = RunConfig(measures=str(folder / "Measure.csv"), programcost=str(folder / "ProgramCost.csv"), out_dir=str(folder / "out"), run_name="w", acc_version="2026",
                    acc_electric_file=str(folder / "acc.parquet"), acc_gas_file=str(folder / "gas.csv"), profiles_8760_file=str(folder / "p.csv"),
                    programs_file=str(folder / "programs.csv"), market_effects=ex.market_effects, **extra)
    return run_job(cfg, log=lambda s: None)


def run_example(folder: Path, key: str) -> pd.Series:
    """The one measure-level row (all output columns) the tool writes for example `key`."""
    return run_example_result(folder, key).frame.iloc[0]
