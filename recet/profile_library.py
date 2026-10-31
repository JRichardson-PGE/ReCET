"""Consolidating the impact-profile library: every profile stored once, with each name it is filed under (BRD v0.5 BR-06.18).

Jake's rule (2026-10-31): consolidate ONLY profiles that match 100%, never within a tolerance. Two library profiles are the same profile when

* their hourly rows are identical: the same climate zones, and in every zone the same 8,760 hours with bit-for-bit the same `impact` values, in the same
  order (`fingerprint`: SHA-256 over every column but the id, in file order, so the repaired shapes real_impact_profiles.shapes_from_frame builds from
  them are identical too), and
* they are drawn on the same calendar (`represents_year` as ReCET prices it: year_represented, blank = 2009), so their weekday alignment is the same.

Names differ freely: CEDARS stores each start-year-2028 deemed shape once per IOU territory, and the same shape can be filed under different Impact
Profile Names. A consolidated profile keeps every name.

The library's format (reference folder `impact_profiles/`):

* `catalog.parquet`: one row per name (id, end_use_name, iou_ac_territory, target_sector, year_represented ...) plus `shape_id`, the id of the hourly
  row group holding its 8,760s (blank: no hourly data). Every lookup by name (real_impact_profiles.match_profile_id) gives the name's own id, and
  real_impact_profiles reads its shapes through shape_id. A catalog without `shape_id` (the scraped library) reads each row's own row group.
* `hourly.parquet`: one row group per STORED profile, its id column (`impact_profile_id`) = the shape_id. A consolidated profile's stored id is the
  smallest source row-group id among its names, so an unshared profile keeps its own id.

`consolidate(src, out)` writes a consolidated copy of a library and checks it: every name's rows in the new file must have the fingerprint of its rows in
the source (an exact check, not a sample). The catalog keeps every source column and row; its Parquet metadata (`recet.consolidation`) records the
source hourly file's size and the new one's, so a System-zone catalog built on the source still applies (system_zones.catalog_usable): the shapes it was
built from are proven identical. CLI: `recet data consolidate-profiles SRC OUT`.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DEFAULT_REPRESENTS_YEAR = 2009                  # = real_impact_profiles.DEFAULT_REPRESENTS_YEAR (not imported: this module needs no reference folder)
METADATA_KEY = b"recet.consolidation"
REPORT_NAME = "consolidation_report.json"


def library_folder(path) -> Path:
    """The folder holding catalog.parquet and hourly.parquet: `path` itself or its impact_profiles/ (a reference folder)."""
    p = Path(path)
    return p / "impact_profiles" if (p / "impact_profiles" / "catalog.parquet").exists() else p


def fingerprint(table: pa.Table, id_column: str) -> str:
    """SHA-256 of a row group's rows, every column but `id_column`, in file order. Equal fingerprints = the same rows, bit for bit."""
    h = hashlib.sha256()
    for name in table.column_names:
        if name == id_column:
            continue
        col = table.column(name).combine_chunks()
        h.update(name.encode() + b"\x00" + str(col.type).encode() + b"\x00" + str(col.null_count).encode() + b"\x00")
        if pa.types.is_string(col.type) or pa.types.is_large_string(col.type) or pa.types.is_dictionary(col.type):
            h.update("\x1f".join("" if v is None else v for v in col.to_pylist()).encode("utf-8"))
        else:
            a = col.to_numpy(zero_copy_only=False)
            h.update(np.ascontiguousarray(a).tobytes())
        h.update(b"\x1e")
    return h.hexdigest()


def _row_groups(pf: pq.ParquetFile) -> dict[int, int]:
    """stored id -> row group (one row group per stored profile, its id column min == max)."""
    out = {}
    for g in range(pf.metadata.num_row_groups):
        stats = pf.metadata.row_group(g).column(0).statistics
        if stats is None or not stats.has_min_max or stats.min != stats.max:
            raise ValueError(f"row group {g} of {pf.metadata} does not hold exactly one profile")
        out[int(stats.min)] = g
    return out


def consolidation_info(catalog_path) -> dict:
    """The `recet.consolidation` record of a consolidated catalog ({} for a library that was never consolidated)."""
    try:
        md = pq.ParquetFile(str(catalog_path)).schema_arrow.metadata or {}
    except (OSError, pa.ArrowInvalid):
        return {}
    raw = md.get(METADATA_KEY)
    return json.loads(raw) if raw else {}


