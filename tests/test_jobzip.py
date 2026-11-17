"""The web app's job zip (recet/web/jobzip.py): Measure.csv with an optional ProgramCost.csv, nothing else, never more than the unzipped limit."""
from __future__ import annotations

import io
import zipfile

import pytest

from recet.web import jobzip

MB = 1024 * 1024


def _zip(tmp_path, members: dict[str, bytes], name="job.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in members.items():
            z.writestr(n, data)
    return p


def test_measure_and_program_cost_come_out_under_fixed_names(tmp_path):
    p = _zip(tmp_path, {"Measure.csv": b"a|b\n1|2\n", "ProgramCost.csv": b"PrgID\nX\n"})
    got = jobzip.unpack(p, tmp_path / "out", 10 * MB)
    assert set(got) == {"measures", "programcost"}
    assert got["measures"].name == "measures.csv" and got["measures"].read_bytes() == b"a|b\n1|2\n"
    assert got["programcost"].read_bytes() == b"PrgID\nX\n"


def test_program_cost_is_optional_and_names_are_case_blind_in_one_folder(tmp_path):
    p = _zip(tmp_path, {"my job/MEASURE.TXT": b"x\n", "__MACOSX/my job/._MEASURE.TXT": b"junk", "my job/.DS_Store": b"junk", "my job/": b""})
    got = jobzip.unpack(p, tmp_path / "out", MB)
    assert set(got) == {"measures"} and got["measures"].read_bytes() == b"x\n"


@pytest.mark.parametrize("members, message", [
    ({"ProgramCost.csv": b"x"}, "no Measure.csv"),
    ({"Measure.csv": b"x", "notes.docx": b"x"}, "only Measure.csv and ProgramCost.csv"),
    ({"Measure.csv": b"x", "a/b/ProgramCost.csv": b"x"}, "only Measure.csv and ProgramCost.csv"),       # deeper than one folder
    ({"Measure.csv": b"x", "copy/measure.csv": b"x"}, "more than one Measure"),
    ({"Measure.xlsx": b"x"}, "only Measure.csv and ProgramCost.csv"),
])
def test_unusable_zips_are_refused_with_a_reason(tmp_path, members, message):
    with pytest.raises(jobzip.JobZipError, match=message):
        jobzip.unpack(_zip(tmp_path, members), tmp_path / "out", MB)
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_not_a_zip(tmp_path):
    p = tmp_path / "job.zip"
    p.write_bytes(b"Measure.csv is not a zip")
    with pytest.raises(jobzip.JobZipError, match="not a valid .zip"):
        jobzip.unpack(p, tmp_path / "out", MB)


def test_the_unzipped_limit_holds_whatever_the_headers_say(tmp_path):
    big = b"0|" * (MB // 2) + b"\n"                                         # ~1 MB that compresses to almost nothing
    with pytest.raises(jobzip.JobZipError, match="unzipped limit"):
        jobzip.unpack(_zip(tmp_path, {"Measure.csv": big}), tmp_path / "out", MB // 2)
    # a zip whose header understates the size is still stopped while copying, and leaves nothing behind
    p = _zip(tmp_path, {"Measure.csv": big}, "lying.zip")
    raw = bytearray(p.read_bytes())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as z:
        size = z.getinfo("Measure.csv").file_size
    raw = raw.replace(size.to_bytes(4, "little"), (10).to_bytes(4, "little"))  # local and central headers both claim 10 bytes
    p.write_bytes(bytes(raw))
    with pytest.raises(jobzip.JobZipError):
        jobzip.unpack(p, tmp_path / "out2", MB // 2)
    assert not (tmp_path / "out2" / "measures.csv").exists()


def test_a_crafted_path_cannot_write_outside_the_folder(tmp_path):
    p = _zip(tmp_path, {"../Measure.csv": b"x\n"})
    got = jobzip.unpack(p, tmp_path / "out", MB)
    assert got["measures"] == tmp_path / "out" / "measures.csv"
    assert not (tmp_path / "Measure.csv").exists()
