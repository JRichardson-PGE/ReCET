"""The combined 8,760 by climate zone and its peaks (BRD BR-06.13): the zones add up to the portfolio, the peaks table reads the profile, and a run in
parts gives the same files."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recet import outputs, profile8760
from recet.jobs import run_job
from test_chunked import CHUNK, _cfg, _job, _same

QUIET = dict(log=lambda s: None)


def test_profile_by_climate_zone_and_its_peaks(tmp_path):
    m, pc = _job(tmp_path)
    meas = pd.read_csv(m, sep="|", dtype=str, keep_default_na=False)
    meas["AC_Climate_Zone"] = ["12" if i % 3 == 0 else "13" for i in range(len(meas))]
    meas.to_csv(m, sep="|", index=False)
    kw = dict(portfolio_8760=True, profile_years=[2028, 2029])
    whole = run_job(_cfg(tmp_path, m, pc, run_name="whole", chunk_size=0, **kw), **QUIET)
    parts = run_job(_cfg(tmp_path, m, pc, run_name="parts", chunk_size=CHUNK, **kw), **QUIET)
    prof = outputs.read_table(whole.files["portfolio_8760"])
    for y in (2028, 2029):
        assert {f"net_kwh_{y}_CZ12", f"net_kwh_{y}_CZ13", f"net_kwh_{y}_PGE"} <= set(prof.columns)
        np.testing.assert_allclose(prof[f"net_kwh_{y}_CZ12"] + prof[f"net_kwh_{y}_CZ13"], prof[f"net_kwh_{y}"], rtol=1e-12)
        assert prof[f"net_kwh_{y}_CZ12"].sum() > 0 and prof[f"net_kwh_{y}_CZ13"].sum() > prof[f"net_kwh_{y}_CZ12"].sum()
    pk = outputs.read_table(whole.files["portfolio_8760_peaks"])
    assert pk[pk["year"] == 2028]["group"].tolist() == ["portfolio", "IOU territory (PA)", "climate zone", "climate zone"]
    for y in (2028, 2029):
        rows = pk[pk["year"] == y].set_index("value")
        col = prof[f"net_kwh_{y}"]
        assert rows.loc["all measures", "peak_kwh"] == pytest.approx(col.max()) and rows.loc["all measures", "peak_hour_of_year"] == int(col.idxmax()) + 1
        assert rows.loc["all measures", "net_kwh"] == pytest.approx(col.sum())
        assert rows.loc[["CZ12", "CZ13"], "share_of_portfolio_peak"].sum() == pytest.approx(1.0)
        assert rows.loc["PGE", "share_of_portfolio_peak"] == pytest.approx(1.0)
    for key in ("portfolio_8760", "portfolio_8760_peaks"):
        _same(outputs.read_table(whole.files[key]), outputs.read_table(parts.files[key]))


def test_peaks_report_added_load():
    prof = pd.DataFrame({"hour_of_year": np.arange(1, 8761), "net_kwh_2028": np.r_[[-5.0], np.ones(8759)], "net_kwh_2028_CZ1": np.r_[[-5.0], np.ones(8759)]})
    pk = profile8760.peaks(prof, [2028]).set_index("value")
    assert pk.loc["CZ1", "max_added_load_hour_of_year"] == 1 and pk.loc["CZ1", "max_added_load_kwh"] == -5.0 and pk.loc["CZ1", "peak_kwh"] == 1.0
    assert profile8760.zone_label("13") == "CZ13" and profile8760.zone_label("CZ13") == "CZ13" and profile8760.zone_label(13.0) == "CZ13"
