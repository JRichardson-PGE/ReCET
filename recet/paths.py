"""Where the package finds its files.

Two kinds of data:

* **Package data** -- small tables that ship inside the package (`recet/data/`): E3 settings and tables, TOU maps and factors, the gas monthly
  profile library, statewide-allocation factors, and the two gas ACC extractions. Always found via `package_data()`.
* **Reference data** -- the large files that cannot live on GitHub (the 2024 and 2026 electric ACC hourly parquets, ~1 GB each, and the CEDARS
  impact-profile library, ~9 GB). They live in a *reference data folder* you can point anywhere (another drive, a network share, another machine).

The reference data folder is resolved, first match wins:

  1. `set_reference_dir()` for this process (the GUI's "Reference data folder" box and `--data-dir` do this),
  2. the environment variable `RECET_DATA`,
  3. the saved setting in `~/.recet/config.json` (`recet data set-dir PATH` or the GUI's "Save as default"; the config folder itself can be
     moved with `RECET_HOME`),
  4. the files where the CEDARS review research scripts wrote them, when a *research checkout* is configured (development machines only):
     the environment variable `RECET_RESEARCH_ROOT`, or `"research_root"` in `~/.recet/config.json`, naming the CEDARS-Review checkout.
     The research scripts' own datasets (`poc/data`, `DEV_DATA_DIR`) are found the same way.

Layout of the reference folder (`recet data status` lists what is present; `manifest.json` carries sizes and checksums):

  acc/2024/electric_hourly.parquet          2024 ACC hourly electric avoided costs (CET Version 2026)
  acc/2026/electric_hourly.parquet          draft 2026 ACC hourly electric avoided costs (CET Version 2027)
  impact_profiles/catalog.parquet           CEDARS impact-profile catalog
  impact_profiles/hourly.parquet            CEDARS 8,760-hour impact profiles, all IOUs
  cet_tables/scg_electric_ac_2026.parquet   OPTIONAL: CET's quarterly SCG electric table (2024 ACC / Version 2026 only)

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
ENV_RESEARCH = "RECET_RESEARCH_ROOT"


def _research_root() -> Path | None:
    """The CEDARS-Review research checkout, if this machine has one configured (env RECET_RESEARCH_ROOT, else "research_root" in the config
    file). Read once at import; None on an ordinary install."""
    p = os.environ.get(ENV_RESEARCH)
    if not p:
        try:
            cfg = Path(os.environ.get("RECET_HOME") or (Path.home() / ".recet")) / "config.json"
            p = json.loads(cfg.read_text(encoding="utf-8")).get("research_root")
        except (OSError, ValueError):
            p = None
    root = Path(p).expanduser() if p else None
    return root if root is not None and (root / "2024 ACC Extraction").exists() else None


DEV_ROOT: Path | None = _research_root()                                                        # None on an ordinary install
DEV_DATA_DIR = DEV_ROOT / "poc" / "data" if DEV_ROOT else PACKAGE_DIR.parent / "data"           # the research scripts' datasets

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
    return "not set" + (f" (using the research checkout's files: {DEV_ROOT})" if DEV_ROOT else "")


@dataclass(frozen=True)
class RefFile:
    key: str
    rel: str                 # path inside the reference folder
    what: str
    dev: Path | None         # where a source checkout keeps it
    required: bool = True


def _dev(*parts: str) -> Path | None:
    return DEV_ROOT.joinpath(*parts) if DEV_ROOT else None


_PROFILES_DEV = DEV_DATA_DIR / "impact_profiles_all_iou_full_2026-09-14"
REF_FILES: dict[str, RefFile] = {r.key: r for r in (
    RefFile("acc_electric_2024", "acc/2024/electric_hourly.parquet", "2024 ACC hourly electric avoided costs (CET Version 2026)",
            _dev("2024 ACC Extraction", "output", "ACC2024_electric_avoided_costs_hourly.parquet")),
    RefFile("acc_electric_2026", "acc/2026/electric_hourly.parquet", "draft 2026 ACC hourly electric avoided costs (CET Version 2027)",
            _dev("2026 ACC Extraction", "output", "ACC2026_electric_avoided_costs_hourly.parquet")),
    RefFile("impact_catalog", "impact_profiles/catalog.parquet", "CEDARS impact-profile catalog", _PROFILES_DEV / "cedars_all_iou_catalog.parquet"),
    RefFile("impact_hourly", "impact_profiles/hourly.parquet", "CEDARS 8,760-hour impact profiles (all IOUs)", _PROFILES_DEV / "impact_profiles_hourly.parquet"),
    RefFile("scg_cet_table", "cet_tables/scg_electric_ac_2026.parquet", "CET quarterly SCG electric avoided-cost table (optional, 2024 ACC only)",
            DEV_DATA_DIR / "elec_ac_scg_real" / "scg_electric_ac_2026.parquet", required=False),
)}


def ref_path(key: str) -> Path | None:
    """The file for `key`: the reference folder's copy if it exists, else the source checkout's, else None."""
    rf = REF_FILES[key]
    ref = reference_dir()
    if ref is not None and (ref / rf.rel).exists():
        return ref / rf.rel
    if rf.dev is not None and rf.dev.exists():
        return rf.dev
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
_runtime: dict[str, Path | None] = {"acc_electric": None, "acc_gas": None, "scg_table": None}


def configure_runtime(*, acc_electric: Path | None = None, acc_gas: Path | None = None, scg_table: Path | None = None) -> None:
    _runtime.update(acc_electric=Path(acc_electric) if acc_electric else None, acc_gas=Path(acc_gas) if acc_gas else None,
                    scg_table=Path(scg_table) if scg_table else None)


def _env_path(name: str) -> Path | None:
    return Path(os.environ[name]) if os.environ.get(name) else None


def current_acc_electric() -> Path | None:
    return _runtime["acc_electric"] or _env_path("POC_ACC_ELEC_PARQUET") or ref_path("acc_electric_2024")


def current_acc_gas() -> Path | None:
    p = _runtime["acc_gas"] or _env_path("POC_ACC_GAS_PARQUET") or package_data("acc", "2024", "gas_components.parquet")
    return p if p and p.exists() else None


def current_scg_table() -> Path | None:
    return _runtime["scg_table"] or ref_path("scg_cet_table")
