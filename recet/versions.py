"""ACC versions: which electric and gas avoided-cost files a run prices against, and which run parameters and E3 tables (discount rates, emissions, rate
schedules) go with it.

Four versions ship with the package, each named by its ACC:

  key "2021"  2021 ACC   electric: reference folder acc/2021/electric_hourly.parquet   gas: packaged   TRC only, 2020 hourly calendar
  key "2022"  2022 ACC   electric: reference folder acc/2022/electric_hourly.parquet   gas: packaged   TRC only, 2020 hourly calendar
  key "2024"  2024 ACC   electric: reference folder acc/2024/electric_hourly.parquet   gas: packaged   TRC, SCT Base, SCT High; 2018 calendar
  key "2026"  2026 ACC   electric: reference folder acc/2026/electric_hourly.parquet   gas: packaged   TRC, SCT Base, SCT High; 2018 calendar
                         (the final v1b release)

A run names a version key. Files supplied with the run REPLACE the shipped file for that fuel (matching label -> the supplied file is used in place
of the built-in one). A label the package does not have ("2028", "2026-draft" ...) is a NEW version made entirely of the supplied files; any fuel not
supplied falls back to the newest built-in version's file, with a note.

Two labels name every version (BRD BR-11.7): the ACC version, the year E3's calculator was released (2024 ACC), and CET's Avoided Cost Version, the first year
that ACC was in effect for claims or forecasts (Avoided Cost Version 2026). Labels read "2024 ACC (Avoided Cost Version 2026)".

Run parameters and E3 tables are picked by ACC version too. CET numbers its avoided-cost versions differently (its "Version 2026" holds the 2024 ACC),
so the packaged tables, which are CET exports, are mapped onto ACC versions when they are read (CET_VERSION_TO_ACC, from each version's release date in
RunParameters AvoidedCostVersion). A new version borrows the tables of `settings_from` (an ACC version; default: the newest built-in), since the tables
for a brand-new ACC do not exist yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# CET avoided-cost version -> the ACC it holds. Release dates (RunParameters AvoidedCostVersion): 2018 = 9/1/2017, 2019 = 6/28/2018, 2020 = 9/15/2019,
# 2021 = 6/24/2020, 2022 = 8/28/2021, 2024 = 9/26/2022, 2026 = 12/12/2024, 2027 = 5/25/2026 (the draft 2026 ACC; the final v1b uses the same tables).
# Confirmed by Jake for 2022, 2024, 2026 and 2027. CET versions 2013 and 2017 both carry 2011 dates and are not mapped (not selectable).
CET_VERSION_TO_ACC = {2018: 2017, 2019: 2018, 2020: 2019, 2021: 2020, 2022: 2021, 2024: 2022, 2026: 2024, 2027: 2026}


# ACC year -> CET's Avoided Cost Version (the first year the ACC is in effect for claims or forecasts). The same as CET's table numbers above, except the 2026 ACC:
# CET's draft tables call it Version 2027, but it takes effect as Avoided Cost Version 2028 (Jake, 2026-10-22).
AVOIDED_COST_VERSION = {**{acc: cet for cet, acc in CET_VERSION_TO_ACC.items()}, 2026: 2028}


def avoided_cost_version(acc) -> int | None:
    """CET's Avoided Cost Version for an ACC year (the 2024 ACC -> 2026), or None if CET has no version for it."""
    try:
        return AVOIDED_COST_VERSION.get(int(acc))
    except (TypeError, ValueError):
        return None


def acc_label(acc: int) -> str:
    """'2024 ACC (Avoided Cost Version 2026)': both names of a version."""
    cv = avoided_cost_version(acc)
    return f"{acc} ACC" + (f" (Avoided Cost Version {cv})" if cv else "")


def acc_of_cet_version(cet_version) -> int | None:
    """The ACC a CET avoided-cost version number holds, or None if it is not mapped."""
    try:
        return CET_VERSION_TO_ACC.get(int(cet_version))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AccVersion:
    key: str
    label: str
    acc: int                    # the ACC year: picks the run parameters and E3 tables
    electric_key: str           # paths.REF_FILES key
    gas_rel: tuple[str, ...]    # inside the package data folder


