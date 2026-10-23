"""Legacy CET field names (E3ClimateZone, E3MeaElecEndUseShape ...) are accepted on input and read under ReCET's names (recet.fieldnames)."""
from __future__ import annotations

import pandas as pd
import pytest

from recet import fieldnames, preflight
from recet.jobs import RunConfig, run_job
from conftest import make_electric_acc, make_flat_profile_csv, make_measure_csv


def _legacy(path_in, path_out):
    """The same Measure.csv with CET's (E3) names in the header."""
    lines = path_in.read_text(encoding="utf-8").splitlines()
    head = "|".join(fieldnames.RECET_TO_CET.get(c, c) for c in lines[0].split("|"))
    path_out.write_text("\n".join([head] + lines[1:]) + "\n", encoding="utf-8")
    return path_out


def _run(tmp_path, measures, name):
    return run_job(RunConfig(measures=str(measures), out_dir=str(tmp_path / "out"), run_name=name, acc_version="2026",
                             acc_electric_file=str(make_electric_acc(tmp_path / "acc.parquet")), profiles_8760_file=str(make_flat_profile_csv(tmp_path / "p.csv")),
                             emissions=False), log=lambda s: None).frame.iloc[0]


def test_a_file_with_cets_legacy_names_gives_the_same_results(tmp_path):
    new = make_measure_csv(tmp_path / "new.csv")
    old = _legacy(new, tmp_path / "old.csv")
    assert "E3MeaElecEndUseShape" in old.read_text(encoding="utf-8").splitlines()[0]
    a, b = _run(tmp_path, new, "new"), _run(tmp_path, old, "old")
    assert b["ElecBen"] == pytest.approx(a["ElecBen"], rel=1e-12) and b["AC_Climate_Zone"] == a["AC_Climate_Zone"] == "13"
    assert "E3ClimateZone" not in b.index                                  # outputs use ReCET's names
    assert not preflight.check_inputs(old).errors


def test_every_legacy_name_maps_and_both_names_at_once_is_an_error():
    assert {fieldnames.recet_name(old) for old in fieldnames.LEGACY_TO_RECET} == set(fieldnames.LEGACY_TO_RECET.values())
    assert fieldnames.recet_name("e3climatezone") == "AC_Climate_Zone"           # case-insensitive, as CET is
    with pytest.raises(ValueError, match="both the legacy and the ReCET name"):
        fieldnames.to_recet(pd.DataFrame(columns=["E3ClimateZone", "AC_Climate_Zone"]))
