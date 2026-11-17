"""The quarterly tables a CET release prices with, as exported from its database backup.

A folder holds one release's tables for one avoided-cost Version (the whole tables, or the rows a job needs):

    ac_elec.csv         E3AvoidedCostElecCO2Seq<Version>   (PA, Version, TS, EU, CZ, Qtr, Qac, Gen, TD, CO2, [Gen_SB, TD_SB, Gen_SH, TD_SH], DSType, ACElecKey)
    ac_gas.csv          E3AvoidedCostGasSeq<Version>       (ID, PA, Version, FirstYear, GS, GP, ComboField, Qtr, Qac, Commodity, TD, Total, ACGasKey, [.._SB, .._SH])
    emissions.csv       E3Emissions                        (PA, Version, TS, EU, CZ, NOx, PM10)
    rates_elec.csv      E3RateScheduleElec                 (PA, Version, Year, Qy, Schedule, RateE)
    rates_gas.csv       E3RateScheduleGas                  (PA, Version, Year, Qy, Schedule, RateG)
    settings.csv        E3Settings                         (PA, Version, DiscountRateAnnual, DiscountRateQtr, BaseYear, CO2Gas, NOxGas, [Societal...])
    rate_map_elec.csv   E3RateScheduleElecMapping          (PA, TargetSector, Schedule, Version)
    rate_map_gas.csv    E3RateScheduleGasMapping           (PA, Version, GasSector, GasRateSchedule)
    combustion_type.csv E3CombustionType                   (LookupCode, NOx)
    meta.json           {"release", "version", "base_year", "max_qac_elec", "max_qac_gas"}

`max_qac_*` are the whole tables' largest Qac: CET pads the rebased table from it, so a folder that holds only some rows must
still carry the whole table's value.

`from_release` reads a real run's tables instead: the release's small tables shipped in `recet/data/cet_versions/<release>/`
(E3Settings, CETAvoidedCostVersions, the rate-schedule mappings, E3CombustionType and, where it is small, E3RateScheduleGas)
and its large ones from the parquet export in the reference data folder (`<reference folder>/cet_versions/manifest.csv` and
`tables/`, written by CEDARS-Research `poc/export_cet_backup_tables.py`). Given the job's measures it keeps only the
avoided-cost rows of the job's PAs, end uses and climate zones (the 2026 table of 25.1 has 23 million rows).

A table also carries a cache (`cache`) for what the calculation derives from it once per run rather than once per chunk of
measures (the rebased source views and their join keys).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

TEXT = {"PA": str, "Version": str, "TS": str, "EU": str, "CZ": str, "Qtr": str, "DSType": str, "ACElecKey": str, "GS": str,
        "GP": str, "ComboField": str, "ACGasKey": str, "Schedule": str, "TargetSector": str, "GasSector": str,
        "GasRateSchedule": str, "LookupCode": str, "AvoidedCostVersion": str, "FirstYear": str}


@dataclass
class QuarterlyTables:
    version: str
    base_year: int
    ac_elec: pd.DataFrame
    ac_gas: pd.DataFrame
    emissions: pd.DataFrame
    rates_elec: pd.DataFrame
    rates_gas: pd.DataFrame
    settings: pd.DataFrame
    rate_map_elec: pd.DataFrame
    rate_map_gas: pd.DataFrame
    combustion_type: pd.DataFrame
    max_qac_elec: int
    max_qac_gas: int
    cache: dict = field(default_factory=dict, repr=False, compare=False)

    def cached(self, name, build):
        """`build()` once per tables object (the views derived from the tables are the same for every chunk of measures)."""
        if name not in self.cache:
            self.cache[name] = build()
        return self.cache[name]

    @classmethod
    def from_folder(cls, folder: str | Path) -> "QuarterlyTables":
        folder = Path(folder)
        meta = json.loads((folder / "meta.json").read_text())

        def read(name, **kw):
            p = folder / name
            return pd.read_csv(p, dtype=TEXT, keep_default_na=False, na_values=[""], encoding="utf-8", **kw)

        ac_elec, ac_gas = read("ac_elec.csv"), read("ac_gas.csv")
        return cls(
            version=str(meta["version"]), base_year=int(meta["base_year"]),
            ac_elec=ac_elec, ac_gas=ac_gas, emissions=read("emissions.csv"),
            rates_elec=read("rates_elec.csv"), rates_gas=read("rates_gas.csv"), settings=read("settings.csv"),
            rate_map_elec=read("rate_map_elec.csv"), rate_map_gas=read("rate_map_gas.csv"),
            combustion_type=read("combustion_type.csv"),
            max_qac_elec=int(meta.get("max_qac_elec", ac_elec.Qac.max())),
            max_qac_gas=int(meta.get("max_qac_gas", ac_gas.Qac.max())),
        )

    @classmethod
    def from_release(cls, release: str, version: str, export_dir: str | Path | None = None,
                     measures: pd.DataFrame | None = None) -> "QuarterlyTables":
        """The release's tables for avoided-cost `version` (CET's Version, e.g. "2026"), from its shipped small tables and
        the parquet export (`export_dir`, default `<reference folder>/cet_versions`). `measures` (CET's CEDARS input names)
        narrows the avoided-cost tables to the job's keys; every row is kept without it."""
        from .inputs import version_key
        from .sql import key

        from .releases import get
        why = dict(get(release).not_offered).get(str(version))
        if why:
            raise ValueError(f"CET {release}: Version {version} is not offered ({why})")

        if export_dir is None:
            from .. import paths
            ref = paths.reference_dir()
            if ref is None:
                raise FileNotFoundError("no reference data folder is set (recet data set-dir), so the CET tables cannot be found")
            export_dir = ref / "cet_versions"
        export_dir = Path(export_dir)
        manifest = export_dir / "manifest.csv"
        if not manifest.exists():
            raise FileNotFoundError(f"{manifest} is missing: export the release's tables with CEDARS-Research poc/export_cet_backup_tables.py")
        with open(manifest, encoding="utf-8") as f:
            files = {r["table"]: export_dir / r["file"] for r in csv.DictReader(f) if r["release"] == str(release)}

        def small(name):
            p = resources.files("recet.data").joinpath(f"cet_versions/{release}/{name}.csv")
            if not p.is_file():
                return None
            with p.open(encoding="utf-8") as f:
                return pd.read_csv(f, dtype=TEXT, keep_default_na=False, na_values=[""])

        def large(name, required=True, keep=None, skip=()):
            """The table, with only the rows whose `keep` columns' keys are in the given sets. A parquet is read in
            batches and filtered before it becomes a DataFrame: 25.1's 2026 electric table has 23.3M rows."""
            if name in files:
                import pyarrow as pa
                import pyarrow.compute as pc
                import pyarrow.parquet as pq
                fields = [f for f in pq.ParquetFile(files[name]).schema_arrow if f.name not in skip]
                strings = [f.name for f in fields if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)]
                # text columns stay dictionary-encoded while the file is read (as they are stored): a row group of 25.1's 2026
                # table is a million rows, and decoded to plain strings it is several hundred MB
                pf = pq.ParquetFile(files[name], read_dictionary=strings, pre_buffer=False)
                schema = pa.schema([pa.field(f.name, pa.float64() if pa.types.is_decimal(f.type) else
                                             pa.string() if f.name in TEXT or f.name in strings else f.type) for f in fields])
                columns = [f.name for f in fields]

                def rows_kept(col, keys):
                    """A mask of the rows whose `col` matches one of `keys` as CET compares text, worked out on each
                    chunk's dictionary (its distinct values) rather than on every row."""
                    masks = []
                    for ch in col.chunks:
                        if pa.types.is_dictionary(ch.type):
                            values, idx = ch.dictionary.cast(pa.string()), ch.indices
                        else:
                            values, idx = pc.unique(ch.cast(pa.string())), None
                        good = key(pd.Series(values.to_pylist(), dtype=object)).isin(keys).fillna(False).to_numpy(bool)
                        if idx is None:
                            masks.append(pc.is_in(ch.cast(pa.string()), value_set=values.filter(pa.array(good))))
                        else:
                            masks.append(pc.is_in(idx, value_set=pa.array(np.flatnonzero(good), idx.type)))
                    return pa.chunked_array(masks, pa.bool_())

                # each row group in two passes: its key columns first, then (only if a row matches) the whole row group,
                # one thread at a time; most of a large table's row groups hold nothing the job needs
                kept = []
                for i in range(pf.num_row_groups):
                    mask = None
                    if keep:
                        k = pf.read_row_group(i, columns=list(keep), use_threads=False)
                        for col, keys in keep.items():
                            m = rows_kept(k[col], keys)
                            mask = m if mask is None else pc.and_(mask, m)
                        del k
                        if not pc.any(mask).as_py():
                            continue
                    t = pf.read_row_group(i, columns=columns, use_threads=False)
                    if mask is not None:
                        t = t.filter(mask)
                    kept.append(t.cast(schema))
                    del t
                t = pa.concat_tables(kept) if kept else schema.empty_table()
                df = t.to_pandas().astype({c: object for c in t.column_names if c in TEXT})
                del kept, t
                pa.default_memory_pool().release_unused()
                return df
            df = small(name)
            if df is None:
                if required:
                    raise FileNotFoundError(f"CET {release}: table {name} is neither in {manifest} nor shipped with ReCET")
                return df
            for col, keys in (keep or {}).items():
                df = df[key(df[col]).isin(keys).fillna(False).astype(bool)]
            return df

        def max_qac(name):
            if name in files:
                import pyarrow.compute as pc
                import pyarrow.parquet as pq
                pf = pq.ParquetFile(files[name])
                j = pf.schema_arrow.get_field_index("Qac")
                stats = [pf.metadata.row_group(i).column(j).statistics for i in range(pf.metadata.num_row_groups)]
                if stats and all(s is not None and s.has_min_max for s in stats):   # the row groups' statistics, without reading the column
                    return int(max(float(s.max) for s in stats))
                return int(pc.max(pq.read_table(files[name], columns=["Qac"])["Qac"].cast("float64")).as_py())
            return int(pd.to_numeric(small(name).Qac).max())

        versions = small("CETAvoidedCostVersions")
        row = versions[version_key(versions.Version) == version_key(pd.Series([str(version)]))[0]]
        if row.empty:
            raise ValueError(f"CET {release} has no avoided-cost Version {version}; it has {', '.join(versions.Version)}")
        row = row.iloc[0]
        # the largest quarter is the whole table's (CalcCE caps a measure's quarters at it), so it is read before filtering
        max_e, max_g = max_qac(row.AVCElecTable), max_qac(row.AVCGasTable)
        keep_elec = keep_gas = None
        if measures is not None:
            pas = set(key(measures.PA).dropna())
            shape = measures["E3MeaElecEndUseShape"].astype("string")
            eus = set(key(shape.str.replace(r"(?i)^(non_res|res):(?=deer)", "", regex=True)).dropna()) | set(key(shape).dropna())
            czs = set(key(measures["E3ClimateZone"].astype("string").fillna("")).dropna())
            keep_elec, keep_gas = {"PA": pas, "EU": eus, "CZ": czs}, {"PA": pas}
        # ID and ACKey (the quarter's key, the widest column) are not read by any calculation
        ac_elec = large(row.AVCElecTable, keep=keep_elec, skip=("ID", "ACKey")).reset_index(drop=True)
        ac_gas = large(row.AVCGasTable, keep=keep_gas).reset_index(drop=True)
        vk = set(version_key(ac_elec.Version.dropna()).unique()) | {version_key(pd.Series([str(version)]))[0]}
        emissions = large("E3Emissions")
        emissions = emissions[version_key(emissions.Version).isin(vk)].reset_index(drop=True)
        v = version_key(pd.Series([str(version)]))[0]

        def of_version(df):
            return df[version_key(df.Version) == v].reset_index(drop=True)

        return cls(
            version=str(row.Version), base_year=int(row.BaseYear), ac_elec=ac_elec, ac_gas=ac_gas, emissions=emissions,
            rates_elec=of_version(large("E3RateScheduleElec")), rates_gas=of_version(large("E3RateScheduleGas")),
            settings=small("E3Settings"), rate_map_elec=small("E3RateScheduleElecMapping"),
            rate_map_gas=small("E3RateScheduleGasMapping"), combustion_type=small("E3CombustionType"),
            max_qac_elec=max_e, max_qac_gas=max_g,
        )