def equivalent_hourly_sizes(catalog_path, hourly_path) -> set[int]:
    """Sizes (bytes) of the hourly files whose profiles this library's are proven identical to: its own, and, when it is a consolidated copy whose hourly
    file is the one consolidation wrote, every source it descends from."""
    size = Path(hourly_path).stat().st_size
    info = consolidation_info(catalog_path)
    if info.get("hourly_bytes") == size:
        return {size, *info.get("equivalent_hourly_bytes", [])}
    return {size}


def consolidate(src, out, *, log=print) -> dict:
    """Write the consolidated library of `src` to `out` (both a library folder or a reference folder; `out` gets catalog.parquet, hourly.parquet and
    consolidation_report.json) and return the report. Reads the source hourly file twice (fingerprints, then copying), the new one once (the check)."""
    src = library_folder(src)
    out = Path(out)
    if (out / "impact_profiles").is_dir():
        out = out / "impact_profiles"
    cat_src, hourly_src = src / "catalog.parquet", src / "hourly.parquet"
    if out.resolve() == src.resolve():
        raise ValueError("write the consolidated library to another folder; the source is read while it is written")
    out.mkdir(parents=True, exist_ok=True)

    cat_table = pq.read_table(str(cat_src))
    cat = cat_table.to_pandas()
    if "id" not in cat.columns:
        raise ValueError(f"{cat_src} has no id column")
    pf = pq.ParquetFile(str(hourly_src))
    id_col = pf.schema_arrow.names[0]
    groups = _row_groups(pf)
    log(f"source: {len(cat):,} catalog rows, {len(groups):,} stored profiles ({hourly_src.stat().st_size / 1e9:.2f} GB)")

    # 1. a fingerprint per stored profile
    prints: dict[int, str] = {}
    zones: dict[int, int] = {}
    for n, (sid, g) in enumerate(sorted(groups.items())):
        t = pf.read_row_group(g)
        prints[sid] = fingerprint(t, id_col)
        zones[sid] = len(pc.unique(t.column("climate_zone"))) if "climate_zone" in t.column_names else 0
        if (n + 1) % 2000 == 0:
            log(f"  fingerprinted {n + 1:,} / {len(groups):,}")

    src_shape = cat["shape_id"] if "shape_id" in cat.columns else cat["id"]
    src_shape = pd.array(src_shape, dtype="Int64")
    year = cat["year_represented"].fillna(DEFAULT_REPRESENTS_YEAR).astype(int) if "year_represented" in cat.columns else pd.Series(
        DEFAULT_REPRESENTS_YEAR, index=cat.index)
    has_data = np.array([not pd.isna(s) and int(s) in groups for s in src_shape], dtype=bool)

    # 2. the same profile = the same rows and the same calendar; its stored id = the smallest source row-group id among its names
    key_of = {i: (int(year.iloc[i]), prints[int(src_shape[i])]) for i in np.flatnonzero(has_data)}
    members: dict[tuple, set[int]] = {}
    for i, k in key_of.items():
        members.setdefault(k, set()).add(int(src_shape[i]))
    taken: set[int] = set()
    next_free = max([*groups, *cat["id"].astype(int).tolist(), 0]) + 1
    stored: dict[tuple, tuple[int, int]] = {}                    # key -> (new stored id, source row group id it is copied from)
    for k in sorted(members, key=lambda k: min(members[k])):
        first = min(members[k])
        new_id = first if first not in taken else next_free
        if new_id == next_free:
            next_free += 1
        taken.add(new_id)
        stored[k] = (new_id, first)
    new_shape = pd.array([stored[key_of[i]][0] if i in key_of else pd.NA for i in range(len(cat))], dtype="Int64")

    # 3. the hourly file: one row group per stored profile, rows copied from its first source row group with the id set
    hourly_out, cat_out = out / "hourly.parquet", out / "catalog.parquet"
    schema = pf.schema_arrow.remove_metadata()
    with pq.ParquetWriter(str(hourly_out), schema, compression="zstd") as w:
        for n, (new_id, first) in enumerate(sorted(stored.values())):
            t = pf.read_row_group(groups[first]).cast(schema)
            ids = pa.array(np.full(t.num_rows, new_id), type=schema.field(id_col).type)
            w.write_table(t.set_column(0, schema.field(id_col), ids), row_group_size=max(t.num_rows, 1))
            if (n + 1) % 2000 == 0:
                log(f"  written {n + 1:,} / {len(stored):,}")

    # 4. the check: every name's rows in the new file have the fingerprint of its rows in the source
    pf_out = pq.ParquetFile(str(hourly_out))
    groups_out = _row_groups(pf_out)
    prints_out = {sid: fingerprint(pf_out.read_row_group(g), id_col) for sid, g in groups_out.items()}
    bad = [int(cat["id"].iloc[i]) for i in key_of if prints_out.get(int(new_shape[i])) != prints[int(src_shape[i])]]
    if bad or len(groups_out) != len(stored):
        raise RuntimeError(f"consolidated library check failed: {len(bad)} name(s) read different rows (ids {bad[:10]}); nothing should be used from {out}")

    # 5. the catalog: every source row and column, shape_id set, and the provenance
    cat_new = cat_table.drop_columns([c for c in ("shape_id",) if c in cat_table.column_names])
    cat_new = cat_new.append_column(pa.field("shape_id", pa.int64()), pa.array([None if pd.isna(v) else int(v) for v in new_shape], type=pa.int64()))
    src_info = consolidation_info(cat_src)
    src_sizes = equivalent_hourly_sizes(cat_src, hourly_src)
    provenance = {
        "format": 1,
        "written": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "rule": "identical rows in every zone (bit for bit) and the same represents_year",
        "source_hourly_bytes": hourly_src.stat().st_size,
        "hourly_bytes": hourly_out.stat().st_size,
        "equivalent_hourly_bytes": sorted(src_sizes),
        "source_consolidated": bool(src_info),
    }
    md = dict(cat_new.schema.metadata or {})
    md[METADATA_KEY] = json.dumps(provenance).encode()
    pq.write_table(cat_new.replace_schema_metadata(md), str(cat_out), compression="zstd")

    report = _report(cat, has_data, src_shape, new_shape, groups, stored, zones, hourly_src, hourly_out, provenance)
    (out / REPORT_NAME).write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"consolidated: {report['stored_profiles_before']:,} -> {report['stored_profiles_after']:,} stored profiles, "
        f"{report['zone_8760s_before']:,} -> {report['zone_8760s_after']:,} zone 8,760s, {report['hourly_gb_before']} -> {report['hourly_gb_after']} GB; "
        f"{report['profiles_with_several_names']:,} profiles carry {report['names_on_shared_profiles']:,} names "
        f"({report['profiles_with_different_impact_profile_names']:,} under more than one Impact Profile Name); checked: every name reads identical rows")
    return report


