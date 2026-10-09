"""SCG electric routing in pipeline/real_avoided_cost_v2.py (Jake, 2026-10-09): ONE utility's whole bundle per zone.

SCG has no electric ACC of its own. In each climate zone it is priced entirely -- generation, T&D and emission rate -- by SCE where the ACC has native SCE
data for the zone, else by PG&E where it has native PG&E data, else by SCE (with SCE's stand-in / blended fallback). Never SCE generation with PG&E T&D:
that mix (the 2026-09-22 routing, which matched CEDARS' own 8,760-path SCG files) made zones 4/5/13 the cheapest zone for nearly every SCG profile once the
System climate zone began picking the lowest-cost zone. "Native" excludes SCE's documented CZ5 stand-in (CZ9), so SCG CZ5 is PG&E.

The synthetic ACC gives PG&E and SCE deliberately different, easy-to-tell-apart values so a test that reads the wrong utility fails loudly.
"""
import numpy as np
import pandas as pd
import pytest

from recet import paths
from recet.pipeline import real_avoided_cost_v2 as acc

YEARS = (2028, 2029)
I = np.arange(8760)

PGE_GEN, PGE_TD = 100.0, 10.0
SCE_GEN, SCE_TD = 200.0, 20.0
PGE_EMISSION, SCE_EMISSION = 0.0001, 0.0009


def _frames(iou: str, zone: str, gen: float, td: float, emission: float) -> list[pd.DataFrame]:
    return [pd.DataFrame({
        "iou": iou, "climate_zone": zone, "cost_test": "TRC", "year": y, "hour_of_year": I + 1,
        "energy": gen + 0 * I, "transmission": td / 2 + 0 * I, "distribution": td / 2 + 0 * I,
        "total_avoided_cost": gen + td + 0 * I, "emission_rate_tonnes_per_kwh": emission + 0 * I,
    }) for y in YEARS]


@pytest.fixture()
def scg_acc(tmp_path):
    frames = []
    for zone in ("CZ4", "CZ5", "CZ13"):
        frames += _frames("PG&E", zone, PGE_GEN, PGE_TD, PGE_EMISSION)
    for zone in ("CZ6", "CZ9", "CZ13"):   # SCE shares CZ13 with PG&E; CZ9 is SCE's documented stand-in for its missing CZ5
        frames += _frames("SCE", zone, SCE_GEN, SCE_TD, SCE_EMISSION)
    path = tmp_path / "acc.parquet"
    pd.concat(frames, ignore_index=True).to_parquet(path, index=False)
    paths.set_reference_dir(None)
    paths.configure_runtime(acc_electric=str(path))
    for f in vars(acc).values():
        if callable(f) and hasattr(f, "cache_clear"):
            f.cache_clear()
    yield
    paths.configure_runtime(acc_electric=None)
    for f in vars(acc).values():
        if callable(f) and hasattr(f, "cache_clear"):
            f.cache_clear()


BUNDLE = {"PGE": (PGE_GEN, PGE_TD, PGE_EMISSION), "SCE": (SCE_GEN, SCE_TD, SCE_EMISSION)}
# zone -> utility whose whole bundle prices SCG there
CASES = [("4", "PGE"),    # PG&E only
         ("5", "PGE"),    # SCE's CZ5 is only a stand-in (CZ9), not native -> PG&E
         ("13", "SCE"),   # both native -> SCE, the default
         ("6", "SCE"),    # SCE only
         ("9", "SCE")]


@pytest.mark.parametrize("zone,iou", CASES)
def test_scg_gen_and_td_come_from_one_utility(scg_acc, zone, iou):
    gen, td = acc.get_gen_td_series("SCG", zone, "TRC")
    g, t, _e = BUNDLE[iou]
    for y in YEARS:
        assert gen[y] == pytest.approx(g / 1000.0), (zone, y, f"generation must be {iou}'s")
        assert td[y] == pytest.approx(t / 1000.0), (zone, y, f"T&D must be {iou}'s")


@pytest.mark.parametrize("zone,iou", CASES)
def test_scg_total_and_components_come_from_one_utility(scg_acc, zone, iou):
    g, t, _e = BUNDLE[iou]
    total, blended = acc.get_acc_series("SCG", zone, "TRC")
    energy, _ = acc.get_acc_series("SCG", zone, "TRC", component="energy")
    trans, _ = acc.get_acc_series("SCG", zone, "TRC", component="transmission")
    assert blended is False
    for y in YEARS:
        assert total[y] == pytest.approx((g + t) / 1000.0)
        assert energy[y] == pytest.approx(g / 1000.0)
        assert trans[y] == pytest.approx(t / 2 / 1000.0)


@pytest.mark.parametrize("zone,iou", CASES)
def test_scg_emission_rate_follows_the_same_utility(scg_acc, zone, iou):
    rate = acc.get_emission_rate_series("SCG", zone)
    for y in YEARS:
        assert rate[y] == pytest.approx(BUNDLE[iou][2])


def test_scg_zone_neither_utility_has_falls_back_to_sce(scg_acc):
    assert acc._scg_iou("7") == "SCE"
    total, blended = acc.get_acc_series("SCG", "7", "TRC")
    assert blended is True                       # SCE's blended mean across its covered zones, as for any SCE zone without data
    for y in YEARS:
        assert total[y] == pytest.approx((SCE_GEN + SCE_TD) / 1000.0)


def test_non_scg_pas_unaffected(scg_acc):
    """PG&E/SCE measures resolve exactly as before -- only SCG routing changed."""
    gen, td = acc.get_gen_td_series("PGE", "13", "TRC")
    for y in YEARS:
        assert gen[y] == pytest.approx(PGE_GEN / 1000.0) and td[y] == pytest.approx(PGE_TD / 1000.0)
    gen, td = acc.get_gen_td_series("SCE", "13", "TRC")
    for y in YEARS:
        assert gen[y] == pytest.approx(SCE_GEN / 1000.0) and td[y] == pytest.approx(SCE_TD / 1000.0)
    gen, td = acc.get_gen_td_series("SCE", "5", "TRC")    # SCE's own CZ5 still uses its CZ9 stand-in
    for y in YEARS:
        assert gen[y] == pytest.approx(SCE_GEN / 1000.0) and td[y] == pytest.approx(SCE_TD / 1000.0)
