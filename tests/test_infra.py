import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from recet import cli, datapack, overrides, paths, templates, versions


# ------------------------------------------------------------------------------------------------------------------ reference folder resolution
def _fake_ref(tmp_path, monkeypatch):
    """Two tiny fake reference files in a source reference folder (tmp_path/src), set as the current folder. Returns {key: path}."""
    src = tmp_path / "src"
    files, paths_by_key = {}, {}
    for key, rel in (("acc_electric_2024", "acc/2024/electric_hourly.parquet"), ("impact_hourly", "impact_profiles/hourly.parquet")):
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(np.random.default_rng(len(rel)).bytes(50_000))
        files[key] = paths.RefFile(key, rel, key)
        paths_by_key[key] = f
    monkeypatch.setattr(paths, "REF_FILES", files)
    paths.set_reference_dir(src)
    return paths_by_key


def test_reference_dir_precedence(tmp_path, monkeypatch):
    assert paths.reference_dir() is None
    paths.save_config(data_dir=str(tmp_path / "saved"))
    assert paths.reference_dir() == tmp_path / "saved" and "saved default" in paths.reference_dir_source()
    monkeypatch.setenv("RECET_DATA", str(tmp_path / "env"))
    assert paths.reference_dir() == tmp_path / "env"
    paths.set_reference_dir(tmp_path / "explicit")
    assert paths.reference_dir() == tmp_path / "explicit"


def test_ref_path_reads_the_reference_folder(tmp_path, monkeypatch):
    files = _fake_ref(tmp_path, monkeypatch)
    assert paths.ref_path("impact_hourly") == files["impact_hourly"]
    folder = tmp_path / "ref"
    (folder / "impact_profiles").mkdir(parents=True)
    (folder / "impact_profiles" / "hourly.parquet").write_bytes(b"x")
    paths.set_reference_dir(folder)
    assert paths.ref_path("impact_hourly") == folder / "impact_profiles" / "hourly.parquet"    # the current folder's copy
    assert paths.ref_path("acc_electric_2024") is None                                          # not in this folder
    monkeypatch.setattr(paths, "REF_FILES", {**paths.REF_FILES, "gone": paths.RefFile("gone", "x/y", "nothing")})
    assert paths.ref_path("gone") is None and "x/y" in paths.missing_required()


# ------------------------------------------------------------------------------------------------------------------ data packaging
def test_build_verify_pack_unpack_roundtrip(tmp_path, monkeypatch):
    files = _fake_ref(tmp_path, monkeypatch)
    ref = datapack.build_reference_folder(tmp_path / "ref", log=lambda s: None)
    manifest = json.loads((ref / "manifest.json").read_text())
    assert set(manifest["files"]) == {paths.REF_FILES[k].rel for k in files}
    assert manifest["files"]["impact_profiles/hourly.parquet"]["sha256"] == hashlib.sha256(files["impact_hourly"].read_bytes()).hexdigest()
    assert datapack.verify(ref, full=True, log=lambda s: None)

    parts = datapack.pack(ref, tmp_path / "parts", part_gb=2e-5, log=lambda s: None)              # 20 kB parts -> several of them
    assert len(parts) > 3 and (tmp_path / "parts" / "SHA256SUMS").exists()
    out = datapack.unpack(tmp_path / "parts", tmp_path / "restored", log=lambda s: None)
    assert datapack.verify(out, full=True, log=lambda s: None)
    for rel in manifest["files"]:
        assert (out / rel).read_bytes() == (ref / rel).read_bytes()


def test_verify_detects_damage(tmp_path, monkeypatch):
    _fake_ref(tmp_path, monkeypatch)
    ref = datapack.build_reference_folder(tmp_path / "ref", log=lambda s: None)
    target = ref / "acc" / "2024" / "electric_hourly.parquet"
    data = bytearray(target.read_bytes())
    data[100] ^= 0xFF
    target.write_bytes(bytes(data))
    assert datapack.verify(ref, full=False, log=lambda s: None)                                  # same size: only the checksum can tell
    assert not datapack.verify(ref, full=True, log=lambda s: None)
    target.write_bytes(b"short")
    assert not datapack.verify(ref, log=lambda s: None)
    target.unlink()
    assert not datapack.verify(ref, log=lambda s: None)


def test_unpack_rejects_a_corrupted_part(tmp_path, monkeypatch):
    _fake_ref(tmp_path, monkeypatch)
    ref = datapack.build_reference_folder(tmp_path / "ref", log=lambda s: None)
    parts = datapack.pack(ref, tmp_path / "parts", part_gb=2e-5, log=lambda s: None)
    parts[1].write_bytes(parts[1].read_bytes()[:-10] + b"0123456789")
    with pytest.raises(ValueError, match="checksum mismatch"):
        datapack.unpack(tmp_path / "parts", tmp_path / "out", log=lambda s: None)


