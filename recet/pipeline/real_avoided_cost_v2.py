"""
Real, fully-hourly electric avoided costs for ANY pricing year (2024-2054),
pulled straight from the validated ACC2024 hourly extraction built for
`CEDARS QAC Validation/` (`2024 ACC Extraction/output/ACC2024_electric_
avoided_costs_hourly.parquet`) -- the same source that project confirmed
reproduces CEDARS' own quarterly ground truth to an exact match (5,965,764
comparison rows) via the correct day-of-week-shift methodology (see that
project's `pipeline.py`).

This replaces the first proof of concept's real_avoided_cost_full.py (now in the CEDARS review research repository's poc/research_lib), whose approach was (extracting
components from the raw 84MB Electric Model .xlsb and reconstructing
generation-capacity/transmission/distribution by reusing ONE shared hourly
allocation shape across all three) with the ACC's OWN real per-hour,
per-climate-zone, per-year totals -- no allocation-shape approximation, no
flat annual-average GHG rate. real_avoided_cost_full.py's README-documented
~20% overshoot on the large/diverse dataset was traced to exactly those
approximations; this module exists to test whether removing them closes
that gap. The older module and its run_poc.py --acc-mode comparison have since moved
to the research repository.

`total_avoided_cost` in that extraction already IS energy + generation
capacity + transmission + distribution + GHG adders + losses + methane
leakage, summed by the extraction itself (confirmed: exactly equals
summing every individual component column, to float noise) -- no manual
stacking needed here. Divided by 1000 to match this POC's existing $/kWh-
per-unit-of-normalized-profile convention (engine.py) -- the same /1000 CEDARS QAC Validation/pipeline.py uses, and
for the same reason: the extraction's dollar columns are $/MWh-denominated.

Electric ACC covers PG&E/SCE/SDG&E only (confirmed: no SCG rows in the
extraction -- it's a gas-only utility). SCG-territory electric measures
need a real proxy utility -- and per Appendix C of "Custom Measure Load
Shape Tools Documentation" (DNV, CPUC, 7/31/2025), that proxy is NOT
always SCE (an earlier version of this module used a blanket SCG->SCE
mapping, found and corrected 2026-09-16 after a live parallel CET run on
a real 73,024-measure filing showed a huge, precisely-CZ05-shaped
outlier): "SCE sells electricity in most of SCG territory except climate
zones 04, 05 and 13 where PG&E is the utility selling electricity to
their customers." So SCG measures in CZ04/05/13 resolve to PG&E; every
other SCG measure still resolves to SCE.

SCG ROUTING, CURRENT (2026-10-09, Jake): ONE utility's whole bundle per zone -- generation, T&D and emission rate all from SCE, or all from PG&E,
never mixed. SCE is the default; PG&E is used only in zones where the ACC has no native SCE data (_scg_iou). On both built-in ACCs that is SCE in 6, 8, 9,
10, 13, 14, 15, 16 and PG&E in 1, 2, 3A, 3B, 4, 5, 11, 12 (CZ5 is PG&E because SCE's CZ5 is only a documented stand-in for CZ9). This SUPERSEDES the
2026-09-22 routing described further down (SCE generation everywhere + PG&E T&D in 4/5/13), which reproduced CEDARS' own 8,760-path SCG files but mixed
two utilities in one zone: once the System climate zone began choosing the lowest-cost zone (pipeline/system_zones.py), those mixed zones -- cheap SCE
generation plus cheap PG&E T&D -- came out 20-30% below every real zone and won for nearly every SCG profile. The new routing equals CEDARS' DEER-path SCG
values for zones 4/5 (PG&E) and 13 (SCE), and departs from CEDARS' 8,760-path SCG files (SCE generation) in zones 4 and 5 and from their T&D in zone 13.
Everything below about SCG is history.

NOTE 2026-10-02 (Jake: the POC uses the 8,760 path only): CET's quarterly SCG table is no longer used anywhere in the POC's pricing; every SCG
electric measure is priced here, on the hourly ACC, with the SCG routing below (generation always SCE's; T&D PG&E's in CZ 4/5/13, SCE's elsewhere --
exactly what CEDARS' own SCG hourly files contain). The next paragraph is history.

SUPERSEDED FOR SCG SPECIFICALLY (same day, still 2026-09-16): the
CZ04/05/13->PG&E fix above measurably helped (a real ~13% error dropped
to ~7-10% depending on what else was borrowed from PG&E) but never fully
closed the gap, because it's the wrong mechanism -- Jake supplied CET's
own real quarterly avoided-cost table for SCG (`dbo.
E3AvoidedCostElecCO2Seq2026`), and SCG's real data is NOT a scaled copy
of PG&E's: direct comparison at the same (target sector, end use,
climate zone, quarter) key shows a real, distinct SEASONAL pattern (Q3
higher than PG&E, Q2 much lower), not a flat ratio. `run_real_hourly()`
in run_poc.py then routed any SCG measure that table covers through CET's
quarterly SCG table (validated ratio 1.0015). That route was retired on
2026-10-02 (every measure is priced on the 8,760 path) and its code removed
from ReCET on 2026-10-09; SCG is priced on the hourly ACC as described below. This module's own PG&E fallback
for SCG remains in place as a safety net for any SCG (sector, end use,
zone) combo the real table doesn't cover.

SCG GENERATION IS ALWAYS SCE'S, EVEN IN CZ04/05/13 (correction, 2026-09-22,
Jake): the CZ04/05/13->PG&E rule above was being applied to this module's
own raw-hourly fallback's ENTIRE bundle (generation and T&D both), on the
assumption that CEDARS' own real SCG data followed the same split. It
doesn't. Directly inspecting CEDARS' own real hourly file extracts
(`CEDARS QAC Validation/v2_extracts/`, supplied 2026-09-22) shows
generation and the emission rate have NO zone dimension at all in
CEDARS' own pipeline (one file per PA, not per PA x zone -- confirmed by
the V2 documentation PDF: "generation costs and emission rates are
territory-wide"), and SCG's generation/emission-rate files are
bit-identical to SCE's for every SCG zone, including 4, 5 and 13. Only
T&D genuinely varies by zone in CEDARS' own files, and only T&D follows
Appendix C's PG&E-for-4/5/13 split (confirmed: SCG's real T&D-by-zone
files are themselves bit-identical copies of PG&E's for zones 4/5/13 and
SCE's for every other zone -- i.e. CEDARS' "SCG data" isn't independent
data at all, it's PG&E/SCE relabeled by exactly this rule). get_acc_series/
get_gen_td_series/get_emission_rate_series below were updated to route
Gen-side quantities to SCE always and only T&D through the 4/5/13 split,
matching CEDARS' own mechanism exactly rather than the earlier (plausible
but wrong) assumption that Appendix C's "PG&E sells electricity there"
applied to generation too.

**FLAGGED AS A POTENTIAL FUTURE FIX, both for CEDARS and for this POC**:
CEDARS' choice to use SCE's generation everywhere for SCG is very likely
itself a simplification forced by their file format having no per-zone
generation slot to substitute into -- not necessarily the most physically
accurate answer. Appendix C's own plain-text claim is that PG&E actually
sells electricity in zones 4/5/13, which would argue for PG&E's real
generation curve there, not SCE's. This POC now matches CEDARS' real,
confirmed mechanism (SCE generation always) because that's what's
verifiable today and what the rest of this project's validation work
target reproduces -- but if CEDARS ever adds zone-specific SCG generation,
or clarifies that the PG&E substitution should cover generation too, this
should be revisited. Do not treat "matches CEDARS" as "physically
correct" for this one specific case.

MISSING-ZONE T&D SUBSTITUTION (also 2026-09-16, from the main body text
adjacent to Appendix C, page 5 -- not literally inside the appendix, but
the same "missing avoided costs" problem the appendix's own title
promises to cover): the ACC Electric Model's own export is missing T&D
data for a few IOU/zone pairs it should have (per Appendix C's own DEER
floor-area table, Table C-1): SCE's CZ05, and SDG&E's CZ06 and CZ08. The
documented fix substitutes ANOTHER real zone's data as a stand-in --
SCE CZ05 <- SCE CZ09 (Appendix C: "CZ09's climate is most similar to
CZ05's" -- note the adjacent main-body text on page 5 says CZ06 instead,
an apparent inconsistency in the source document itself; CZ09 is used
here as the more specific, deliberately-justified appendix rule) --
SDG&E CZ06 <- SDG&E CZ07, SDG&E CZ08 <- SDG&E CZ07 (both documents
agree). Only T&D is actually documented as missing (Generation avoided
costs are IOU-level, not zone-specific, so they need no substitution at
all); this extraction's own `total_avoided_cost` column bundles
Generation+T&D+everything together, so borrowing the WHOLE stand-in
zone's total (not just its T&D) is used as the practical proxy -- a
reasonable one since Generation shouldn't vary by zone within an IOU
anyway, confirmed by how tight the non-outlier zones' ratios already are.
This is a SPECIFIC, real substitution rule for exactly these 3 (IOU,
zone) pairs -- NOT the same thing as `_blended_series()` below, which
remains a generic, un-documented fallback for genuinely unknown/
placeholder zones (e.g. the catalog's generic "System" zone) that
Appendix C says nothing about.

Climate zones: each IOU only has real ACC data for the subset of the 17
statewide zones it actually serves (PG&E 10, SCE 8, SDG&E 4 -- confirmed
by direct inspection). A measure's own real AC_Climate_Zone is used
directly (prefixed "CZ..."), except for the SCE CZ05 / SDG&E CZ06 / CZ08
substitutions documented above; a measure whose zone isn't one of that
IOU's covered real zones AND isn't one of those 3 documented pairs
(observed only for the catalog's generic placeholder zone "System" in the
real ~5,000-measure sample) falls back to a simple mean across that IOU's
own covered zones, flagged via get_acc_series's `used_blended_zone`
return value.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from .. import paths
from . import acc_store

HOURS_PER_YEAR = 8760

# Which ACC extraction to read is chosen at run time (paths.configure_runtime / recet.runtime.configure): the 2024 ACC by default, the 2026
# ACC (final, v1b), or a user-supplied file with the same layout. Changing it requires clearing this module's caches (runtime.clear_caches does).


def _pq() -> Path:
    p = paths.current_acc_electric()
    if p is None:
        raise FileNotFoundError("electric ACC parquet not found: set the reference data folder (see recet/paths.py)")
    return p


# The components the ACC extraction stores per hour ($/MWh); their sum is `total_avoided_cost`. The 2024 ACC has ghg_cap_and_trade and
# ghg_portfolio_rebalancing, the 2026 ACC has ghg_cap_and_invest instead; available_components() reports what the loaded file has.
ALL_COMPONENT_COLUMNS = ("energy", "generation_capacity", "avoided_as_procurement", "losses", "methane_leakage", "ghg_cap_and_trade",
                         "ghg_cap_and_invest", "ghg_adder", "ghg_portfolio_rebalancing", "air_quality_adder", "transmission", "distribution")
TD_COMPONENTS = ("transmission", "distribution")   # the kW-scalable T&D term of a TOU profile; every other component is in the kWh "Gen" term

PA_TO_IOU_LABEL = {"PGE": "PG&E", "SCE": "SCE", "SDGE": "SDG&E", "SCG": "SCE"}
# SCG (a gas-only utility) has no electric ACC of its own: _scg_iou() picks ONE utility per zone (SCE where SCE has native data, else PG&E) and that
# utility's whole bundle prices it. PA_TO_IOU_LABEL's "SCG" entry is only the default for that choice. Zone strings follow this module's convention (no
# leading zero, no "CZ" prefix -- e.g. "5" not "05" or "CZ05").

# Documented substitutions for real (IOU, zone) pairs missing from the ACC
# Electric Model's own export (main body text, page 5, plus Appendix C's
# Table C-1 -- see module docstring for the CZ05 CZ06-vs-CZ09 discrepancy
# between the two). Keys/values are (iou_label, cz_label) as used
# elsewhere in this module (cz_label already has the "CZ" prefix).
MISSING_ZONE_SUBSTITUTE = {
    ("SCE", "CZ5"): ("SCE", "CZ9"),
    ("SDG&E", "CZ6"): ("SDG&E", "CZ7"),
    ("SDG&E", "CZ8"): ("SDG&E", "CZ7"),
}


def is_available() -> bool:
    p = paths.current_acc_electric()
    return p is not None and p.exists()


@lru_cache(maxsize=2)
def _store_for(path: str, size: int, mtime_ns: int) -> acc_store.AccStore:
    return acc_store.AccStore(path)


def _store() -> acc_store.AccStore:
    """The dense, memory-mapped copy of the loaded ACC file (pipeline/acc_store.py). Keyed by the file's path, size and modification time."""
    p = _pq()
    st = p.stat()
    return _store_for(str(p), st.st_size, st.st_mtime_ns)


