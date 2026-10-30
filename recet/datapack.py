"""The reference data folder: building it, checking it, and moving it to another machine.

The big files (the two electric ACC parquets and the CEDARS impact-profile library, ~11 GB together) do not live in the repository. They live in a *reference data
folder* laid out as described in `recet.paths`, with a `manifest.json` recording each file's size and SHA-256:

  build_reference_folder(dest)         copy the current reference folder's files to a new folder (or hard link, same drive); the manifest names the pack
                                       (version, type) and each file (its type, ACC version, draft flag)
  set_draft(folder, acc, draft)        mark an ACC in a folder's manifest as a draft (BR-11.8) or as final
  verify(folder, full=False)           check the folder against its manifest (sizes; add full=True for the checksums)
  pack(folder, out_dir, part_gb=4)     write the folder as a plain (uncompressed -- parquet is already compressed) tar split into parts <= part_gb, e.g. to fit FAT32
                                       drives or upload limits, plus SHA256SUMS
  unpack(parts_dir, dest)              rebuild the folder from the parts

A DRAFT ACC (BRD v0.5 BR-11.8: a proposed ACC loaded before its release, for analysis) is usable for analysis runs only: a run on it is refused when it
asks for CEDARS filing checks or is priced for a filing (`acc_purpose`), and every other run labels its ACC "(draft)" in every output (jobs.apply_draft).
`file_info` finds a file's manifest entry; tieout.py writes the acceptance report a pack (and an engine release) is promoted on (BR-06.21).

Point the program at the folder with `recet data set-dir PATH`, the GUI's "Reference data folder" box, `--data-dir`, or the environment variable RECET_DATA.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths

MANIFEST = "manifest.json"
MANIFEST_FORMAT = 2                     # 2 (0.1.79): the pack block and each file's type, acc_version and draft; format 1 manifests are still read
PACK_TYPE = "reference"
Log = Callable[[str], None]
CHUNK = 16 * 1024 * 1024


def sha256_file(path: Path, log: Log | None = None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            if log and total > 512e6 and done % (CHUNK * 32) < CHUNK:
                log(f"    ... {done / total:.0%} of {path.name}")
    return h.hexdigest()


def _acc_of(key: str) -> str | None:
    return key.rsplit("_", 1)[1] if key.startswith("acc_electric_") else None


def build_reference_folder(dest: str | Path, *, link: bool = False, log: Log = print, checksums: bool = True, pack_version: str | None = None,
                           draft_accs: tuple[str, ...] | list[str] = ()) -> Path:
    """Copy (or hard-link, same drive only) every file in paths.REF_FILES that exists in the current reference folder into `dest` and write the manifest.
    `pack_version` names this pack (default: today's date); `draft_accs` lists the ACC versions whose electric file is a draft (BR-11.8)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for rf in paths.REF_FILES.values():
        src = paths.ref_path(rf.key)
        if src is None:
            log(f"  MISSING {rf.rel} ({rf.what})" + ("" if rf.required else " -- optional, skipped"))
            if rf.required:
                raise FileNotFoundError(f"cannot build the reference folder: {rf.rel} not found in the current reference folder")
            continue
        target = dest / rf.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == src.stat().st_size:
            log(f"  have {rf.rel}")
        else:
            log(f"  {'linking' if link else 'copying'} {rf.rel}  ({src.stat().st_size / 1e9:.2f} GB)")
            if link:
                if target.exists():
                    target.unlink()
                os.link(src, target)
            else:
                shutil.copyfile(src, target)
        files[rf.rel] = {"size": target.stat().st_size, "what": rf.what, "required": rf.required, "type": rf.key}
        if _acc_of(rf.key):
            files[rf.rel].update(acc_version=_acc_of(rf.key), draft=_acc_of(rf.key) in {str(a) for a in draft_accs})
    if checksums:
        for rel, info in files.items():
            log(f"  checksum {rel}")
            info["sha256"] = sha256_file(dest / rel, log)
    unknown = {str(a) for a in draft_accs} - {f.get("acc_version") for f in files.values()}
    if unknown:
        raise ValueError(f"draft ACC(s) {sorted(unknown)} are not in the folder")
    now = datetime.now()
    manifest = {"format": MANIFEST_FORMAT, "created": now.isoformat(timespec="seconds"),
                "pack": {"type": PACK_TYPE, "version": pack_version or now.strftime("%Y-%m-%d")}, "files": files}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    drafts = [f["acc_version"] for f in files.values() if f.get("draft")]
    log(f"reference folder ready: {dest} (pack version {manifest['pack']['version']}" + (f"; DRAFT ACC {', '.join(drafts)}" if drafts else "") + ")")
    return dest


def read_manifest(folder: str | Path) -> dict | None:
    mf = Path(folder) / MANIFEST
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def set_draft(folder: str | Path, acc: str, draft: bool = True, log: Log = print) -> None:
    """Mark the ACC version `acc`'s electric file in `folder`'s manifest as a draft (or, draft=False, as final)."""
    folder = Path(folder)
    manifest = read_manifest(folder)
    if manifest is None:
        raise FileNotFoundError(f"{folder} has no {MANIFEST}; run `recet data build` first")
    hit = [rel for rel, f in manifest["files"].items() if (f.get("acc_version") or _acc_of(f.get("type", "")) or _acc_of_rel(rel)) == str(acc)]
    if not hit:
        raise ValueError(f"no ACC {acc} electric file in {folder}'s manifest")
    for rel in hit:
        manifest["files"][rel].update(acc_version=str(acc), draft=bool(draft))
    manifest["format"] = max(int(manifest.get("format", 1)), MANIFEST_FORMAT)
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"{', '.join(hit)}: {'DRAFT (analysis runs only)' if draft else 'final'}")


