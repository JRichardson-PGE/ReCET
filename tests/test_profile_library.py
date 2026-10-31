"""The consolidated impact-profile library (recet/profile_library.py, BR-06.18): only profiles whose every zone's 8,760 is identical (and drawn on the same
calendar) are stored once; every name keeps resolving to the same shapes; and real jobs price BIT FOR BIT the same on the consolidated library as on the
library it came from."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from recet import paths, profile_library, regress, runtime
from recet.jobs import run_job
from recet.pipeline import real_impact_profiles as rip

FIXTURES = Path(__file__).parent / "data" / "regression"
REF = FIXTURES / "ref"

COPIES = {4: 2, 6: 2, 3: 2, 25: 23, 120: 118}     # id -> the id whose rows it gets: identical, under other Impact Profile Names (3 is unused by the jobs)
NOT_COPIES = {7: "one value one ulp off", 8: "an extra zone", 122: "same rows, another calendar (2018)"}


def _rows(lib: pd.DataFrame, pid: int) -> pd.DataFrame:
    return lib[lib["impact_profile_id"] == pid]


def unconsolidated_reference(dest: Path) -> Path:
    """The regression fixtures' reference folder, its library changed so that COPIES hold exact copies of other profiles' rows and NOT_COPIES near copies."""
    shutil.copytree(REF, dest)
    pf = pq.ParquetFile(REF / "impact_profiles" / "hourly.parquet")
    lib = pf.read().to_pandas()
    groups = {int(pid): g for pid, g in lib.groupby("impact_profile_id", sort=True)}
    for pid, src in COPIES.items():
        groups[pid] = _rows(lib, src).assign(impact_profile_id=pid)
    off = _rows(lib, 2).assign(impact_profile_id=7)
    off.iloc[100, off.columns.get_loc("impact")] = np.nextafter(off.iloc[100]["impact"], 1.0)
    groups[7] = off
    groups[8] = pd.concat([_rows(lib, 2), _rows(lib, 2).assign(climate_zone="12")]).assign(impact_profile_id=8)
    groups[122] = _rows(lib, 2).assign(impact_profile_id=122)              # 122's catalog row is drawn on 2018, 2's on 2009 (blank)
    with pq.ParquetWriter(dest / "impact_profiles" / "hourly.parquet", pf.schema_arrow) as w:
        for pid in sorted(groups):
            t = pa.Table.from_pandas(groups[pid], schema=pf.schema_arrow, preserve_index=False)
            w.write_table(t, row_group_size=t.num_rows)
    return dest


@pytest.fixture(scope="module")
def libraries(tmp_path_factory):
    root = tmp_path_factory.mktemp("libs")
    before = unconsolidated_reference(root / "before")
    after = root / "after"
    shutil.copytree(before, after)
    shutil.rmtree(after / "impact_profiles")
    report = profile_library.consolidate(before, after / "impact_profiles", log=lambda s: None)
    return before, after, report


def test_only_exact_copies_are_stored_once(libraries):
    before, after, report = libraries
    cat = pd.read_parquet(after / "impact_profiles" / "catalog.parquet").set_index("id")
    for pid, src in COPIES.items():
        assert cat.loc[pid, "shape_id"] == src, pid                                       # every name of the profile points at the one stored copy
    for pid in NOT_COPIES:
        assert cat.loc[pid, "shape_id"] == pid, NOT_COPIES[pid]                           # near copies keep their own
    assert cat["shape_id"].isna().sum() == (~cat.index.isin(pd.read_parquet(before / "impact_profiles" / "hourly.parquet")["impact_profile_id"])).sum()
    stored = pq.ParquetFile(after / "impact_profiles" / "hourly.parquet").metadata.num_row_groups
    assert report["stored_profiles_before"] == 28 + 1 and stored == report["stored_profiles_after"] == 29 - len(COPIES)
    assert report["profiles_with_several_names"] == 3 and report["names_on_shared_profiles"] == 3 + len(COPIES)
    assert report["profiles_with_different_impact_profile_names"] == 3                    # e.g. HVAC_Chillers and HVAC_Refrig_Charge, one profile
    assert len(cat) == len(pd.read_parquet(before / "impact_profiles" / "catalog.parquet"))   # every name kept
    assert json.loads((after / "impact_profiles" / profile_library.REPORT_NAME).read_text())["stored_profiles_after"] == stored


def test_every_name_resolves_to_the_same_shapes(libraries):
    before, after, _ = libraries
    shapes = {}
    for folder in (before, after):
        paths.set_reference_dir(folder)
        runtime.clear_caches()
        cat = rip._catalog_hourly()
        shapes[folder] = {int(pid): rip._profile_shapes_by_zone(int(pid)) for pid in cat["id"]}
        assert rip.match_profile_id("DEER:HVAC_Refrig_Charge", "PGE", "Non_Res") == (4, True)    # a lookup by name gives the name's own id
    runtime.clear_caches()
    assert shapes[before].keys() == shapes[after].keys()
    for pid, zones in shapes[before].items():
        got = shapes[after][pid]
        assert list(got) == list(zones), pid
        for z in zones:
            assert got[z].tobytes() == zones[z].tobytes(), (pid, z)


def test_real_jobs_price_bit_for_bit_the_same(libraries, tmp_path):
    before, after, _ = libraries
    runs = {}
    for name, ref in (("before", before), ("after", after)):
        for job in regress.fixture_jobs(FIXTURES):
            res = run_job(regress.run_config(job, ref, tmp_path / name), log=lambda s: None)
            runs[(name, job.name)] = res.run_dir
    runtime.clear_caches()
    for job in regress.fixture_jobs(FIXTURES):
        a, b = regress.tables(runs[("after", job.name)]), regress.tables(runs[("before", job.name)])
        assert a.keys() == b.keys() and a
        for k in a:
            x, y = regress.read_table(a[k]), regress.read_table(b[k])
            keep = [c for c in y.columns if c not in regress.SKIP_COLUMNS]
            pd.testing.assert_frame_equal(x[keep], y[keep], check_exact=True, obj=f"{job.name} {k}")
            for c in keep:                                                                # bit for bit, signed zeros included
                if pd.api.types.is_float_dtype(y[c]):
                    assert x[c].to_numpy().tobytes() == y[c].to_numpy().tobytes(), (job.name, k, c)


def test_consolidating_again_changes_nothing_and_keeps_the_lineage(libraries, tmp_path):
    before, after, _ = libraries
    again = tmp_path / "again"
    report = profile_library.consolidate(after, again, log=lambda s: None)
    assert report["stored_profiles_after"] == report["stored_profiles_before"]
    c1 = pd.read_parquet(after / "impact_profiles" / "catalog.parquet")
    c2 = pd.read_parquet(again / "catalog.parquet")
    pd.testing.assert_series_equal(c1["shape_id"], c2["shape_id"])
    sizes = profile_library.equivalent_hourly_sizes(again / "catalog.parquet", again / "hourly.parquet")
    for folder in (before / "impact_profiles", after / "impact_profiles", again):     # a System-zone catalog built on any of them still applies
        assert (folder / "hourly.parquet").stat().st_size in sizes
    (again / "hourly.parquet").write_bytes((after / "impact_profiles" / "hourly.parquet").read_bytes() + b"x")    # a changed file is not vouched for
    assert profile_library.equivalent_hourly_sizes(again / "catalog.parquet", again / "hourly.parquet") == {(again / "hourly.parquet").stat().st_size}


def test_a_library_without_shape_id_reads_each_row_group(libraries):
    before, _, _ = libraries
    paths.set_reference_dir(before)
    runtime.clear_caches()
    try:
        assert "shape_id" not in pq.ParquetFile(before / "impact_profiles" / "catalog.parquet").schema_arrow.names
        assert rip.shape_id_of(4) == 4 and rip.shape_id_of(5) == 5
        assert list(rip._profile_shapes_by_zone(8)) == ["13", "12"]
    finally:
        runtime.clear_caches()


def test_the_output_folder_must_differ(libraries):
    before, _, _ = libraries
    with pytest.raises(ValueError):
        profile_library.consolidate(before, before / "impact_profiles", log=lambda s: None)


def test_cli_consolidates(libraries, tmp_path, capsys):
    from recet import cli
    before, _, _ = libraries
    with pytest.raises(SystemExit) as e:
        cli.main(["data", "consolidate-profiles", str(before), str(tmp_path / "out")])
    assert e.value.code == 0 and (tmp_path / "out" / "hourly.parquet").exists()
    assert "29 -> 24 stored profiles" in capsys.readouterr().out


def test_the_system_zone_catalog_is_the_same_on_both(libraries, tmp_path, monkeypatch):
    from recet.pipeline import system_zones
    before, after, _ = libraries
    built = {}
    try:
        for name, ref in (("before", before), ("after", after)):
            paths.set_reference_dir(ref)
            runtime.clear_caches()
            system_zones.build_catalog(tmp_path / name, version_keys=("2024",), log=lambda s: None)
            built[name] = pd.read_csv(tmp_path / name / system_zones.CATALOG_NAME, dtype=str)
        monkeypatch.setattr(system_zones, "catalog_dir", lambda: tmp_path / "before")      # the catalog built on the source library ...
        runtime.clear_caches()
        assert system_zones.catalog_usable() == ("2024", "")                               # ... applies to its consolidated copy
    finally:
        runtime.clear_caches()
    assert len(built["before"]) > 0
    pd.testing.assert_frame_equal(built["after"], built["before"])