@lru_cache(maxsize=1)
def available_components() -> tuple[str, ...]:
    """The per-hour component columns present in the loaded ACC file, in canonical order (their sum is total_avoided_cost)."""
    import pyarrow.parquet as pq
    names = set(pq.ParquetFile(str(_pq())).schema_arrow.names)
    return tuple(c for c in ALL_COMPONENT_COLUMNS if c in names)


def available_cost_tests() -> tuple[str, ...]:
    """The cost tests the loaded electric ACC has rows for: ("TRC",) for the 2021 and 2022 ACCs, ("TRC", "SCT Base", "SCT High") for 2024 and 2026."""
    tests = {t for (_iou, _zone, t) in _store().blocks}
    return tuple(t for t in ("TRC", "SCT Base", "SCT High") if t in tests) + tuple(sorted(tests - {"TRC", "SCT Base", "SCT High"}))


@lru_cache(maxsize=1)
def _covered_zones() -> dict[str, list[str]]:
    return _store().zones_by_iou()


@lru_cache(maxsize=512)
def load_hourly_by_year(iou_label: str, cz_label: str, cost_test: str = "TRC", component: str | None = None) -> dict[int, np.ndarray]:
    """{year: array(8760,)} of real $/kWh-equivalent avoided cost (total,
    energy+capacity+T&D+GHG+losses, /1000 from the extraction's $/MWh
    columns), for one (iou, real climate zone) pair, across every year the
    extraction covers. `component` (a column name from available_components()) returns just that component instead of the total."""
    return _store().series(iou_label, cz_label, cost_test, "total_avoided_cost" if component is None else component)