def _acc_of_rel(rel: str) -> str | None:
    parts = Path(rel).parts
    return parts[1] if len(parts) == 3 and parts[0] == "acc" and parts[2] == "electric_hourly.parquet" else None


def file_info(path: str | Path, folder: str | Path | None = None) -> dict | None:
    """The manifest entry of `path` in reference folder `folder` (default: the current one), with "rel" and the pack block; None when the folder has no
    manifest or the manifest does not list the file at its present size."""
    folder = Path(folder) if folder else paths.reference_dir()
    if folder is None or path is None:
        return None
    try:
        rel = Path(path).resolve().relative_to(Path(folder).resolve()).as_posix()
    except ValueError:
        return None
    manifest = read_manifest(folder)
    info = (manifest or {}).get("files", {}).get(rel)
    if info is None or (Path(path).exists() and info.get("size") not in (None, Path(path).stat().st_size)):
        return None
    return {**info, "rel": rel, "pack": manifest.get("pack", {"type": PACK_TYPE, "version": manifest.get("created", "")[:10]})}


def verify(folder: str | Path, full: bool = False, log: Log = print) -> bool:
    """True if every file in the manifest is present with the right size (and checksum if full)."""
    folder = Path(folder)
    mf = folder / MANIFEST
    if not mf.exists():
        log(f"{folder} has no {MANIFEST}: not a reference data folder (or not built with `recet data build`)")
        return False
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    pack = manifest.get("pack")
    if pack:
        log(f"pack: {pack.get('type')} version {pack.get('version')} (manifest format {manifest.get('format')})")
    ok = True
    for rel, info in manifest["files"].items():
        p = folder / rel
        if not p.exists():
            log(f"  MISSING  {rel}")
            ok = False
        elif p.stat().st_size != info["size"]:
            log(f"  BAD SIZE {rel}: {p.stat().st_size:,} bytes, expected {info['size']:,}")
            ok = False
        elif full and info.get("sha256") and sha256_file(p, log) != info["sha256"]:
            log(f"  BAD CHECKSUM {rel}")
            ok = False
        else:
            log(f"  ok       {rel}  ({info['size'] / 1e9:.2f} GB)" + ("  checksum verified" if full and info.get("sha256") else "")
                + ("  DRAFT ACC: analysis runs only" if info.get("draft") else ""))
    log("reference data folder is complete" if ok else "reference data folder has problems")
    return ok


