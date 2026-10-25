"""The performance work must not change a single result: the dense ACC store, the on-disk caches, the gas index and the grouped hourly engine are each checked against the
straightforward implementation they replaced (pandas filters, per-measure loops)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import paths, runtime
from recet.pipeline import acc_store, discount_settings, engine, gas_avoided_cost, real_impact_profiles

HOURS = 8760


# ------------------------------------------------------------------------------------------------------------------------------------ the dense ACC store
def _acc_frame(seed: int = 5) -> pd.DataFrame:
    """A multi-IOU, multi-zone, three-test ACC with distinct random values, its rows shuffled (the real files are not in block order either)."""
    rng = np.random.default_rng(seed)
    frames = []
    for iou, zones in (("PG&E", ("CZ1", "CZ13")), ("SCE", ("CZ9",)), ("SDG&E", ("CZ7",))):
        for zone in zones:
            for test in ("TRC", "SCT Base", "SCT High"):
                for year in (2027, 2028, 2029):
                    frames.append(pd.DataFrame({"iou": iou, "climate_zone": zone, "cost_test": test, "year": np.int16(year), "hour_of_year": np.arange(1, HOURS + 1, dtype=np.int32),
                                                "energy": rng.uniform(10, 90, HOURS), "transmission": rng.uniform(0, 8, HOURS), "distribution": rng.uniform(0, 12, HOURS),
                                                "total_avoided_cost": rng.uniform(20, 140, HOURS), "emission_rate_tonnes_per_kwh": rng.uniform(0, 4e-4, HOURS)}))
    return pd.concat(frames, ignore_index=True).sample(frac=1.0, random_state=1).reset_index(drop=True)


def _pandas_series(df, iou, zone, test, column, divisor=1000.0):
    """What real_avoided_cost_v2 did before the store: filter, sort, reshape."""
    d = df[(df["iou"] == iou) & (df["climate_zone"] == zone) & (df["cost_test"] == test)].sort_values(["year", "hour_of_year"])
    years = sorted(d["year"].unique())
    v = d[column].to_numpy(dtype=np.float64)
    v = v / divisor if divisor else v
    v = v.reshape(len(years), HOURS)
    return {int(y): v[i] for i, y in enumerate(years)}


def _write(tmp_path, df, name="acc.parquet"):
    p = tmp_path / name
    df.to_parquet(p, index=False)
    return p


@pytest.mark.parametrize("cache", [True, False])
def test_store_gives_exactly_the_values_the_pandas_filter_gave(tmp_path, monkeypatch, cache):
    if not cache:
        monkeypatch.setenv("RECET_NO_CACHE", "1")
    df = _acc_frame()
    store = acc_store.AccStore(_write(tmp_path, df))
    assert (store.dir is not None) == cache
    for (iou, zone, test) in [("PG&E", "CZ1", "TRC"), ("PG&E", "CZ13", "SCT High"), ("SCE", "CZ9", "SCT Base"), ("SDG&E", "CZ7", "TRC")]:
        for column, divisor, name in (("total_avoided_cost", 1000.0, None), ("energy", 1000.0, None), ("emission_rate_tonnes_per_kwh", None, None)):
            got = store.series(iou, zone, test, column, divisor)
            want = _pandas_series(df, iou, zone, test, column, divisor or 0)
            assert list(got) == list(want) == [2027, 2028, 2029]
            for y in got:
                assert np.array_equal(got[y], want[y]), (iou, zone, test, column, y)                    # bit for bit, not just close
        td = store.series(iou, zone, test, "td")
        want_td = {y: (v) for y, v in _pandas_series(df.assign(td=df["transmission"] + df["distribution"]), iou, zone, test, "td").items()}
        assert all(np.array_equal(td[y], want_td[y]) for y in td)                                          # T&D is transmission + distribution, added then divided
    assert store.series("PG&E", "CZ99", "TRC", "total_avoided_cost") == {}
    assert store.zones_by_iou() == {"PG&E": ["CZ1", "CZ13"], "SCE": ["CZ9"], "SDG&E": ["CZ7"]}
    with pytest.raises(KeyError, match="no column"):
        store.column("no_such_column")


def test_store_series_are_read_only_views_of_the_cache(tmp_path):
    """With the cache on, series are views of a memory-mapped file of the divided values (no private copy per series), and cannot be written to."""
    store = acc_store.AccStore(_write(tmp_path, _acc_frame()))
    a = store.series("SCE", "CZ9", "TRC", "total_avoided_cost")
    scaled = store.scaled_column("total_avoided_cost", 1000.0)
    assert isinstance(scaled, np.memmap) and (store.dir / "col-total_avoided_cost-div1000.npy").exists()
    assert np.shares_memory(a[2027], scaled) and not a[2027].flags.writeable
    with pytest.raises(ValueError):
        a[2027][:] = -1.0                                   # a caller cannot scribble on the store
    assert store.series("SCE", "CZ9", "TRC", "total_avoided_cost")[2027].min() >= 0.02


def test_store_series_are_fresh_copies_without_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RECET_NO_CACHE", "1")
    store = acc_store.AccStore(_write(tmp_path, _acc_frame()))
    assert store.scaled_column("total_avoided_cost", 1000.0) is None
    a = store.series("SCE", "CZ9", "TRC", "total_avoided_cost")
    a[2027][:] = -1.0                                       # a caller scribbling on its copy must not touch the store
    assert store.series("SCE", "CZ9", "TRC", "total_avoided_cost")[2027].min() >= 0.02


def test_importing_recet_first_gives_numpy_one_blas_thread():
    """recet sets OPENBLAS_NUM_THREADS=1 unless it is already set (OpenBLAS reserves a buffer per thread; ReCET gains nothing from the threads)."""
    import subprocess
    import sys
    env = {k: v for k, v in __import__("os").environ.items() if k != "OPENBLAS_NUM_THREADS"}
    out = subprocess.run([sys.executable, "-c", "import recet, os; print(os.environ['OPENBLAS_NUM_THREADS'])"], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "1"
    env["OPENBLAS_NUM_THREADS"] = "4"
    out = subprocess.run([sys.executable, "-c", "import recet, os; print(os.environ['OPENBLAS_NUM_THREADS'])"], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "4"                        # a user's own setting wins


def test_a_second_store_reads_the_cache_and_is_identical(tmp_path):
    p = _write(tmp_path, _acc_frame())
    first = acc_store.AccStore(p)
    want = first.series("PG&E", "CZ13", "TRC", "total_avoided_cost")
    files = {f.name for f in first.dir.iterdir()}
    assert {"index.npz", "order.npy", "col-total_avoided_cost.npy"} <= files
    second = acc_store.AccStore(p)                          # index from disk, column memory-mapped
    got = second.series("PG&E", "CZ13", "TRC", "total_avoided_cost")
    assert isinstance(second.column("total_avoided_cost"), np.memmap)
    assert all(np.array_equal(got[y], want[y]) for y in want)
    third = acc_store.AccStore(p)                           # a column that was never built is built from the parquet using the cached order
    assert np.array_equal(third.series("SCE", "CZ9", "TRC", "energy")[2028], _pandas_series(_acc_frame(), "SCE", "CZ9", "TRC", "energy")[2028])


def test_a_changed_acc_file_is_never_served_from_a_stale_cache(tmp_path):
    p = tmp_path / "acc.parquet"
    df = _acc_frame()
    df.to_parquet(p, index=False)
    old_dir = acc_store.AccStore(p).dir
    edited = df.copy()
    edited.loc[edited["total_avoided_cost"] > 0, "total_avoided_cost"] *= 2.0
    edited.to_parquet(p, index=False)
    store = acc_store.AccStore(p)
    assert store.dir != old_dir
    assert np.array_equal(store.series("SCE", "CZ9", "TRC", "total_avoided_cost")[2027], _pandas_series(edited, "SCE", "CZ9", "TRC", "total_avoided_cost")[2027])


def test_an_unwritable_cache_falls_back_to_memory(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")
    monkeypatch.setenv("RECET_CACHE", str(blocker))     # mkdir under a file fails
    df = _acc_frame()
    store = acc_store.AccStore(_write(tmp_path, df))
    assert store.dir is None
    assert np.array_equal(store.series("PG&E", "CZ1", "SCT Base", "total_avoided_cost")[2029], _pandas_series(df, "PG&E", "CZ1", "SCT Base", "total_avoided_cost")[2029])


def test_an_acc_block_missing_an_hour_is_rejected(tmp_path):
    df = _acc_frame()
    bad = df.drop(df[(df.iou == "SCE") & (df.year == 2028) & (df.cost_test == "TRC") & (df.hour_of_year == 100)].index)
    with pytest.raises(ValueError):
        acc_store.AccStore(_write(tmp_path, bad))


def test_the_pipeline_reads_a_supplied_acc_through_the_store(tmp_path):
    from recet import versions
    from recet.pipeline import real_avoided_cost_v2 as v2
    df = _acc_frame()
    p = _write(tmp_path, df)
    runtime.apply(versions.resolve_acc("2026", electric_file=p))
    series, blended = v2.get_acc_series("PGE", "13", "TRC")
    assert not blended and np.array_equal(series[2028], _pandas_series(df, "PG&E", "CZ13", "TRC", "total_avoided_cost")[2028])
    gen, td = v2.get_gen_td_series("PGE", "13", "TRC")
    assert np.array_equal(td[2028], _pandas_series(df.assign(td=df["transmission"] + df["distribution"]), "PG&E", "CZ13", "TRC", "td")[2028])
    assert np.array_equal(gen[2028], series[2028] - td[2028])
    blend, used = v2.get_acc_series("PGE", "System", "TRC")          # a zone the file does not have: the mean over the IOU's zones
    assert used and np.allclose(blend[2027], (_pandas_series(df, "PG&E", "CZ1", "TRC", "total_avoided_cost")[2027] + _pandas_series(df, "PG&E", "CZ13", "TRC", "total_avoided_cost")[2027]) / 2)
    assert set(v2.get_emission_rate_series("PGE", "13")) == {2027, 2028, 2029}


# ------------------------------------------------------------------------------------------------------------------------------------ profile-shape cache
def test_cached_profile_shapes_are_identical_to_freshly_read_ones(tmp_path):
    from test_calcs import make_stub_library
    ref = tmp_path / "ref"
    make_stub_library(ref)
    paths.set_reference_dir(ref)
    runtime.clear_caches()
    fresh = real_impact_profiles._profile_shapes_by_zone(1)                        # reads the parquet, writes the cache
    cache_dir = real_impact_profiles._shape_cache_dir()
    assert (cache_dir / "1.npz").exists()
    real_impact_profiles._profile_shapes_by_zone.cache_clear()
    cached = real_impact_profiles._profile_shapes_by_zone(1)                       # now from disk
    assert list(cached) == list(fresh) and all(np.array_equal(cached[z], fresh[z]) for z in fresh)
    assert real_impact_profiles.get_shape(1, "5") is not None                     # the System fallback still works on cached data


def test_profile_cache_can_be_switched_off(tmp_path, monkeypatch):
    from test_calcs import make_stub_library
    monkeypatch.setenv("RECET_NO_CACHE", "1")
    ref = tmp_path / "ref"
    make_stub_library(ref)
    paths.set_reference_dir(ref)
    runtime.clear_caches()
    assert real_impact_profiles._shape_cache_dir() is None
    assert set(real_impact_profiles._profile_shapes_by_zone(1)) == {"13", "System"}


def test_a_warm_cache_changes_no_result(tmp_path):
    """The same job, run on an empty cache and again on the cache the first run left behind."""
    from test_run import _cfg
    from recet.jobs import run_job
    a = run_job(_cfg(tmp_path, run_name="cold"), log=lambda s: None).frame
    assert any(paths.cache_dir().rglob("*.npy"))
    b = run_job(_cfg(tmp_path, run_name="warm"), log=lambda s: None).frame
    pd.testing.assert_frame_equal(a, b, check_exact=True)


# ------------------------------------------------------------------------------------------------------------------------------------ small lookups
def test_gas_component_index_matches_the_row_filter_it_replaced():
    from recet import versions
    runtime.apply(versions.resolve_acc("2026"))
    table = gas_avoided_cost._table()
    checked = 0
    for (pa, sector, test, scc, year) in [("PGE", "Residential", "TRC", "", 2030), ("SCG", "Commercial", "SCT", "Base", 2040), ("SDGE", "Core", "SCT", "High", 2026),
                                          ("SCE", "Residential", "TRC", "", 2010), ("PGE", "Core", "TRC", "", 2070)]:      # the last two clamp to the covered years
        got = gas_avoided_cost.get_components_for_measure(pa, sector, year, test, scc)
        lo, hi = gas_avoided_cost.year_range()
        m = ((table["utility"] == gas_avoided_cost.utility_for_pa(pa)) & (table["class"] == gas_avoided_cost.class_for_gas_sector(sector)) & (table["cost_test"] == test)
             & (table["year"] == min(max(year, lo), hi)))
        if test == "SCT":
            m &= table["scc"] == scc
        row = table[m].iloc[0]
        for name in gas_avoided_cost.MONTHLY_COMPONENTS:
            assert np.array_equal(got[name], row[[f"{name}_{mo}" for mo in gas_avoided_cost.MONTHS]].to_numpy(dtype=np.float64))
        for name in gas_avoided_cost.ANNUAL_COMPONENTS:
            assert np.array_equal(got[name], np.full(12, float(row[name])))
        checked += 1
    assert checked == 5
    with pytest.raises(KeyError):
        gas_avoided_cost.get_components_for_measure("PGE", "Residential", 2030, "SCT", "Medium")
    with pytest.raises(ValueError):
        got["nox"][0] = 1.0                                                          # shared arrays are read-only


def test_discount_settings_lookups_are_cached_but_safe():
    s = discount_settings.get_settings("PGE", 2026)                                 # keyed by ACC version (the 2026 ACC = CET Version 2027)
    s["discount_rate_qtr"] = 99.0                                                   # a caller changing its copy...
    assert discount_settings.get_settings("PGE", 2026)["discount_rate_qtr"] == pytest.approx(0.01915)   # ...does not change the next call's
    assert discount_settings.get_co2_gas("PGE", 2026) == pytest.approx(0.00531)
    assert discount_settings.get_co2_gas("pge", 2026) == discount_settings.get_co2_gas("PGE", 2026)
    assert discount_settings.get_settings("PGE", 2022)["settings_base_year"] == 2022   # CET Version 2024
    assert discount_settings.get_settings("PGE", 2021)["settings_base_year"] == 2021   # CET Version 2022
    with pytest.raises(KeyError):
        discount_settings.get_settings("PGE", 2027)                                 # no 2027 ACC
    with pytest.raises(KeyError):
        discount_settings.get_settings("PGE", 1999)


def test_per_route_helper_equals_the_row_by_row_apply():
    from recet import run_poc
    df = pd.DataFrame({"measure_id": ["a", "b", "c", "d", "e"], "shape_key": ["s1", "s1", "s2", "s1", "s2"], "PA": ["PGE"] * 5,
                       "target_sector": ["R", "R", "R", "R", "C"], "climate_zone": ["13", "13", "13", "13", "13"]})
    calls = []

    def fn(m):
        calls.append(m["measure_id"])
        return m["shape_key"] == "s1"

    got = run_poc._per_route(df, run_poc.ROUTE_COLUMNS, fn)
    assert len(calls) == 3                                                          # one call per route (a, b, d share one; c; e), not one per row
    assert got.tolist() == df.apply(fn, axis=1).tolist()


# ------------------------------------------------------------------------------------------------------------------------------------ the grouped hourly engine
def _engine_inputs(seed: int, n: int = 90):
    rng = np.random.default_rng(seed)
    measures = pd.DataFrame({
        "measure_id": [f"m{i}" for i in range(n)],
        "claim_year": rng.integers(2026, 2032, n), "claim_quarter": rng.integers(1, 5, n),
        "eul_years_exact": rng.choice([0.25, 1.0, 2.4, 3.5, 5.0, 7.33, 12.9, 31.0], n),
        "rul_years_exact": rng.choice([0.0, 0.0, 0.0, 1.0, 1.4, 2.5, 9.0, 40.0], n),          # includes whole, fractional and RUL >= EUL
        "shape_scale": rng.normal(1000, 400, n), "shape_scale_2nd": rng.normal(500, 300, n),
        "route": rng.integers(0, 7, n)})
    shapes = {}
    for r in range(6):                                                                         # route 6 has no shape: its measures are unmatched
        w = rng.uniform(0.2, 1.8, HOURS)
        shapes[r] = (w / w.sum(), int(rng.choice([2009, 2012, 2018])))
    acc = {r: {y: rng.uniform(0.02, 0.25, HOURS) for y in range(2027, 2034)} for r in range(6)}   # ends before the longest lives do: later years use the last one
    return measures, shapes, acc


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_grouped_engine_equals_the_per_measure_engine(seed):
    from recet.pipeline.real_impact_profiles import shift_for_year
    measures, shapes, acc = _engine_inputs(seed)
    args = (measures, lambda m: shapes.get(m["route"]), lambda m: acc[m["route"]], shift_for_year, lambda m: 0.0191 + 0.0004 * m["route"], 2028)
    ref = engine.price_real_hourly_per_measure(*args)
    grouped = engine.price_real_hourly(*args, route_key=measures["route"])
    own_route = engine.price_real_hourly(*args)                                                    # every measure its own route
    lifetime = ref.measure_year.groupby("measure_id", sort=False)["annual_value"].sum().reset_index()      # the grouped engine returns lifetime values per measure
    for got in (grouped, own_route):
        pd.testing.assert_frame_equal(got.measure_year.reset_index(drop=True), lifetime, check_dtype=False, rtol=1e-12, atol=1e-9)
        assert got.unmatched_measure_ids == ref.unmatched_measure_ids and got.n_real_shape == ref.n_real_shape
    assert len(ref.unmatched_measure_ids) > 0 and ref.n_real_shape > 0


def test_grouped_engine_shifts_per_year_when_the_shift_depends_on_the_year():
    """The production shift ignores the priced year, so the grouped engine shifts each route once; a shift that does depend on the year (CEDARS' own rule,
    cedars_shift_for_year) must still be applied year by year."""
    from recet.pipeline.real_impact_profiles import cedars_shift_for_year, shift_for_year
    assert shift_for_year.year_independent and not getattr(cedars_shift_for_year, "year_independent", False)
    measures, shapes, acc = _engine_inputs(7)
    args = (measures, lambda m: shapes.get(m["route"]), lambda m: acc[m["route"]], cedars_shift_for_year, lambda m: 0.019, 2028)
    ref = engine.price_real_hourly_per_measure(*args).measure_year.groupby("measure_id", sort=False)["annual_value"].sum().reset_index()
    pd.testing.assert_frame_equal(engine.price_real_hourly(*args, route_key=measures["route"]).measure_year.reset_index(drop=True), ref,
                                  check_dtype=False, rtol=1e-12, atol=1e-9)


def _gas_inputs(seed: int, n: int = 90):
    rng = np.random.default_rng(seed)
    measures = pd.DataFrame({
        "measure_id": [f"g{i}" for i in range(n)],
        "claim_year": rng.integers(2026, 2032, n), "claim_quarter": rng.integers(1, 5, n),
        "eul_years_exact": rng.choice([0.25, 1.0, 2.4, 3.5, 5.0, 7.33, 12.9, 31.0], n),
        "rul_years_exact": rng.choice([0.0, 0.0, 0.0, 1.0, 1.4, 2.5, 9.0, 40.0], n),
        "annual_net_therms_1st": rng.choice([0.0, 0.0, 120.0, -60.0, 900.5], n), "annual_net_therms_2nd": rng.choice([0.0, 0.0, 30.0, 400.0], n),
        "route": rng.integers(0, 7, n)})
    shapes = {}
    for r in range(6):                                                                         # route 6 has no profile: its measures get no rows
        w = rng.uniform(0.0, 1.0, 12)
        shapes[r] = w / w.sum()
    acc = {r: {y: rng.uniform(0.3, 3.0, 12) for y in range(2028, 2034)} for r in range(6)}    # starts after the earliest claims and ends before the longest lives: years clamp
    return measures, shapes, acc


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_grouped_gas_engine_equals_the_per_measure_engine(seed):
    measures, shapes, acc = _gas_inputs(seed)
    args = (measures, lambda m: shapes.get(m["route"]), lambda m: acc[m["route"]], lambda m: 0.0191 + 0.0004 * m["route"], 2028)
    ref = engine.price_gas_benefit_per_measure(*args)
    lifetime = ref.groupby("measure_id", sort=False)["gas_benefit"].sum().reset_index()
    for got in (engine.price_gas_benefit(*args, route_key=measures["route"]), engine.price_gas_benefit(*args)):
        pd.testing.assert_frame_equal(got.reset_index(drop=True), lifetime, check_dtype=False, rtol=1e-12, atol=1e-9)
    assert len(ref) > 0 and set(ref["measure_id"]) < set(measures["measure_id"])                # some measures (no therms, no profile) are rightly absent


def test_grouped_gas_engine_with_nothing_to_price_is_empty_with_columns():
    measures, shapes, acc = _gas_inputs(4)
    none_priced = engine.price_gas_benefit(measures, lambda m: None, lambda m: acc[0], lambda m: 0.019, 2028, route_key=measures["route"])
    no_therms = engine.price_gas_benefit(measures.assign(annual_net_therms_1st=0.0, annual_net_therms_2nd=0.0), lambda m: shapes[0], lambda m: acc[0], lambda m: 0.019, 2028)
    for got in (none_priced, no_therms):
        assert got.empty and list(got.columns) == ["measure_id", "gas_benefit"]


def test_grouped_engine_with_nothing_priced_returns_an_empty_frame_with_columns():
    measures, shapes, acc = _engine_inputs(4)
    from recet.pipeline.real_impact_profiles import shift_for_year
    got = engine.price_real_hourly(measures, lambda m: None, lambda m: acc[0], shift_for_year, lambda m: 0.019, 2028, route_key=measures["route"])
    assert got.measure_year.empty and list(got.measure_year.columns) == ["measure_id", "annual_value"]
    assert got.unmatched_measure_ids == measures["measure_id"].tolist()


def test_lifetime_values_share_work_between_measures_and_keep_each_quarter_discounted():
    """Measures with the same route, claim quarter, EUL and RUL share one lifetime sum; their values still scale with their own savings, and a measure whose EUL
    gives no quarter is left without a row, as the per-measure engine leaves it."""
    from recet.pipeline.real_impact_profiles import shift_for_year
    measures, shapes, acc = _engine_inputs(6, n=40)
    measures = pd.concat([measures, measures.assign(measure_id=measures["measure_id"] + "x", shape_scale=measures["shape_scale"] * 3,
                                                    shape_scale_2nd=measures["shape_scale_2nd"] * 3)], ignore_index=True)
    measures.loc[0, "eul_years_exact"] = 0.0
    args = (measures, lambda m: shapes.get(m["route"]), lambda m: acc[m["route"]], shift_for_year, lambda m: 0.0191 + 0.0004 * m["route"], 2028)
    got = engine.price_real_hourly(*args, route_key=measures["route"]).measure_year.set_index("measure_id")["annual_value"]
    ref = engine.price_real_hourly_per_measure(*args).measure_year.groupby("measure_id")["annual_value"].sum()
    assert "m0" not in got.index and "m0" not in ref.index
    pd.testing.assert_series_equal(got.sort_index(), ref.sort_index(), check_names=False, rtol=1e-12, atol=1e-9)
    tripled = [i for i in got.index if i.endswith("x") and i[:-1] in got.index]
    assert tripled and np.allclose(got[tripled].to_numpy(), 3 * got[[i[:-1] for i in tripled]].to_numpy())


def test_portfolio_accumulator_still_comes_from_the_per_measure_engine():
    from recet.pipeline.real_impact_profiles import shift_for_year
    measures, shapes, acc = _engine_inputs(5, n=12)
    args = (measures, lambda m: shapes.get(m["route"]), lambda m: acc[m["route"]], shift_for_year, lambda m: 0.019, 2028)
    got = engine.price_real_hourly(*args, route_key=measures["route"], need_portfolio_hourly=True)
    assert got.portfolio_hourly.sum() > 0 and np.allclose(got.portfolio_hourly, engine.price_real_hourly_per_measure(*args).portfolio_hourly)


# ------------------------------------------------------------------------------------------------------------------------------------ the cache command
def test_data_clear_cache_removes_the_cache(tmp_path, capsys):
    from recet import cli
    acc_store.AccStore(_write(tmp_path, _acc_frame()))
    assert paths.cache_dir().exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["data", "clear-cache"])
    assert e.value.code == 0 and "removed" in capsys.readouterr().out and not paths.cache_dir().exists()
    with pytest.raises(SystemExit):
        cli.main(["data", "clear-cache"])
    assert "no cache" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------------------------- the array versions of three per-row loops
def _cost_inputs(seed: int, n: int = 400) -> pd.DataFrame:
    """Measures covering every branch: single and dual baseline (RUL whole, fractional, 0), fractional and short lives, fuel substitution and not, signs of both
    savings, incentives above and below the measure cost, two PAs and sectors, one sector with no rate table, claims before and after the First Year."""
    rng = np.random.default_rng(seed)
    rul = rng.choice([0.0, 0.0, 0.5, 1.0, 2.25, 3.0, 7.75], n)
    eul = np.maximum(rul + rng.choice([0.0, 0.25, 1.0, 4.4, 10.0], n), rng.choice([0.25, 1.0, 3.5, 12.0], n))
    return pd.DataFrame({
        "measure_id": [f"m{i}" for i in range(n)], "PA": rng.choice(["PGE", "SCE"], n), "target_sector": rng.choice(["Res", "Com", "NoTable"], n),
        "gas_sector": rng.choice(["Residential", "Commercial", "NoTable"], n),
        "claim_year": rng.choice([2027, 2028, 2030], n), "claim_quarter": rng.choice([1, 2, 3, 4], n), "rul_years_exact": rul, "eul_years_exact": eul,
        "ir_kwh": rng.uniform(0.8, 1.0, n), "rr_kwh": rng.uniform(0.8, 1.1, n),
        "annual_net_kwh_1st": rng.normal(0, 5000, n), "annual_net_kwh_2nd": rng.normal(0, 3000, n),
        "annual_net_therms_1st": rng.normal(0, 300, n), "annual_net_therms_2nd": rng.normal(0, 200, n), "is_fuel_sub": rng.random(n) < 0.5,
        "num_units": rng.integers(1, 50, n).astype(float), "NTGRkWh": rng.uniform(0.5, 1.0, n), "ntgr_cost": rng.uniform(0.5, 1.0, n),
        "unit_incentive_to_others": rng.uniform(0, 400, n), "unit_di_labor": rng.uniform(0, 100, n), "unit_di_material": rng.uniform(0, 100, n),
        "unit_end_user_rebate": rng.uniform(0, 200, n), "unit_meas_cost_1st": rng.uniform(50, 900, n), "unit_meas_cost_2nd": rng.uniform(20, 600, n),
        "meas_inflation": rng.choice([0.0, 0.02], n), "unit_gas_infra_bens": rng.uniform(0, 10, n), "unit_refrig_bens": rng.uniform(0, 10, n),
        "unit_misc_bens": rng.uniform(0, 10, n), "unit_refrig_costs": rng.uniform(0, 10, n), "unit_misc_costs": rng.uniform(0, 10, n),
        "unit_tax_credits": rng.uniform(0, 10, n),
    })


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_array_bill_impact_participant_and_other_costs_equal_the_per_measure_versions(seed):
    m = _cost_inputs(seed)
    tables = {(pa, s): {y: 0.1 + 0.01 * (y - 2024) + (0.03 if pa == "SCE" else 0.0) for y in range(2024, 2041) if y != 2033}       # 2033 missing
              for pa in ("PGE", "SCE") for s in ("Res", "Com", "Residential", "Commercial")}
    elec, gas = (lambda r: tables.get((r["PA"], r["target_sector"]))), (lambda r: tables.get((r["PA"], r["gas_sector"])))
    rate = lambda r: 0.0766 if r["PA"] == "PGE" else 0.0765
    for got, ref in ((engine.price_bill_impact(m, elec, gas, rate, 2024), engine.price_bill_impact_per_measure(m, elec, gas, rate, 2024)),
                     (engine.price_participant_cost(m, lambda r: rate(r) / 4, 2028, 0.05), engine.price_participant_cost_per_measure(m, lambda r: rate(r) / 4, 2028, 0.05)),
                     (engine.price_other_ben_cost(m, 0.05, 0.05), engine.price_other_ben_cost_per_measure(m, 0.05, 0.05))):
        pd.testing.assert_frame_equal(got[ref.columns].reset_index(drop=True), ref.reset_index(drop=True), check_dtype=False, rtol=1e-12, atol=1e-9)
    bills = engine.price_bill_impact(m, elec, gas, rate, 2024)
    assert (bills.BillIncrElec > 0).any() and (bills.BillReducElec < 0).any() and (bills.BillReducGas != 0).any()      # both sign rules were exercised
