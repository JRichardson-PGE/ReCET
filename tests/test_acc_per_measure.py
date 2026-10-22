"""A different ACC version per measure (BRD BR-06.5): the optional ACCVersion column prices each measure on its own ACC within one run."""
from __future__ import annotations

import pytest

from recet import versions
from recet.jobs import RunConfig, run_job
from recet.pipeline import discount_settings
from conftest import expected_flat_value, make_electric_acc, make_flat_profile_csv, make_measure_csv


def _two_measures(tmp_path, second_version="2024"):
    a = make_measure_csv(tmp_path / "a.csv", CEInputID="ON_RUN", ACCVersion="").read_text(encoding="utf-8").splitlines()
    b = make_measure_csv(tmp_path / "b.csv", CEInputID="ON_OTHER", ACCVersion=second_version).read_text(encoding="utf-8").splitlines()
    path = tmp_path / "Measure.csv"
    path.write_text("\n".join([a[0], a[1], b[1]]) + "\n", encoding="utf-8")
    return path


def _reference(tmp_path):
    """A reference folder holding a synthetic 2026 ACC at $100/MWh and a 2024 ACC at $50/MWh."""
    ref = tmp_path / "ref"
    for year, energy in ((2026, 60.0), (2024, 10.0)):
        (ref / "acc" / str(year)).mkdir(parents=True)
        make_electric_acc(ref / "acc" / str(year) / "electric_hourly.parquet", energy=energy, ghg=40.0)
    return ref


def _rate(acc):
    return discount_settings.get_discount_rate_qtr("PGE", acc)


def test_each_measure_is_priced_on_its_own_acc_version(tmp_path):
    r = run_job(RunConfig(measures=str(_two_measures(tmp_path)), out_dir=str(tmp_path / "out"), run_name="mixed", acc_version="2026",
                          reference_dir=str(_reference(tmp_path)), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False),
                log=lambda s: None)
    f = r.frame.set_index("CEInputID")
    assert f.loc["ON_RUN", "ElecBen"] == pytest.approx(expected_flat_value(1000, 0.100, _rate(2026)), rel=1e-12)
    assert f.loc["ON_OTHER", "ElecBen"] == pytest.approx(expected_flat_value(1000, 0.050, _rate(2024)), rel=1e-12)
    assert f.loc["ON_RUN", "POC_ACC"] == versions.acc_label(2026) and f.loc["ON_OTHER", "POC_ACC"] == versions.acc_label(2024)
    assert list(f["poc_acc_version"]) == ["2026", "2024"]


def test_unknown_acc_version_is_a_preflight_error(tmp_path):
    with pytest.raises(RuntimeError, match="preflight"):
        run_job(RunConfig(measures=str(_two_measures(tmp_path, "1999")), out_dir=str(tmp_path / "out"), run_name="bad", acc_version="2026",
                          reference_dir=str(_reference(tmp_path)), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")), emissions=False),
                log=lambda s: None)
    assert "bad_acc_version" in (next((tmp_path / "out").rglob("preflight.txt"))).read_text(encoding="utf-8")