# ------------------------------------------------------------------------------------------------------------------ templates
def test_every_template_passes_its_own_validator(tmp_path):
    written = templates.write_all(tmp_path / "t")
    names = {p.name for p in written}
    assert {"template_electric_8760_profiles.csv", "template_gas_monthly_profiles.csv", "template_acc_electric.csv", "template_acc_gas.csv",
            "template_Measure_header.csv", "INPUT_FORMATS.md"} <= names
    u = overrides.load_user_profiles(tmp_path / "t" / "template_electric_8760_profiles.csv")
    assert len(u) == 1 and abs(u.entries[0].zones["ALL"].sum() - 1) < 1e-9
    prof, _ = overrides.load_user_gas_profiles(tmp_path / "t" / "template_gas_monthly_profiles.csv")
    assert len(prof) == 1
    assert overrides.check_electric_acc(tmp_path / "t" / "template_acc_electric.csv").ok
    header = (tmp_path / "t" / "template_Measure_header.csv").read_text().strip().split("|")
    from recet.preflight import REQUIRED_MEASURE_COLUMNS
    assert set(REQUIRED_MEASURE_COLUMNS) <= set(header)


def test_unknown_template_kind():
    with pytest.raises(ValueError):
        templates.write_template("nonsense", "x.csv")


# ------------------------------------------------------------------------------------------------------------------ versions
def test_builtin_versions_and_replacement(tmp_path):
    a = versions.resolve_acc("2024")
    assert a.cet_version == 2026 and a.gas.name == "gas_components.parquet" and a.gas_source == "built-in"
    b = versions.resolve_acc("2026", electric_file=tmp_path / "mine.parquet", gas_file=tmp_path / "g.parquet")
    assert b.cet_version == 2027 and "replaces the built-in 2026" in b.electric_source and b.gas == tmp_path / "g.parquet"
    c = versions.resolve_acc("2030", electric_file=tmp_path / "e.parquet", e3_version=2026)
    assert c.cet_version == 2026 and c.label == "Custom ACC '2030'" and any("no gas ACC supplied" in n for n in c.notes)


# ------------------------------------------------------------------------------------------------------------------ CLI
def test_cli_version_and_template(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and "recet" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["template", "profiles-gas", "--out", str(tmp_path / "g.csv")])
    assert e.value.code == 0 and (tmp_path / "g.csv").exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "profiles-gas", str(tmp_path / "g.csv")])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "acc-gas", str(tmp_path / "missing.csv")])
    assert e.value.code == 1


def test_cli_config_flags_override_a_saved_config(tmp_path):
    saved = tmp_path / "job.json"
    saved.write_text(json.dumps({"measures": "m.csv", "acc_version": "2024", "components": True}))
    a = cli.build_parser().parse_args(["run", "--config", str(saved), "--acc", "2026", "--no-societal"])
    cfg = cli._config_from_args(a)
    assert cfg.measures == "m.csv" and cfg.acc_version == "2026" and cfg.components is True and cfg.include_societal is False


def test_data_set_dir_saves_the_default(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["data", "set-dir", str(tmp_path / "somewhere")])
    assert paths.read_config()["data_dir"] == str(tmp_path / "somewhere")


# ------------------------------------------------------------------------------------------------------------------ GUI
def test_gui_config_roundtrip():
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    try:
        from recet.gui import App
        from recet.jobs import RunConfig
        app = App(root)
        cfg = RunConfig(measures="C:/m/Measure.csv", programcost="C:/m/ProgramCost.csv", out_dir="C:/out", run_name="x", acc_version="2026", base_year=2028,
                        profiles_8760_file="C:/p.csv", acc_electric_file="C:/a.parquet", e3_version=2027, components=True, portfolio_8760=True, profile_years=[2028, 2030],
                        market_effects=0.05, include_societal=False, emissions=False, allocate_statewide=True, compare_with="2024",
                        cet_outputs="C:/cet.csv", output_format="parquet")
        app.apply_config(cfg)
        back = app.to_config()
        for field in ("measures", "programcost", "out_dir", "run_name", "acc_version", "base_year", "profiles_8760_file", "acc_electric_file", "e3_version", "components",
                      "portfolio_8760", "profile_years", "market_effects", "include_societal", "emissions", "allocate_statewide", "compare_with",
                      "cet_outputs", "output_format"):
            assert getattr(back, field) == getattr(cfg, field), field
        app.v["acc_version"].set("2030-draft")                                       # a typed new label is kept as is
        assert app.to_config().acc_version == "2030-draft"
    finally:
        root.destroy()
