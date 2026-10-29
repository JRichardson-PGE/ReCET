"""The CET releases ReCET reproduces, and how each one calculates.

Each `Release` names a CET build from a CPUC database backup (CEDARS-Research `reference-docs/cet_versions/`) and carries the
switches the replica needs to price exactly as that build's RunCET does, CET's errors included (Jake, 2026-10-29: prior
versions are exact replicas, labelled). Every switch says which releases it applies to; `notes/cet_prior_versions.md` in
the research repository has the evidence (the SQL lines) for each.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Release:
    name: str                 # "25.1"
    backup: str               # the database backup it was read from
    built: str                # the backup's date
    # --- InputMeasurevw / MappingMeasurevw
    fuel_sub_types: tuple[str, ...] = ()        # FuelType values that make a measure fuel substitution (#FuelTypesForFuelSubstitution)
    fuel_sub_impact_types: tuple[str, ...] = ()  # before 25.0: MeasImpactType endings that do (LIKE '%FuelSub', '%NC-AE')
    fuel_type_from_impact: bool = False          # 25.1: a blank FuelType with MeasImpactType LIKE '%FuelSub' is 'FuelSub-ToElec'
    tax_credits: bool = False                    # 25.0+: UnitTaxCredits is priced (TaxCredits, TRC ratio)
    trim_program_keys: bool = False              # 25.1: InputProgramvw strips CR/LF and spaces from PrgID and PA
    qm_one_based: bool = False                   # 21.0: InputMeasurevw's Qm = (year - FirstYear) x 4 + quarter (later: less 1)
    water: bool = True                           # 24.1+: water-energy kWh are priced (before, the inputs are not read)
    # --- CalcCE
    calc_ce: str = "2022"                        # "2021": CET 21.0's CalcCE (calc_ce_2021); "2022": every later release's
    lifetime_sums: str = "plain"                 # "guarded" (22.1 and before): see calc_ce._lifetime_sums
    sign_test: str = "priced"                    # which sign decides a fuel-substitution measure's benefit or supply cost:
                                                 # "priced" (24.3+: kWh1 x ace_1.Gen + kWh2 x ace_2.Gen) or "first_baseline" (to 24.2: kWh1)
    misc_other: bool = True                      # 24.3+: UnitMiscBens / UnitMiscCosts are in OtherBen / OtherCost
    sct: str = "none"                            # "none"; "elec" (25.0: electric SB/SH only, TRC discount rate); "full" (25.1)
    sct_elec_versions: tuple[str, ...] = ()      # 25.0: AvoidedCostElecvw passes Gen_SB.. only for these Versions, else 0
    bills: str = "net"                           # "net" (one bill-reduction sum); "split" (25.1: reductions and increases)
    # --- CalcSavings
    savings: str = "2024"                        # "2024" (24.1+: site / water split) or "2022" (22.1 and before: goal attainment)
    savings_dual_eul_error: bool = False         # 24.1: the dual-baseline lifecycle savings are multiplied by EUL once more
    # --- CalcEmissions
    emissions_water: bool = False                # 24.1-25.0: the electric emission quantity adds the water-energy kWh
    nox_gas_lookup: bool = False                 # 21.0, 22.1, 25.1: gas NOx from E3CombustionType by CombustionType (none: 0); 24.x-25.0 a constant
    nox_gas_constant: float = 0.0092
    # --- E3RateScheduleElecvw / Gasvw
    rate_yr1_base: int | None = None             # 25.1: Yr1 = claim year - 2024 (hard-coded); None: Yr1 = Qy (claim year - FirstYear)
    elec_rate_by_target_sector: bool = False     # 25.1: map every PA's target sector; before: PGE and SDGE by sector, others 'All'
    gas_named_schedule: bool = False             # 25.1: a measure's own RateScheduleGas is used when given
    gas_named_excluded: bool = False             # 21.0: a measure that names a RateScheduleGas gets no gas rates at all
    # --- SaveCE / SaveCost ...: columns the Save procedure leaves out of its insert list, so they are always NULL
    saved_blank: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def label(self) -> str:
        return f"CET {self.name} replica"


RELEASES: dict[str, Release] = {r.name: r for r in [
    Release(
        name="25.1", backup="CEDARS_CET_v25.120261007.bak", built="2026-10-07",
        fuel_sub_types=("AllElec-New", "FuelSub-ToElec", "FuelSub-ToGas"), fuel_type_from_impact=True, tax_credits=True,
        trim_program_keys=True, sct="full", bills="split", nox_gas_lookup=True, rate_yr1_base=2024,
        elec_rate_by_target_sector=True, gas_named_schedule=True,
    ),
    Release(
        name="25.0", backup="CEDARS_CET_25_0.bak", built="2025-05-18",
        fuel_sub_types=("AllElec-New", "FuelSub-ToElec", "FuelSub-ToGas-Ex"), tax_credits=True, sct="elec",
        sct_elec_versions=("2026",), emissions_water=True,
        saved_blank=(("SavedCost", "LevSCBCostNoAdmin"), ("SavedCost", "LevSCHCostNoAdmin")),   # CET error kept
    ),
    Release(
        name="24.3", backup="WIN-B2737UGDAJ5_CEDARS_CET_Module_Staging_FULL_COPY_ONLY_20240828_055105.bak", built="2024-08-28",
        fuel_sub_impact_types=("FuelSub", "NC-AE"), emissions_water=True,
    ),
    Release(
        name="24.2", backup="CEDARS_CET_Module_Staging (zip named 20240621)", built="2023-09-20",
        fuel_sub_impact_types=("FuelSub", "NC-AE"), emissions_water=True, sign_test="first_baseline", misc_other=False,
    ),
    Release(
        name="24.1", backup="CEDARS_CET_Module_Staging", built="2023-05-16",
        fuel_sub_impact_types=("FuelSub", "NC-AE"), emissions_water=True, sign_test="first_baseline", misc_other=False,
        savings_dual_eul_error=True,
    ),
    Release(
        name="22.1", backup="CEDARS_CET_Module_Staging", built="2021-12-09",
        fuel_sub_impact_types=("FuelSub",), water=False, sign_test="first_baseline", misc_other=False,
        lifetime_sums="guarded", savings="2022", nox_gas_lookup=True,
    ),
    Release(
        name="21.0", backup="CET_2021", built="2020-08-24",
        qm_one_based=True, water=False, calc_ce="2021", savings="2022", nox_gas_lookup=True, gas_named_excluded=True,
    ),
]}


def get(name: str) -> Release:
    try:
        return RELEASES[str(name)]
    except KeyError:
        raise ValueError(f"unknown CET release {name!r}; known: {', '.join(RELEASES)}") from None