@lru_cache(maxsize=512)
def load_emission_rate_by_year(iou_label: str, cz_label: str) -> dict[int, np.ndarray]:
    """{year: array(8760,)} of the ACC's own hourly grid emission rate
    (tonnes CO2 per kWh, `emission_rate_tonnes_per_kwh` in the extraction)
    for one (iou, real climate zone) pair. TRC rows are used; the rate does
    not depend on cost test in any way this POC relies on."""
    return _store().series(iou_label, cz_label, "TRC", "emission_rate_tonnes_per_kwh", divisor=None)


def _scg_iou(cz: str) -> str:
    """The ONE ACC utility whose whole bundle -- generation, T&D and emission rate -- prices SCG electric savings in zone `cz` (Jake, 2026-10-09): SCE
    wherever the ACC has native SCE data for the zone, else PG&E where it has native PG&E data, else SCE (then _resolve_zone's stand-in / blended rules
    apply). "Native" means the ACC file's own (iou, zone) rows, so SCE's documented CZ5 stand-in (CZ9) does not count and SCG CZ5 is PG&E. On both
    built-in ACCs: SCE in 6, 8, 9, 10, 13, 14, 15, 16; PG&E in 1, 2, 3A, 3B, 4, 5, 11, 12. The single source of truth for SCG routing."""
    label = f"CZ{cz}"
    covered = _covered_zones()
    if label in covered.get("SCE", ()):
        return "SCE"
    if label in covered.get("PG&E", ()):
        return "PG&E"
    return "SCE"


