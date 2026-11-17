import numpy as np
import pandas as pd
import pytest

from recet import overrides, templates
from conftest import make_electric_acc, make_flat_profile_csv


def test_flat_profile_loads_and_matches(tmp_path):
    u = overrides.load_user_profiles(make_flat_profile_csv(tmp_path / "p.csv"))
    hit = u.lookup("test_shape", "PGE", "residential", "13")          # case-insensitive, zone falls back to ALL
    assert hit is not None and hit[0].shape == (8760,) and hit[1] == 2009
    assert u.lookup("TEST_SHAPE", "SCE", "Residential", "13") is None  # different IOU: pa was given, so no match
    assert u.lookup("OTHER", "PGE", "Residential", "13") is None


def test_wildcards_and_specificity(tmp_path):
    a = pd.DataFrame({"end_use_name": "X", "pa": "", "target_sector": "", "climate_zone": "ALL", "hour_of_year": np.arange(1, 8761), "impact": 1 / 8760})
    b = a.assign(pa="PGE", impact=2 / 8760 / 2)                       # PGE-specific, also sums to 1 after the /2
    p = tmp_path / "p.csv"
    pd.concat([a, b]).to_csv(p, index=False)
    u = overrides.load_user_profiles(p)
    assert u.match("X", "SCE", "Any").pa is None                      # wildcard entry serves other IOUs
    assert u.match("X", "PGE", "Any").pa == "PGE"                     # the more specific entry wins


@pytest.mark.parametrize("zone_in,zone_out", [("CZ13", "13"), ("01", "1"), ("3a", "3A"), ("System", "SYSTEM"), ("", "ALL"), ("*", "ALL"), (13, "13")])
def test_zone_normalisation(zone_in, zone_out):
    assert overrides.norm_zone(zone_in) == zone_out


def test_missing_hour_is_an_error(tmp_path):
    df = pd.read_csv(make_flat_profile_csv(tmp_path / "p.csv"))
    df.iloc[:-1].to_csv(tmp_path / "bad.csv", index=False)
    with pytest.raises(overrides.OverrideError, match="1..8760"):
        overrides.load_user_profiles(tmp_path / "bad.csv")


def test_sum_rescaled_with_note_or_rejected(tmp_path):
    p = make_flat_profile_csv(tmp_path / "p.csv", total=2.0)
    u = overrides.load_user_profiles(p)
    assert abs(u.entries[0].zones["ALL"].sum() - 1.0) < 1e-12 and u.notes
    with pytest.raises(overrides.OverrideError, match="not 1"):
        overrides.load_user_profiles(p, normalize=False)


def test_zero_sum_profile_rejected(tmp_path):
    df = pd.read_csv(make_flat_profile_csv(tmp_path / "p.csv"))
    df["impact"] = np.where(df["hour_of_year"] <= 4380, 1.0, -1.0)
    df.to_csv(tmp_path / "z.csv", index=False)
    with pytest.raises(overrides.OverrideError, match="~0"):
        overrides.load_user_profiles(tmp_path / "z.csv")


def test_user_profile_missing_zone_signals_gap(tmp_path):
    u = overrides.load_user_profiles(make_flat_profile_csv(tmp_path / "p.csv", zone="5"))
    assert u.lookup("TEST_SHAPE", "PGE", "Residential", "5") is not None
    assert u.lookup("TEST_SHAPE", "PGE", "Residential", "7") == "zone_missing"


def test_gas_profiles(tmp_path):
    p = tmp_path / "g.csv"
    pd.DataFrame({"Profile": "New Gas", "Month": np.arange(1, 13), "Impact": [0.2] * 5 + [0.0] * 7}).to_csv(p, index=False)
    prof, notes = overrides.load_user_gas_profiles(p)
    assert abs(prof["New Gas"].sum() - 1.0) < 1e-12 and prof["New Gas"][0] == 0.2 and not notes
    q = tmp_path / "g2.csv"
    pd.DataFrame({"Profile": "Annual", "Month": ["Jan", "Feb"], "Impact": [0.5, 0.5]}).to_csv(q, index=False)
    with pytest.raises(overrides.OverrideError, match="month"):
        overrides.load_user_gas_profiles(q)