class _SplitWriter(io.RawIOBase):
    """Write a byte stream as consecutive files name.part001, name.part002 ... each at most `part_bytes`."""

    def __init__(self, base: Path, part_bytes: int):
        self.base, self.part_bytes, self.n, self.left, self.f = base, part_bytes, 0, 0, None
        self.parts: list[Path] = []

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        mv = memoryview(b)
        total = len(mv)
        while len(mv):
            if self.f is None or self.left == 0:
                if self.f:
                    self.f.close()
                self.n += 1
                path = self.base.with_name(f"{self.base.name}.part{self.n:03d}")
                self.parts.append(path)
                self.f, self.left = open(path, "wb"), self.part_bytes
            k = min(self.left, len(mv))
            self.f.write(mv[:k])
            self.left -= k
            mv = mv[k:]
        return total

    def close(self) -> None:
        if self.f:
            self.f.close()
        super().close()


class _JoinReader(io.RawIOBase):
    def __init__(self, parts: list[Path]):
        self.parts, self.i, self.f = parts, 0, None

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while True:
            if self.f is None:
                if self.i >= len(self.parts):
                    return 0
                self.f = open(self.parts[self.i], "rb")
                self.i += 1
            n = self.f.readinto(b)
            if n:
                return n
            self.f.close()
            self.f = None


def pack(folder: str | Path, out_dir: str | Path, part_gb: float = 4.0, log: Log = print) -> list[Path]:
    """Split the reference folder into transfer-sized parts (uncompressed tar). Returns the part files; SHA256SUMS is written beside them."""
    folder, out_dir = Path(folder), Path(out_dir)
    if not (folder / MANIFEST).exists():
        raise FileNotFoundError(f"{folder} has no {MANIFEST}; run `recet data build` first")
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = _SplitWriter(out_dir / "recet_reference_data.tar", max(1, int(part_gb * 1e9)))
    buffered = io.BufferedWriter(writer, CHUNK)
    with tarfile.open(fileobj=buffered, mode="w|") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                log(f"  packing {p.relative_to(folder)}  ({p.stat().st_size / 1e9:.2f} GB)")
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    buffered.flush()          # tarfile does not close a file object it was given: without this the last (up to 16 MB) of the archive is never written
    writer.close()
    sums = out_dir / "SHA256SUMS"
    with open(sums, "w", encoding="utf-8") as fh:
        for part in writer.parts:
            log(f"  checksum {part.name}")
            fh.write(f"{sha256_file(part)}  {part.name}\n")
    log(f"{len(writer.parts)} part(s) written to {out_dir}")
    return writer.parts


def unpack(parts_dir: str | Path, dest: str | Path, log: Log = print) -> Path:
    """Verify the parts against SHA256SUMS (if present), then extract them into `dest` (the reference data folder)."""
    parts_dir, dest = Path(parts_dir), Path(dest)
    parts = sorted(parts_dir.glob("recet_reference_data.tar.part*"))
    if not parts:
        raise FileNotFoundError(f"no recet_reference_data.tar.part* files in {parts_dir}")
    sums = parts_dir / "SHA256SUMS"
    if sums.exists():
        expected = dict(line.split("  ", 1)[::-1] for line in sums.read_text(encoding="utf-8").strip().splitlines())
        for part in parts:
            want = expected.get(part.name.strip())
            if want is not None and sha256_file(part) != want.strip():
                raise ValueError(f"checksum mismatch: {part.name} (re-copy it)")
            log(f"  checked {part.name}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BufferedReader(_JoinReader(parts), CHUNK), mode="r|") as tar:
        for member in tar:
            if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts:
                raise ValueError(f"unsafe path in archive: {member.name}")
            log(f"  extracting {member.name}")
            tar.extract(member, dest)
    log(f"reference data folder ready: {dest}")
    return dest