def _iou_for(pa: str, climate_zone) -> str:
    """The ACC utility label that prices a PA's electric savings in a zone: the PA's own utility, or for SCG the one utility _scg_iou() picks."""
    pa_key = pa.upper().replace("&", "")
    return _scg_iou(str(climate_zone)) if pa_key == "SCG" else PA_TO_IOU_LABEL[pa_key]


def _resolve_zone(iou_label: str, cz_label: str) -> tuple[str, str, bool]:
    """Shared (iou, zone) -> real (iou, zone) resolution: the documented missing-zone
    substitution first, then real coverage, else a signal to fall back to a blended mean
    across that IOU's own covered zones. Returns (iou_label, cz_label, used_blended) --
    the returned cz_label is meaningless when used_blended is True."""
    if (iou_label, cz_label) in MISSING_ZONE_SUBSTITUTE:
        iou_label, cz_label = MISSING_ZONE_SUBSTITUTE[(iou_label, cz_label)]
        return iou_label, cz_label, False
    covered = _covered_zones()[iou_label]
    if cz_label in covered:
        return iou_label, cz_label, False
    return iou_label, cz_label, True


def get_emission_rate_series(pa: str, climate_zone: str) -> dict[int, np.ndarray]:
    """Hourly emission-rate series for a measure's PA and zone, from the same utility that prices its avoided costs (_iou_for; SCG: SCE where SCE has
    native data, else PG&E). The documented SCE CZ5 / SDG&E CZ6/CZ8 stand-ins apply; any other zone not covered by that utility falls back to a mean across
    its covered zones."""
    iou_label, cz_label, blended = _resolve_zone(_iou_for(pa, climate_zone), f"CZ{climate_zone}")
    if not blended:
        return load_emission_rate_by_year(iou_label, cz_label)
    per_zone = [load_emission_rate_by_year(iou_label, cz) for cz in _covered_zones()[iou_label]]
    return {y: np.mean([pz[y] for pz in per_zone], axis=0) for y in sorted(per_zone[0])}


