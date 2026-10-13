"""ACC versions: which electric and gas avoided-cost files a run prices against, and which E3 tables (discount rates, emissions, rate schedules) go with it.

Two versions ship with the package:

  key "2024"  2024 ACC             CET "Version 2026"   electric: reference folder acc/2024/electric_hourly.parquet   gas: packaged
  key "2026"  2026 ACC (final v1b) CET "Version 2027"   electric: reference folder acc/2026/electric_hourly.parquet   gas: packaged

A run names a version key. Files supplied with the run REPLACE the shipped file for that fuel (matching label -> the supplied file is used in place
of the built-in one). A label the package does not have ("2028", "2026-final" ...) is a NEW version made entirely of the supplied files; any fuel not
supplied falls back to the newest built-in version's file, with a note. The E3 tables (E3Settings, E3Emissions, rate schedules) carry their own
`Version` column: the built-in versions use their CET version, and a new version borrows `e3_version` (default: the newest in the tables), since
the tables for a brand-new ACC do not exist yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class AccVersion:
    key: str
    label: str
    cet_version: int            # the E3 tables' Version column
    electric_key: str           # paths.REF_FILES key
    gas_rel: tuple[str, ...]    # inside the package data folder


BUILTIN: dict[str, AccVersion] = {v.key: v for v in (
    AccVersion("2024", "2024 ACC (CET Version 2026)", 2026, "acc_electric_2024", ("acc", "2024", "gas_components.parquet")),
    AccVersion("2026", "2026 ACC (CET Version 2027)", 2027, "acc_electric_2026", ("acc", "2026", "gas_components.parquet")),
)}
NEWEST_BUILTIN = "2026"


@dataclass
class ResolvedAcc:
    key: str
    label: str
    cet_version: int
    electric: Path | None
    gas: Path | None
    electric_source: str = ""
    gas_source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def uses_builtin_electric(self) -> bool:
        return self.electric_source.startswith("built-in")

    def describe(self) -> str:
        return (f"ACC version {self.key!r} ({self.label}); electric: {self.electric_source} [{self.electric}]; gas: {self.gas_source} [{self.gas}]; "
                f"E3 tables Version {self.cet_version}")


def known_keys() -> list[str]:
    return list(BUILTIN)


def choices() -> list[tuple[str, str]]:
    """[(key, label)] for the GUI's drop-down."""
    return [(v.key, v.label) for v in BUILTIN.values()]


def _e3_versions() -> list[int]:
    from .pipeline import discount_settings
    return discount_settings.known_versions()


def resolve_acc(key: str, electric_file: str | Path | None = None, gas_file: str | Path | None = None, e3_version: int | None = None) -> ResolvedAcc:
    """Decide the files for a run. `electric_file` / `gas_file` are already-prepared parquet paths (overrides.prepare_*), or None."""
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
        return ResolvedAcc(key, known.label, e3_version or known.cet_version, electric, gas, esrc, gsrc, notes)

    if electric_file is None and gas_file is None:
        raise ValueError(f"ACC version {key!r} is not one the package ships ({', '.join(BUILTIN)}); supply an electric and/or gas ACC file to define it")
    base = BUILTIN[NEWEST_BUILTIN]
    e3 = e3_version or max(_e3_versions())
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
    notes.append(f"new ACC version {key!r} uses the E3 tables' Version {e3} (discount rates, emissions, rate schedules)")
    return ResolvedAcc(key, f"Custom ACC '{key}'", e3, electric, gas, esrc, gsrc, notes)