def test_gas_profile_name_case_is_canonicalised(tmp_path):
    p = tmp_path / "g.csv"
    pd.DataFrame({"Profile": "annual", "Month": np.arange(1, 13), "Impact": 1 / 12}).to_csv(p, index=False)
    prof, notes = overrides.load_user_gas_profiles(p)
    assert "Annual" in prof and notes


def test_electric_acc_check(tmp_path):
    good = make_electric_acc(tmp_path / "acc.parquet")
    chk = overrides.check_electric_acc(good)
    assert chk.ok, chk.text()
    assert chk.info["years"] == "2027-2029" and "TRC" in chk.info["cost_tests"]
    df = pd.read_parquet(good)
    df.drop(columns=["total_avoided_cost"]).to_parquet(tmp_path / "nototal.parquet")
    assert not overrides.check_electric_acc(tmp_path / "nototal.parquet").ok
    df.iloc[:-5].to_parquet(tmp_path / "short.parquet")
    chk = overrides.check_electric_acc(tmp_path / "short.parquet")
    assert not chk.ok and "8,760" in chk.text()
    df[df.cost_test == "TRC"].to_parquet(tmp_path / "trc_only.parquet")
    chk = overrides.check_electric_acc(tmp_path / "trc_only.parquet")
    assert chk.ok and any("SCB" in w for w in chk.warnings)


def test_electric_acc_component_sum_warning(tmp_path):
    df = pd.read_parquet(make_electric_acc(tmp_path / "acc.parquet"))
    df["energy"] += 5.0                                             # components no longer add to the total
    df.to_parquet(tmp_path / "off.parquet")
    chk = overrides.check_electric_acc(tmp_path / "off.parquet")
    assert chk.ok and any("sum of its component" in w for w in chk.warnings)


def test_electric_acc_csv_is_converted_to_parquet(tmp_path):
    df = pd.read_parquet(make_electric_acc(tmp_path / "acc.parquet", years=(2028,)))
    df.assign(iou=df["iou"].replace({"PG&E": "PGE", "SDG&E": "SDGE"}), cost_test=df["cost_test"].str.lower()).to_csv(tmp_path / "acc.csv", index=False)
    assert not overrides.check_electric_acc(tmp_path / "acc.csv").ok            # PGE / lowercase labels are reported, not silently accepted
    df.to_csv(tmp_path / "good.csv", index=False)
    assert overrides.check_electric_acc(tmp_path / "good.csv").ok
    out = overrides.prepare_electric_acc(tmp_path / "good.csv", tmp_path / "work")
    assert out.suffix == ".parquet"
    back = pd.read_parquet(out)
    assert set(back["iou"]) == {"PG&E", "SCE", "SDG&E"} and len(back) == len(df)


def test_gas_acc_template_roundtrip(tmp_path):
    out = templates.write_template("acc-gas", tmp_path / "gas.csv", "2026")
    chk = overrides.check_gas_acc(out)
    assert chk.ok or all("rows for" in w or "combination" in w for w in chk.warnings), chk.text()
    prepared = overrides.prepare_gas_acc(out, tmp_path / "work")
    df = pd.read_parquet(prepared)
    assert set(df["utility"]) == {"PG&E"} and df.loc[df.cost_test == "TRC", "scc"].isna().all()


def test_gas_acc_rejects_bad_labels(tmp_path):
    df = pd.read_parquet(__import__("recet.paths", fromlist=["x"]).package_data("acc", "2026", "gas_components.parquet"))
    df["utility"] = df["utility"].replace({"PG&E": "Pacific"})
    df.to_csv(tmp_path / "bad.csv", index=False)
    chk = overrides.check_gas_acc(tmp_path / "bad.csv")
    assert not chk.ok and "utility" in chk.text()