def _report(cat, has_data, src_shape, new_shape, groups, stored, zones, hourly_src, hourly_out, provenance) -> dict:
    df = pd.DataFrame({"id": cat["id"].astype(int), "end_use_name": cat.get("end_use_name"), "iou": cat.get("iou_ac_territory"),
                       "sector": cat.get("target_sector"), "shape_id": new_shape})[has_data]
    by = df.groupby("shape_id")
    n_names = by.size()
    shared = n_names[n_names > 1]
    n_eu = by["end_use_name"].nunique()
    multi_eu = n_eu[n_eu > 1]
    used_src = sorted({int(s) for s in src_shape[has_data]})
    largest = []
    for sid in shared.sort_values(ascending=False).index[:10]:
        g = df[df["shape_id"] == sid]
        largest.append({"shape_id": int(sid), "names": int(len(g)),
                        "examples": [f"{r.end_use_name} | {r.iou} | {r.sector}" for r in g.head(5).itertuples()]})
    diff_names = []
    for sid in multi_eu.index[:10]:
        g = df[df["shape_id"] == sid]
        diff_names.append({"shape_id": int(sid), "impact_profile_names": sorted(map(str, g["end_use_name"].unique()))[:6]})
    return {
        **provenance,
        "catalog_rows": int(len(cat)),
        "names_with_hourly_data": int(has_data.sum()),
        "stored_profiles_before": len(groups),
        "stored_profiles_used_before": len(used_src),
        "stored_profiles_after": len(stored),
        "zone_8760s_before": int(sum(zones.get(s, 0) for s in groups)),
        "zone_8760s_after": int(sum(zones[first] for _new, first in stored.values())),
        "hourly_gb_before": round(Path(hourly_src).stat().st_size / 1e9, 3),
        "hourly_gb_after": round(Path(hourly_out).stat().st_size / 1e9, 3),
        "profiles_with_several_names": int(len(shared)),
        "names_on_shared_profiles": int(shared.sum()),
        "profiles_with_different_impact_profile_names": int(len(multi_eu)),
        "largest": largest,
        "different_names_examples": diff_names,
    }
