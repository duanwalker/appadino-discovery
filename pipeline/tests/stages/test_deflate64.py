"""Deflate64 (ZIP method 9) archive members — IRS TEOS XML archives use it for every
entry from 2025_TEOS_XML_05A on, and stdlib zipfile can't decode it. The 2026-11-08
Stage 2 validation run lost 2,532 of 10,481 filings (every one a 2025/2026
submission) to NotImplementedError("That compression method is not supported").

fixtures/deflate64_990.zip is a one-entry zip wrapped around the real, unmodified
Deflate64 bytes of a filing that failed in that run (object_id 202501289349304705,
from 2025_TEOS_XML_05A.zip; 990 filings are public records). Stdlib zipfile can't
write method 9, so the fixture's headers were written by hand around those bytes.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from discovery.stages.extract_signals import ZIP_DEFLATE64, fetch_filing_xml, parse_990_xml

FIXTURE = Path(__file__).parent / "fixtures" / "deflate64_990.zip"
OBJECT_ID = "202501289349304705"
MEMBER = f"{OBJECT_ID}_public.xml"


def _year_index(zip_path: Path) -> dict[str, tuple[str, str]]:
    return {OBJECT_ID: (str(zip_path), MEMBER)}


def test_fixture_is_deflate64_and_stdlib_zipfile_cannot_read_it() -> None:
    """Guards the fixture itself: if this stops raising, the test below no longer
    proves anything about the Deflate64 path."""
    with zipfile.ZipFile(FIXTURE) as archive:
        assert archive.getinfo(MEMBER).compress_type == ZIP_DEFLATE64
        with pytest.raises(NotImplementedError, match="compression method is not supported"):
            archive.read(MEMBER)


@pytest.mark.parametrize("cached", [False, True], ids=["transient_handle", "cached_handle"])
def test_fetch_filing_xml_decodes_real_deflate64_filing(cached: bool) -> None:
    archive_cache: dict[str, zipfile.ZipFile] | None = {} if cached else None
    try:
        xml_bytes = fetch_filing_xml(_year_index(FIXTURE), OBJECT_ID, archive_cache)
    finally:
        for archive in (archive_cache or {}).values():
            archive.close()

    assert xml_bytes is not None
    assert len(xml_bytes) == 33934
    parsed = parse_990_xml(xml_bytes)
    assert parsed["revenue_total"] == 2606176
    assert parsed["mission_text"] == "PROVIDE FINANCIAL SUPPORT FOR PUNAHOU SCHOOL"


def test_fetch_filing_xml_rejects_corrupted_deflate64_member(tmp_path: Path) -> None:
    """A Deflate64 member whose bytes don't match the recorded CRC raises (and is then
    counted as a per-filing failure by the orchestrator) instead of returning garbage."""
    corrupted = tmp_path / "corrupted.zip"
    shutil.copy(FIXTURE, corrupted)
    data = bytearray(corrupted.read_bytes())
    # The compressed bytes start right after the 30-byte local header and the name.
    data[30 + len(MEMBER) + 2000] ^= 0xFF
    corrupted.write_bytes(bytes(data))

    with pytest.raises(Exception):  # noqa: B017 - BadZipFile (CRC) or an inflate64 decode error
        fetch_filing_xml(_year_index(corrupted), OBJECT_ID)
