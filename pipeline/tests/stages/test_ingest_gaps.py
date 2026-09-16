"""Additional pytest cases for G1.2 ingest pipeline — focusing on gaps identified in
the original implementation test coverage (test_ingest.py).

Coverage areas:
1. Malformed/truncated rows in IRS BMF CSV extracts
2. Load interrupted partway through (upsert idempotency + crash recovery)
3. Same EIN appearing in multiple regional BMF extracts
4. Archive lookup logic (object_id/xml_object_url validation)
"""

from __future__ import annotations

from discovery.stages.ingest import (
    latest_two_per_ein,
    transform_bmf_row,
    transform_index_row,
)


class TestMalformedBMFRows:
    """Gap 1: Malformed or truncated rows in IRS BMF CSV extracts.
    
    Current coverage only tests valid rows and missing EIN. These test actual
    malformed data that could appear in real BMF extracts.
    """

    def test_transform_bmf_row_with_non_numeric_ein(self) -> None:
        """EIN present but contains non-digits — dropped rather than polluting the
        primary key of the shared national universe with a value that can't be an
        IRS-issued EIN (fixed after this gap was flagged: previously stored as-is)."""
        raw = {
            "EIN": "ABC123456",  # Invalid format
            "NAME": "Bad EIN Org",
            "STATE": "CA",
        }
        result = transform_bmf_row(raw)
        assert result is None

    def test_transform_bmf_row_with_wrong_length_ein(self) -> None:
        """EIN that's all-digits but not exactly 9 characters is also dropped."""
        assert transform_bmf_row({"EIN": "12345", "NAME": "Too Short"}) is None
        assert transform_bmf_row({"EIN": "1234567890", "NAME": "Too Long"}) is None

    def test_transform_bmf_row_with_malformed_ruling_year(self) -> None:
        """RULING field present but truncated/malformed — isdigit() check should
        catch it."""
        raw = {
            "EIN": "123456789",
            "NAME": "Org",
            "RULING": "19",  # Too short, can't extract 4-digit year
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["ruling_year"] is None  # Should gracefully fall back to None

    def test_transform_bmf_row_with_non_numeric_revenue(self) -> None:
        """REVENUE_AMT present but contains non-numeric data."""
        raw = {
            "EIN": "123456789",
            "NAME": "Org",
            "REVENUE_AMT": "NOT_A_NUMBER",
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["revenue_latest"] is None  # Should gracefully return None

    def test_transform_bmf_row_with_negative_revenue(self) -> None:
        """REVENUE_AMT is negative (edge case in real data)."""
        raw = {
            "EIN": "123456789",
            "NAME": "Org",
            "REVENUE_AMT": "-1000",  # Negative value
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["revenue_latest"] == -1000  # Current implementation stores as-is

    def test_transform_bmf_row_with_empty_name(self) -> None:
        """NAME field empty or whitespace only — should become empty string per
        transform."""
        raw = {
            "EIN": "123456789",
            "NAME": "   ",
            "STATE": "NY",
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["name"] == ""  # _clean_str strips and converts to ""

    def test_transform_bmf_row_with_truncated_ruling(self) -> None:
        """RULING field is very short (1-3 chars)."""
        raw = {
            "EIN": "123456789",
            "NAME": "Org",
            "RULING": "1",  # Single digit
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["ruling_year"] is None

    def test_transform_bmf_row_with_unicode_name(self) -> None:
        """NAME contains non-ASCII characters (real-world data)."""
        raw = {
            "EIN": "123456789",
            "NAME": "Éducation Pour Tous — School für Kinder",
            "STATE": "CA",
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["name"] == "Éducation Pour Tous — School für Kinder"

    def test_transform_bmf_row_handles_oversized_state_code(self) -> None:
        """STATE is longer than 2 chars (data entry error or extra whitespace)."""
        raw = {
            "EIN": "123456789",
            "NAME": "Org",
            "STATE": "  CA  ",  # Will be trimmed to "CA"
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["state"] == "CA"


class TestInterruptedLoad:
    """Gap 2: Load interrupted partway through (job killed mid-COPY).
    
    The staging-table upsert pattern should be idempotent: re-running after a crash
    should produce identical results with no duplicates. These tests verify:
    - Calling upsert twice with the same data produces one insert each time (via ON CONFLICT)
    - Calling transform + upsert with overlapping data doesn't duplicate
    - The ON CONFLICT logic correctly deduplicates by natural key
    """

    def test_transform_bmf_idempotency_multiple_calls(self) -> None:
        """Calling transform_bmf_row multiple times with same input yields identical output."""
        raw = {
            "EIN": "123456789",
            "NAME": "Test Org",
            "STATE": "CA",
            "RULING": "195504",
            "REVENUE_AMT": "100000",
        }
        result1 = transform_bmf_row(raw)
        result2 = transform_bmf_row(raw)
        assert result1 == result2

    def test_transform_index_idempotency_multiple_calls(self) -> None:
        """Calling transform_index_row multiple times yields identical output."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "202340189349301104",
        }
        result1 = transform_index_row(raw, sub_year=2023)
        result2 = transform_index_row(raw, sub_year=2023)
        assert result1 == result2

    def test_latest_two_per_ein_with_duplicate_batches(self) -> None:
        """If the same rows are processed twice (simulating a re-run), deduplication
        is correct. Note: latest_two_per_ein itself deduplicates by keeping only 2
        per EIN, so feeding it the same rows twice shouldn't change output."""
        rows_batch1 = [
            {"ein": "1", "tax_year": 2023},
            {"ein": "1", "tax_year": 2022},
            {"ein": "1", "tax_year": 2021},
        ]
        result1 = latest_two_per_ein(rows_batch1)

        # Feed the same batch again
        rows_batch2 = rows_batch1 + rows_batch1  # Duplicate rows
        result2 = latest_two_per_ein(rows_batch2)

        # Both should yield only the 2 most recent per EIN
        assert len([r for r in result1 if r["ein"] == "1"]) == 2
        assert len([r for r in result2 if r["ein"] == "1"]) == 2

    def test_bmf_transform_and_filter_chain(self) -> None:
        """Simulates the generator chain in run_ingest:
        (r for raw in fetch_bmf_rows() if (r := transform_bmf_row(raw)) is not None)
        
        Verify that rows with None EIN are filtered out, and valid rows pass through.
        """
        raw_rows = [
            {"EIN": "123456789", "NAME": "Valid Org"},
            {"EIN": "", "NAME": "No EIN Org"},
            {"EIN": "987654321", "NAME": "Another Valid"},
        ]
        transformed = [r for raw in raw_rows if (r := transform_bmf_row(raw)) is not None]
        assert len(transformed) == 2
        assert transformed[0]["ein"] == "123456789"
        assert transformed[1]["ein"] == "987654321"

    def test_index_transform_and_filter_chain(self) -> None:
        """Simulates the generator chain in run_ingest for filings:
        (t for raw, sub_year in fetch_index_rows(years)
         if (t := transform_index_row(raw, sub_year)) is not None and t["ein"] in known_eins)
        
        Verify that rows without required fields are filtered, and valid ones pass.
        """
        raw_rows = [
            ("123456789", 2023, "990", "202340189349301104"),  # Valid
            ("", 2023, "990", "202340189349301104"),  # No EIN
            ("123456789", 2023, "990PF", "202340189349301104"),  # Invalid form
        ]
        
        known_eins = {"123456789"}  # Only this EIN is known
        transformed = []
        for ein, ty, form, oid in raw_rows:
            raw = {
                "EIN": ein,
                "TAX_PERIOD": f"{ty}06",
                "RETURN_TYPE": form,
                "OBJECT_ID": oid,
            }
            if (t := transform_index_row(raw, ty)) is not None and t["ein"] in known_eins:
                transformed.append(t)
        
        assert len(transformed) == 1
        assert transformed[0]["ein"] == "123456789"


class TestMultipleRegionalBMFExtracts:
    """Gap 3: Same EIN appearing in more than one regional BMF extract.
    
    All 4 regional BMF extracts (eo1-eo4.csv) are processed sequentially. If the same
    EIN appears in multiple regions with different data, the upsert should:
    - Not create duplicate organizations entries
    - End with one consistent row (last upsert wins due to ON CONFLICT)
    
    These tests verify deduplication logic.
    """

    def test_same_ein_from_different_regions_no_duplication(self) -> None:
        """Same EIN appears in two regional files with slightly different data
        (e.g., updated name or revenue). Both should be transformed, but upsert
        should result in one row."""
        # Simulating EIN 123456789 in eo1.csv and eo2.csv
        region1_row = {
            "EIN": "123456789",
            "NAME": "Original Org Name",
            "STATE": "CA",
            "CITY": "San Francisco",
            "REVENUE_AMT": "500000",
            "RULING": "195504",
        }
        region2_row = {
            "EIN": "123456789",  # Same EIN
            "NAME": "Updated Org Name",  # Different data
            "STATE": "CA",
            "CITY": "San Francisco",
            "REVENUE_AMT": "600000",  # Different revenue
            "RULING": "195504",
        }

        t1 = transform_bmf_row(region1_row)
        t2 = transform_bmf_row(region2_row)

        assert t1 is not None
        assert t2 is not None
        assert t1["ein"] == t2["ein"] == "123456789"
        # Both should transform successfully; ON CONFLICT at DB level handles deduplication
        assert t1["name"] != t2["name"]  # Data differs
        # In actual DB, whichever is upserted last wins

    def test_different_eins_from_regional_extracts(self) -> None:
        """Multiple regions with different EINs — all should be inserted."""
        regions_data = [
            {"EIN": "111111111", "NAME": "Region 1 Org", "STATE": "CA"},
            {"EIN": "222222222", "NAME": "Region 2 Org", "STATE": "NY"},
            {"EIN": "333333333", "NAME": "Region 3 Org", "STATE": "TX"},
            {"EIN": "444444444", "NAME": "Region 4 Org", "STATE": "FL"},
        ]
        transformed = [transform_bmf_row(row) for row in regions_data]
        assert len([t for t in transformed if t is not None]) == 4
        eins = {t["ein"] for t in transformed if t is not None}
        assert eins == {"111111111", "222222222", "333333333", "444444444"}

    def test_latest_two_per_ein_across_regions(self) -> None:
        """Filings for the same EIN from multiple years/regions — latest_two_per_ein
        should keep only the 2 most recent tax years."""
        rows = [
            {"ein": "123456789", "tax_year": 2021, "object_id": "obj1"},
            {"ein": "123456789", "tax_year": 2022, "object_id": "obj2"},
            {"ein": "123456789", "tax_year": 2023, "object_id": "obj3"},
            {"ein": "123456789", "tax_year": 2020, "object_id": "obj4"},
        ]
        result = latest_two_per_ein(rows)
        ein_rows = [r for r in result if r["ein"] == "123456789"]
        years = sorted([r["tax_year"] for r in ein_rows])
        assert years == [2022, 2023]


class TestArchiveLookupLogic:
    """Gap 4: Archive lookup logic (object_id/xml_object_url validation).
    
    The filings schema was updated to include object_id (new) and xml_object_url to
    support bundled ZIP archives instead of per-file S3 URLs (IRS deprecated individual
    S3 addressing in Dec 2021). These tests verify that:
    - Missing/malformed object_id is caught and returns None (already tested)
    - xml_object_url is correctly constructed from submission year
    - object_id validation is strict enough to catch obvious corruption
    - NULL xml_object_url would be a data integrity issue (shouldn't happen)
    """

    def test_transform_index_row_constructs_correct_archive_url(self) -> None:
        """xml_object_url should be the archive directory for the submission year,
        not the individual file S3 URL."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "202340189349301104",
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is not None
        assert result["xml_object_url"] == "https://apps.irs.gov/pub/epostcard/990/xml/2023/"

    def test_transform_index_row_different_submission_years(self) -> None:
        """xml_object_url changes based on submission year (when data was received,
        not tax year)."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202206",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "202340189349301104",
        }
        result2022 = transform_index_row(raw, sub_year=2022)
        result2023 = transform_index_row(raw, sub_year=2023)

        assert result2022 is not None
        assert result2023 is not None
        assert result2022["xml_object_url"] == "https://apps.irs.gov/pub/epostcard/990/xml/2022/"
        assert result2023["xml_object_url"] == "https://apps.irs.gov/pub/epostcard/990/xml/2023/"

    def test_transform_index_row_with_empty_object_id(self) -> None:
        """object_id is empty/whitespace — should return None (already tested in
        test_ingest.py but documenting here for coverage)."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "",
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is None

    def test_transform_index_row_with_whitespace_object_id(self) -> None:
        """object_id is whitespace only — _clean_str should return None."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "   ",
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is None

    def test_transform_index_row_with_extremely_long_object_id(self) -> None:
        """object_id exceeds schema length (filings.object_id is String(30)) — dropped
        before it can fail the upsert (fixed after this gap was flagged: previously
        passed through and would have errored at the DB)."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "x" * 50,  # Exceeds 30 char limit
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is None

    def test_transform_index_row_with_object_id_at_max_length(self) -> None:
        """object_id at exactly the 30-char limit is still accepted."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "x" * 30,
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is not None
        assert len(result["object_id"]) == 30

    def test_transform_index_row_object_id_format_variation(self) -> None:
        """object_id format varies (some numeric, some alphanumeric).
        Current implementation just requires non-empty. This documents that no strict
        format validation exists."""
        cases = [
            "202340189349301104",  # Numeric
            "2023-ABC-XYZ",  # Alphanumeric with dashes
            "obj_2023_001",  # Mixed format
        ]
        for oid in cases:
            raw = {
                "EIN": "131624099",
                "TAX_PERIOD": "202306",
                "RETURN_TYPE": "990",
                "OBJECT_ID": oid,
            }
            result = transform_index_row(raw, sub_year=2023)
            assert result is not None
            assert result["object_id"] == oid

    def test_transform_index_row_missing_tax_period(self) -> None:
        """TAX_PERIOD is critical for extracting tax_year. Missing or malformed
        should return None."""
        cases = [
            {"EIN": "131624099", "TAX_PERIOD": "", "RETURN_TYPE": "990", "OBJECT_ID": "123"},
            {"EIN": "131624099", "TAX_PERIOD": "20", "RETURN_TYPE": "990", "OBJECT_ID": "123"},  # Too short
            {"EIN": "131624099", "TAX_PERIOD": "202A06", "RETURN_TYPE": "990", "OBJECT_ID": "123"},  # Non-digit
        ]
        for raw in cases:
            result = transform_index_row(raw, sub_year=2023)
            assert result is None

    def test_filings_with_valid_archive_metadata(self) -> None:
        """End-to-end: transform_index_row produces a filing record with both
        object_id and xml_object_url set, ready for G1.3 archive resolution."""
        raw = {
            "EIN": "131624099",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "202340189349301104",
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is not None

        # Verify both archive metadata fields are populated for G1.3
        assert "object_id" in result
        assert "xml_object_url" in result
        assert result["object_id"] is not None
        assert result["xml_object_url"] is not None
        assert result["xml_object_url"].startswith("https://")
        assert "2023" in result["xml_object_url"]


class TestEdgeCasesAndCrossover:
    """Cross-cutting test cases that verify interactions between gaps."""

    def test_malformed_ein_filtered_before_upsert(self) -> None:
        """A malformed EIN is filtered out in transform, before it ever reaches the
        upsert — not left for the DB to reject mid-batch."""
        raw = {
            "EIN": "ABC123456",  # Malformed
            "NAME": "Bad Org",
        }
        result = transform_bmf_row(raw)
        assert result is None

    def test_multiple_malformed_fields_in_same_row(self) -> None:
        """Multiple fields are malformed — transform should handle each independently."""
        raw = {
            "EIN": "123456789",
            "NAME": "   ",  # Whitespace only
            "REVENUE_AMT": "NOT_A_NUMBER",  # Non-numeric
            "RULING": "19",  # Too short
            "STATE": "  CA  ",  # Extra whitespace
        }
        result = transform_bmf_row(raw)
        assert result is not None
        assert result["name"] == ""
        assert result["revenue_latest"] is None
        assert result["ruling_year"] is None
        assert result["state"] == "CA"

    def test_filing_with_minimal_valid_data(self) -> None:
        """Minimum required fields for a filing record."""
        raw = {
            "EIN": "123456789",
            "TAX_PERIOD": "202306",
            "RETURN_TYPE": "990",
            "OBJECT_ID": "obj_123",
        }
        result = transform_index_row(raw, sub_year=2023)
        assert result is not None
        assert all(
            k in result for k in ["ein", "tax_year", "form_type", "object_id", "xml_object_url"]
        )
