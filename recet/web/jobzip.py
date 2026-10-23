"""The web app's job upload: one .zip holding Measure.csv and, optionally, ProgramCost.csv, the way CET takes a job.

Bare CSV files are not accepted for the job; the zip keeps a large job inside the upload limit (a 1,000,000-row Measure.csv of 603 MB zips to 38 MB).

What a zip may hold: files named Measure and ProgramCost (any case, .csv or .txt), at the top or in one folder; macOS / Windows extras (`__MACOSX/`, files whose
name starts with a dot, Thumbs.db) are skipped; anything else is refused, so a wrong file is never silently ignored. Each file is copied out by itself to a
fixed name in the run's folder (the member's own path is never used, so a crafted path cannot write elsewhere), counting bytes as it goes: the total unzipped
size is capped (`max_bytes`), whatever the zip's headers claim.
"""
from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

NAMES = {"measure": "measures", "programcost": "programcost"}     # file stem (lower case) -> the job's input key
SUFFIXES = (".csv", ".txt")
SKIPPED_NAMES = {"thumbs.db", "desktop.ini"}


class JobZipError(ValueError):
    """The zip cannot be used; the message says why, for the person who uploaded it."""


def _skipped(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return any(p == "__MACOSX" for p in parts) or PurePosixPath(name).name.startswith(".") or PurePosixPath(name).name.lower() in SKIPPED_NAMES


def unpack(zip_path: Path, dest: Path, max_bytes: int) -> dict[str, Path]:
    """Copy Measure.csv (required) and ProgramCost.csv (optional) out of `zip_path` into `dest` as measures.csv / programcost.csv. Returns
    {"measures": path, "programcost": path (when given)}. Raises JobZipError for anything that is not a usable job zip."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise JobZipError("the job file is not a valid .zip") from None
    with z:
        found: dict[str, zipfile.ZipInfo] = {}
        other: list[str] = []
        for info in z.infolist():
            if info.is_dir() or _skipped(info.filename):
                continue
            p = PurePosixPath(info.filename.replace("\\", "/"))
            key = NAMES.get(p.stem.lower()) if p.suffix.lower() in SUFFIXES else None
            if key is None or len(p.parts) > 2:
                other.append(info.filename)
                continue
            if key in found:
                raise JobZipError(f"the zip has more than one {'Measure' if key == 'measures' else 'ProgramCost'} file "
                                  f"({found[key].filename}, {info.filename})")
            found[key] = info
        if other:
            shown = ", ".join(other[:5]) + (f" and {len(other) - 5} more" if len(other) > 5 else "")
            raise JobZipError(f"the zip may hold only Measure.csv and ProgramCost.csv; it also has {shown}")
        if "measures" not in found:
            raise JobZipError("the zip has no Measure.csv")
        if any(i.flag_bits & 0x1 for i in found.values()):
            raise JobZipError("the zip is password-protected; upload it without a password")
        limit_mb = max_bytes // (1024 * 1024)
        if sum(i.file_size for i in found.values()) > max_bytes:
            raise JobZipError(f"the files in the zip are larger than the {limit_mb:,} MB unzipped limit")
        dest.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        total = 0
        try:
            for key, info in found.items():
                target = dest / f"{key}.csv"
                with z.open(info) as src, open(target, "wb") as f:
                    while block := src.read(1 << 20):
                        total += len(block)
                        if total > max_bytes:
                            raise JobZipError(f"the files in the zip are larger than the {limit_mb:,} MB unzipped limit")
                        f.write(block)
                out[key] = target
        except (JobZipError, zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
            for key in NAMES.values():                              # nothing half-written is left behind
                (dest / f"{key}.csv").unlink(missing_ok=True)
            if isinstance(exc, JobZipError):
                raise
            raise JobZipError(f"the zip could not be read: {exc}") from None
        return out