BUILTIN: dict[str, AccVersion] = {v.key: v for v in (
    AccVersion("2021", acc_label(2021), 2021, "acc_electric_2021", ("acc", "2021", "gas_components.parquet")),
    AccVersion("2022", acc_label(2022), 2022, "acc_electric_2022", ("acc", "2022", "gas_components.parquet")),
    AccVersion("2024", acc_label(2024), 2024, "acc_electric_2024", ("acc", "2024", "gas_components.parquet")),
    AccVersion("2026", acc_label(2026), 2026, "acc_electric_2026", ("acc", "2026", "gas_components.parquet")),
)}
NEWEST_BUILTIN = "2026"


@dataclass
class ResolvedAcc:
    key: str
    label: str
    settings_acc: int           # the ACC whose run parameters and E3 tables the run uses
    electric: Path | None
    gas: Path | None
    electric_source: str = ""
    gas_source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def avoided_cost_version(self) -> int | None:
        """CET's Avoided Cost Version of a built-in ACC; None for a new version defined by supplied files."""
        return avoided_cost_version(self.key) if self.key in BUILTIN else None

    @property
    def uses_builtin_electric(self) -> bool:
        return self.electric_source.startswith("built-in")

    def describe(self) -> str:
        return (f"ACC version {self.key!r} ({self.label}); electric: {self.electric_source} [{self.electric}]; gas: {self.gas_source} [{self.gas}]; "
                f"run parameters and E3 tables of the {self.settings_acc} ACC")


def known_keys() -> list[str]:
    return list(BUILTIN)


def choices() -> list[tuple[str, str]]:
    """[(key, label)] for the GUI's drop-down."""
    return [(v.key, v.label) for v in BUILTIN.values()]


def _settings_accs() -> list[int]:
    from .pipeline import discount_settings
    return discount_settings.known_versions()


def resolve_acc(key: str, electric_file: str | Path | None = None, gas_file: str | Path | None = None, settings_from: int | None = None) -> ResolvedAcc:
    """Decide the files for a run. `electric_file` / `gas_file` are already-prepared parquet paths (overrides.prepare_*), or None. `settings_from` (an ACC
    version) overrides whose run parameters and E3 tables are used."""
    key = str(key).strip()
    if not key:
        raise ValueError("no ACC version given")
    known = BUILTIN.get(key)
    notes: list[str] = []
    if known is not None:
        electric_file, gas_file = (Path(electric_file) if electric_file else None), (Path(gas_file) if gas_file else None)
        if electric_file is not None:
            electric, esrc = electric_file, f"supplied file replaces the built-in {key} electric ACC"
        else:
            electric, esrc = paths.ref_path(known.electric_key), "built-in"
        if gas_file is not None:
            gas, gsrc = gas_file, f"supplied file replaces the built-in {key} gas ACC"
        else:
            gas, gsrc = paths.package_data(*known.gas_rel), "built-in"
        return ResolvedAcc(key, known.label, int(settings_from or known.acc), electric, gas, esrc, gsrc, notes)

    if electric_file is None and gas_file is None:
        raise ValueError(f"ACC version {key!r} is not one the package ships ({', '.join(BUILTIN)}); supply an electric and/or gas ACC file to define it")
    base = BUILTIN[NEWEST_BUILTIN]
    settings = int(settings_from or base.acc)
    if electric_file is not None:
        electric, esrc = Path(electric_file), f"supplied file (new version {key})"
    else:
        electric, esrc = paths.ref_path(base.electric_key), f"built-in {base.key} (no electric file supplied for {key})"
        notes.append(f"no electric ACC supplied for new version {key!r}: using the built-in {base.key} electric ACC")
    if gas_file is not None:
        gas, gsrc = Path(gas_file), f"supplied file (new version {key})"
    else:
        gas, gsrc = paths.package_data(*base.gas_rel), f"built-in {base.key} (no gas file supplied for {key})"
        notes.append(f"no gas ACC supplied for new version {key!r}: using the built-in {base.key} gas ACC")
    notes.append(f"new ACC version {key!r} uses the run parameters and E3 tables of the {settings} ACC (discount rates, emissions, rate schedules)")
    return ResolvedAcc(key, f"Custom ACC '{key}'", settings, electric, gas, esrc, gsrc, notes)