@lru_cache(maxsize=16)
def _blended_series(iou_label: str, cost_test: str, component: str | None = None) -> dict[int, np.ndarray]:
    covered = _covered_zones()[iou_label]
    per_zone = [load_hourly_by_year(iou_label, cz, cost_test, component) for cz in covered]
    years = sorted(per_zone[0].keys())
    return {y: np.mean([pz[y] for pz in per_zone], axis=0) for y in years}


def get_acc_series(pa: str, climate_zone: str, cost_test: str = "TRC", component: str | None = None) -> tuple[dict[int, np.ndarray], bool]:
    """Returns ({year: array(8760,)}, used_blended_zone). Resolves PA to a real ACC utility (_iou_for: the PA's own, or for SCG the one utility whose
    whole bundle prices that zone) and the measure's own climate zone to that utility's real covered zone. A zone matching one of the 3 documented
    missing-zone substitutions (SCE CZ5, SDG&E CZ6/CZ8 -- see MISSING_ZONE_SUBSTITUTE) uses its specific documented stand-in zone,
    used_blended_zone=False (it's a real, specific zone's data, not an average). Any other zone that isn't one of that utility's covered real zones
    (observed only for the catalog's generic "System" placeholder) falls back to a blended mean across its covered zones instead, flagged via the
    `used_blended_zone` return."""
    iou_label, cz_label, blended = _resolve_zone(_iou_for(pa, climate_zone), f"CZ{climate_zone}")
    if blended:
        return _blended_series(iou_label, cost_test, component), True
    return load_hourly_by_year(iou_label, cz_label, cost_test, component), False


@lru_cache(maxsize=512)
def load_td_by_year(iou_label: str, cz_label: str, cost_test: str = "TRC") -> dict[int, np.ndarray]:
    """{year: array(8760,)} of the T&D part of the avoided cost, $/kWh (same units/order as load_hourly_by_year)."""
    return _store().series(iou_label, cz_label, cost_test, "td")   # transmission + distribution; total_avoided_cost = Gen + T&D exactly (max |diff| 2e-12), so Gen = total - T&D


@lru_cache(maxsize=16)
def _blended_td(iou_label: str, cost_test: str) -> dict[int, np.ndarray]:
    per_zone = [load_td_by_year(iou_label, cz, cost_test) for cz in _covered_zones()[iou_label]]
    return {y: np.mean([pz[y] for pz in per_zone], axis=0) for y in sorted(per_zone[0])}


def get_gen_td_series(pa: str, climate_zone: str, cost_test: str = "TRC",
                      component: str | None = None) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """({year: Gen array}, {year: T&D array}) in $/kWh for the same (PA, zone) resolution rules as get_acc_series (one utility per zone, SCG included;
    documented missing-zone substitutions; blended fallback for unknown zones). Gen = total - T&D, with `total` read from get_acc_series so the two
    functions can never disagree with each other.

    component=<column name>: just that ACC component. transmission / distribution are the whole T&D term (Gen is zero); every other component is
    the whole Gen term (T&D is zero), so the per-component values of a TOU profile add up to the total's."""
    if component is not None:
        series, _blended = get_acc_series(pa, climate_zone, cost_test, component)
        zeros = {y: np.zeros_like(v) for y, v in series.items()}
        return (zeros, series) if component in TD_COMPONENTS else (series, zeros)
    total, _blended = get_acc_series(pa, climate_zone, cost_test)
    td_iou, td_cz, blended = _resolve_zone(_iou_for(pa, climate_zone), f"CZ{climate_zone}")
    td = _blended_td(td_iou, cost_test) if blended else load_td_by_year(td_iou, td_cz, cost_test)
    gen = {y: total[y] - td[y] for y in total if y in td}
    return gen, td
