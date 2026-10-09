"""Where the package finds its files.

Two kinds of data:

* **Package data** -- small tables that ship inside the package (`recet/data/`): E3 settings and tables, the gas monthly
  profile library, statewide-allocation factors, and the two gas ACC extractions. Always found via `package_data()`.
* **Reference data** -- the large files that cannot live on GitHub (the 2024 and 2026 electric ACC hourly parquets, ~1 GB each, and the CEDARS
  impact-profile library, ~9 GB). They live in a *reference data folder* you can point anywhere (another drive, a network share, another machine).

The reference data folder is resolved, first match wins:

  1. `set_reference_dir()` for this process (the GUI's "Reference data folder" box and `--data-dir` do this),
  2. the environment variable `RECET_DATA`,
  3. the saved setting in `~/.recet/config.json` (`recet data set-dir PATH` or the GUI's "Save as default"; the config folder itself can be
     moved with `RECET_HOME`).

Layout of the reference folder (`recet data status` lists what is present; `manifest.json` carries sizes and checksums):

  acc/2024/electric_hourly.parquet          2024 ACC hourly electric avoided costs (CET Version 2026)
  acc/2026/electric_hourly.parquet          draft 2026 ACC hourly electric avoided costs (CET Version 2027)
  impact_profiles/catalog.parquet           CEDARS impact-profile catalog
  impact_profiles/hourly.parquet            CEDARS 8,760-hour impact profiles, all IOUs

The ACC files actually used in a run are chosen at run time (`configure_runtime`), so one process can run several ACC versions; the older
environment variables `POC_ACC_ELEC_PARQUET` / `POC_ACC_GAS_PARQUET` still work as a default when nothing is configured.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PKG_DATA = PACKAGE_DIR / "data"
ENV_DATA = "RECET_DATA"


def package_data(*parts: str) -> Path:
    return PKG_DATA.joinpath(*parts)


# --------------------------------------------------------------------------------------------------------------------------- config file
def config_dir() -> Path:
    return Path(os.environ.get("RECET_HOME") or (Path.home() / ".recet"))


def config_path() -> Path:
    return config_dir() / "config.json"


def cache_dir() -> Path | None:
    """Where derived, rebuildable data is cached between runs (the ACC as memory-mapped arrays, profile shapes): `~/.recet/cache`, or `RECET_CACHE`;
    None when `RECET_NO_CACHE` is set. Nothing in it is ever the only copy of anything: delete it any time (`recet data clear-cache`)."""
    if os.environ.get("RECET_NO_CACHE"):
        return None
    return Path(os.environ["RECET_CACHE"]).expanduser() if os.environ.get("RECET_CACHE") else config_dir() / "cache"


def read_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**values) -> Path:
    cfg = read_config()
    cfg.update({k: v for k, v in values.items()})
    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return config_path()


# ------------------------------------------------------------------------------------------------------------------- reference data folder
_explicit_ref: Path | None = None


def set_reference_dir(path: str | Path | None, persist: bool = False) -> None:
    """Use `path` as the reference data folder for this process (None clears it). persist=True also saves it as the default."""
    global _explicit_ref
    _explicit_ref = Path(path).expanduser() if path else None
    if persist and path:
        save_config(data_dir=str(Path(path).expanduser()))


def reference_dir() -> Path | None:
    if _explicit_ref is not None:
        return _explicit_ref
    if os.environ.get(ENV_DATA):
        return Path(os.environ[ENV_DATA]).expanduser()
    saved = read_config().get("data_dir")
    return Path(saved).expanduser() if saved else None


def reference_dir_source() -> str:
    if _explicit_ref is not None:
        return "set for this run"
    if os.environ.get(ENV_DATA):
        return f"environment variable {ENV_DATA}"
    if read_config().get("data_dir"):
        return f"saved default ({config_path()})"
    return "not set"


@dataclass(frozen=True)
class RefFile:
    key: str
    rel: str                 # path inside the reference folder
    what: str
    required: bool = True


REF_FILES: dict[str, RefFile] = {r.key: r for r in (
    RefFile("acc_electric_2024", "acc/2024/electric_hourly.parquet", "2024 ACC hourly electric avoided costs (CET Version 2026)"),
    RefFile("acc_electric_2026", "acc/2026/electric_hourly.parquet", "draft 2026 ACC hourly electric avoided costs (CET Version 2027)"),
    RefFile("impact_catalog", "impact_profiles/catalog.parquet", "CEDARS impact-profile catalog"),
    RefFile("impact_hourly", "impact_profiles/hourly.parquet", "CEDARS 8,760-hour impact profiles (all IOUs)"),
)}


def ref_path(key: str) -> Path | None:
    """The file for `key` in the reference folder, or None if it is not there."""
    rf = REF_FILES[key]
    ref = reference_dir()
    if ref is not None and (ref / rf.rel).exists():
        return ref / rf.rel
    return None


def ref_status() -> list[dict]:
    rows = []
    for rf in REF_FILES.values():
        p = ref_path(rf.key)
        rows.append({"key": rf.key, "file": rf.rel, "what": rf.what, "required": rf.required, "found": p is not None,
                     "path": str(p) if p else "", "size_gb": round(p.stat().st_size / 1e9, 2) if p else None})
    return rows


def missing_required() -> list[str]:
    return [r["file"] for r in ref_status() if r["required"] and not r["found"]]


# ------------------------------------------------------------------------------------------------------------------- run-time ACC selection
_runtime: dict[str, Path | None] = {"acc_electric": None, "acc_gas": None}


def configure_runtime(*, acc_electric: Path | None = None, acc_gas: Path | None = None) -> None:
    _runtime.update(acc_electric=Path(acc_electric) if acc_electric else None, acc_gas=Path(acc_gas) if acc_gas else None)


def _env_path(name: str) -> Path | None:
    return Path(os.environ[name]) if os.environ.get(name) else None


def current_acc_electric() -> Path | None:
    return _runtime["acc_electric"] or _env_path("POC_ACC_ELEC_PARQUET") or ref_path("acc_electric_2024")


def current_acc_gas() -> Path | None:
    p = _runtime["acc_gas"] or _env_path("POC_ACC_GAS_PARQUET") or package_data("acc", "2024", "gas_components.parquet")
    return p if p and p.exists() else None
