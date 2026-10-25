"""A job run in parts (chunked.py) gives the files a whole-job run gives: same rows, same columns, values equal to rounding."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import outputs, preflight, templates
from recet.jobs import RunConfig, run_job
from recet.pipeline import delimited
from conftest import make_electric_acc, make_flat_profile_csv

N = 40           # measures; parts of CHUNK rows -> 6 parts, the last one short
CHUNK = 7


def _job(tmp_path, n=N):
    """`n` PG&E measures over three programs with admin costs: varied claim quarters, lives, NTG, costs and early replacement. CombustionType is blank in the
    first part and text later, so a part's own type inference would differ from the whole file's."""
    rows = []
    for i in range(n):
        r = {c: "" for c in templates.MEASURE_COLUMNS}
        r.update({"CEInputID": f"M{i:03d}", "PrgID": f"PRG{i % 3}", "ClaimYearQuarter": f"{2028 + i % 2}Q{1 + i % 4}", "Sector": "Res", "AC_Climate_Zone": "13",
                  "AC_Gas_Target_Sector": "Residential", "AC_Elec_Impact_Profile": "TEST_SHAPE", "AC_Elec_Target_Sector": "Residential",
                  "MeasDescription": f"measure {i}", "IOU_AC_Territory": "PGE", "EUL_Yrs": str(1 + (i % 5) * 0.75), "RUL_Yrs": "0.5" if i % 6 == 0 else "0",
                  "NumUnits": str(1 + i % 4), "UnitkWh1stBaseline": str(100 + 37 * i), "UnitkWh2ndBaseline": str(50 + i) if i % 6 == 0 else "0",
                  "UnitkW1stBaseline": "0", "UnitTherm1stBaseline": "0", "UnitkW2ndBaseline": "0", "UnitTherm2ndBaseline": "0",
                  "NTGRkW": "1", "NTGRkWh": str(0.6 + 0.01 * (i % 30)), "NTGRTherm": "1", "NTGRCost": "0.8", "RealizationRatekW": "1", "RealizationRatekWh": "1",
                  "RealizationRateTherm": "1", "InstallationRatekW": "1", "InstallationRatekWh": "1", "InstallationRateTherm": "1", "MeasInflation": "0",
                  "UnitMeaCost1stBaseline": str(20 + i), "UnitEndUserRebate": str(i % 7), "UnitkWhIOUWater1stBaseline": "5" if i % 9 == 0 else "",
                  "CombustionType": "" if i < CHUNK else ("Commercial" if i % 2 else "Residential")})
        rows.append(r)
    m = tmp_path / "Measure.csv"
    m.write_text("\n".join(["|".join(templates.MEASURE_COLUMNS)] + ["|".join(r[c] for c in templates.MEASURE_COLUMNS) for r in rows]) + "\n", encoding="utf-8")
    pc_rows = []
    for p in range(3):
        for y in (2028, 2029):
            r = {c: "0" for c in templates.PROGRAMCOST_COLUMNS}
            r.update({"PrgID": f"PRG{p}", "PrgYear": str(y), "ClaimYearQuarter": f"{y}Q4", "AdminCostsOverheadAndGA": str(1000 * (p + 1)), "MarketingOutreach": str(300 + y - 2028),
                      "PA": "PGE"})
            pc_rows.append(r)
    pc = tmp_path / "ProgramCost.csv"
    pc.write_text("\n".join(["|".join(templates.PROGRAMCOST_COLUMNS)] + ["|".join(r[c] for c in templates.PROGRAMCOST_COLUMNS) for r in pc_rows]) + "\n",
                  encoding="utf-8")
    return m, pc


def _cfg(tmp_path, m, pc, **kw):
    base = dict(measures=str(m), programcost=str(pc), out_dir=str(tmp_path / "out"), acc_version="2026",
                acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet", years=(2027, 2028, 2029, 2030, 2031, 2032), transmission=5.0)),
                profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False)
    base.update(kw)
    return RunConfig(**base)


def _same(a: pd.DataFrame, b: pd.DataFrame) -> None:
    assert list(a.columns) == list(b.columns) and len(a) == len(b)
    for c in a.columns:
        x, y = a[c], b[c]
        if pd.api.types.is_numeric_dtype(x) and not pd.api.types.is_bool_dtype(x):
            np.testing.assert_allclose(y.to_numpy(float), x.to_numpy(float), rtol=1e-12, atol=1e-9, equal_nan=True, err_msg=c)
        else:
            assert (x.astype(object).fillna("").astype(str) == y.astype(object).fillna("").astype(str)).all(), c


@pytest.mark.parametrize("fmt, extra", [("csv", {}), ("csv.zip", {"portfolio_8760": True}), ("parquet", {"market_effects": 0.05, "components": True, "portfolio_8760": True}),
                                        ("xlsx", {"apply_exclude_from_ce": True})])
def test_parts_give_the_whole_jobs_files(tmp_path, fmt, extra):
    m, pc = _job(tmp_path)
    whole = run_job(_cfg(tmp_path, m, pc, run_name="whole", chunk_size=0, output_format=fmt, **extra), log=lambda s: None)
    parts = run_job(_cfg(tmp_path, m, pc, run_name="parts", chunk_size=CHUNK, output_format=fmt, **extra), log=lambda s: None)
    assert parts.frame is None and any("parts" in n for n in parts.notes)
    assert not (parts.run_dir / "_parts").exists()
    assert set(parts.files) == set(whole.files)
    for key in whole.files:
        if key in ("run_settings", "preflight"):
            continue
        _same(outputs.read_table(whole.files[key]), outputs.read_table(parts.files[key]))
    assert parts.totals == pytest.approx(whole.totals, rel=1e-12)
    costs = pd.read_csv(pc, sep="|")
    total = costs[["AdminCostsOverheadAndGA", "MarketingOutreach"]].to_numpy().sum()
    assert outputs.read_table(parts.files["measure_level"])["WeightedProgramCost"].sum() == pytest.approx(total, rel=1e-12)     # every program's costs shared out once


def test_part_types_follow_the_whole_file(tmp_path):
    m, _pc = _job(tmp_path)
    parts = delimited.split_file(m, CHUNK, tmp_path / "parts")
    assert [n for _f, _s, n in parts] == [7, 7, 7, 7, 7, 5] and [s for _f, s, _n in parts] == [0, 7, 14, 21, 28, 35]
    dtypes, values = delimited.part_dtypes(parts, ("ClaimYearQuarter",))
    whole = delimited.read_cet_csv(m, low_memory=False)
    joined = pd.concat([delimited.read_cet_csv(f, low_memory=False, dtype=dtypes) for f, _s, _n in parts], ignore_index=True)
    pd.testing.assert_frame_equal(joined, whole)
    assert delimited.read_cet_csv(parts[0][0], low_memory=False)["CombustionType"].dtype != whole["CombustionType"].dtype   # what the dtypes fix
    assert values["ClaimYearQuarter"] == set(whole["ClaimYearQuarter"])
    assert delimited.count_rows(m) == N


def test_preflight_in_parts_equals_the_whole_check(tmp_path):
    m, pc = _job(tmp_path)
    from recet import runtime, versions
    acc = versions.resolve_acc("2026")
    runtime.apply(acc)
    key = lambda r, sev=None: {(i.severity, i.code, i.message): (i.count, i.examples, sorted(i.cells)) for i in r.issues if sev in (None, i.severity)}
    a = preflight.check_inputs(m, pc, acc=acc)
    b = preflight.check_inputs(m, pc, acc=acc, chunk_size=CHUNK)
    assert key(b) == key(a) and b.stats == a.stats and list(b.stats) == list(a.stats)


def test_preflight_in_parts_finds_errors_across_parts(tmp_path):
    """A duplicate CEInputID whose two rows sit in different parts, and bad values in a later part, are found with the whole file's row numbers. (The whole-file
    check stops after these; a part without them goes on, so only the errors are compared.)"""
    m, pc = _job(tmp_path)
    text = m.read_text(encoding="utf-8").splitlines()
    text[31] = text[31].replace("M030", "M002", 1)                                   # data row 30 (part 5) repeats data row 2 (part 1)
    text[26] = text[26].replace("|PGE|", "|XYZ|", 1)                                  # data row 25: a bad territory
    m.write_text("\n".join(text) + "\n", encoding="utf-8")
    from recet import runtime, versions
    acc = versions.resolve_acc("2026")
    runtime.apply(acc)
    errors = lambda r: {(i.code, i.message): (i.count, sorted(i.cells)) for i in r.errors}
    a = preflight.check_inputs(m, pc, acc=acc)
    b = preflight.check_inputs(m, pc, acc=acc, chunk_size=CHUNK)
    assert errors(b) == errors(a)
    assert errors(b)[next(k for k in errors(b) if k[0] == "duplicate_id")][1] == [("Measure", 2, "CEInputID"), ("Measure", 30, "CEInputID")]
    assert ("Measure", 25, "IOU_AC_Territory") in errors(b)[next(k for k in errors(b) if k[0] == "bad_iou")][1]


def test_parquet_parts_are_joined_into_full_row_groups(tmp_path):
    """TableWriter gathers parquet parts into row groups of row_group_rows (every group full but the last), whatever the part size, and keeps every row."""
    import pyarrow.parquet as pq
    df = pd.DataFrame({"a": np.arange(1000), "b": [None] * 500 + ["x"] * 500})       # b is blank in the first parts: its type comes from the later ones
    for rows, groups in ((150, [150] * 6 + [100]), (200, [200] * 5), (5000, [1000])):
        w = outputs.TableWriter(tmp_path / f"t{rows}", "parquet", len(df), rows)
        for i in range(0, len(df), 100):
            w.write(df.iloc[i:i + 100])
        p = w.close()
        meta = pq.ParquetFile(p).metadata
        assert [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] == groups
        pd.testing.assert_frame_equal(pd.read_parquet(p), df)
        assert not list(tmp_path.glob(f"t{rows}.part*"))


def test_an_excel_job_in_parts_over_the_limit_writes_every_table_as_zipped_csv(tmp_path):
    m, pc = _job(tmp_path)
    r = run_job(_cfg(tmp_path, m, pc, run_name="xl", chunk_size=CHUNK, output_format="xlsx", excel_max_rows=N - 1, portfolio_8760=True), log=lambda s: None)
    tables = {k: v.name for k, v in r.files.items() if k not in ("preflight", "run_settings")}
    assert set(tables) >= {"measure_level", "portfolio_summary", "portfolio_8760"} and all(n.endswith(".csv.zip") for n in tables.values()), tables
    assert any("every table" in n for n in r.notes)
    ok = run_job(_cfg(tmp_path, m, pc, run_name="xl_ok", chunk_size=CHUNK, output_format="xlsx", excel_max_rows=N), log=lambda s: None)
    assert ok.files["measure_level"].suffix == ".xlsx"                                # exactly at the limit: Excel


def test_runs_keep_the_results_group_sums(tmp_path, monkeypatch):
    """Both a run in one piece and a run in parts leave the kept Results sums beside their results, equal to the sums of the file itself. Neither reads
    the file back to make them: the run in one piece sums its frame, the run in parts adds up each part's sums as it writes it."""
    from recet import results_view as rv
    m, pc = _job(tmp_path)
    for chunk, fmt, extra in ((0, "parquet", {}), (CHUNK, "parquet", {}), (CHUNK, "csv", {}), (CHUNK, "csv.zip", {"components": True, "market_effects": 0.05})):
        def no_read_back(path):
            raise AssertionError("the run read its results file back to make the group sums")
        monkeypatch.setattr(rv, "group_sums_for", no_read_back)
        run = run_job(_cfg(tmp_path, m, pc, run_name=f"r{chunk}{fmt}", chunk_size=chunk, output_format=fmt, **extra), log=lambda s: None).run_dir
        monkeypatch.undo()
        src = outputs.results_source(run)
        kept = sorted((run / outputs.RESULTS_DIR).glob("group_sums*"))
        assert len(kept) == 1, kept
        pd.testing.assert_frame_equal(pd.read_parquet(kept[0]), rv.build_group_sums(src), check_dtype=False, rtol=1e-12)
        assert rv.RunResults.from_file(src).sums.equals(pd.read_parquet(kept[0]))        # the page uses the kept sums as they are
