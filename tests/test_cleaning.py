"""
Tests for app/cleaning.py — covers each data-quality issue we found in the
provided CSVs, plus the full pipeline end to end.

Run with: pytest tests/test_cleaning.py -v
"""

import math

import pandas as pd
import pytest

from app.cleaning import (
    normalize_timestamp,
    normalize_latency_ms,
    deduplicate,
    flag_invalid_status,
    flag_invalid_latency,
    clean_dataframe,
)


class TestNormalizeTimestamp:
    def test_iso_utc(self):
        result = normalize_timestamp("2025-04-11T17:30:00Z")
        assert result == pd.Timestamp("2025-04-11 17:30:00", tz="UTC")

    def test_epoch_seconds(self):
        # 1700000000 -> a known, fixed UTC instant
        result = normalize_timestamp("1700000000")
        assert result == pd.Timestamp(1700000000, unit="s", tz="UTC")

    def test_offset_converted_not_relabeled(self):
        # 04:00 IST (+05:30) must become 22:30 UTC the PREVIOUS day —
        # this is the exact bug this function exists to prevent
        result = normalize_timestamp("2025-04-26T04:00:00+05:30")
        assert result == pd.Timestamp("2025-04-25 22:30:00", tz="UTC")


class TestNormalizeLatency:
    def test_ms_passthrough(self):
        assert normalize_latency_ms(245, "ms") == 245.0

    def test_seconds_converted_to_ms(self):
        assert normalize_latency_ms(1.2, "s") == 1200.0

    def test_missing_value_is_nan(self):
        assert math.isnan(normalize_latency_ms("", "ms"))

    def test_unknown_unit_is_nan(self):
        # must not silently guess what an unrecognized unit means
        assert math.isnan(normalize_latency_ms(300, "min"))

    def test_negative_still_converts(self):
        # unit conversion's only job is units — validity is a separate step
        assert normalize_latency_ms(-223, "ms") == -223.0


class TestDeduplicate:
    def test_exact_duplicate_dropped(self):
        df = pd.DataFrame([
            {"service_id": "svc-a", "timestamp": "t1", "agent": "agent-1", "latency_ms": 100.0},
            {"service_id": "svc-a", "timestamp": "t1", "agent": "agent-1", "latency_ms": 100.0},
        ])
        result, report = deduplicate(df)
        assert len(result) == 1
        assert report["exact_duplicates_dropped"] == 1
        assert report["conflicting_duplicates_resolved"] == 0

    def test_conflicting_duplicate_keeps_more_complete_row(self):
        df = pd.DataFrame([
            {"service_id": "svc-a", "timestamp": "t1", "agent": "agent-1", "latency_ms": float("nan")},
            {"service_id": "svc-a", "timestamp": "t1", "agent": "agent-1", "latency_ms": 269.0},
        ])
        result, report = deduplicate(df)
        assert len(result) == 1
        assert report["conflicting_duplicates_resolved"] == 1
        assert result.iloc[0]["latency_ms"] == 269.0

    def test_distinct_rows_untouched(self):
        df = pd.DataFrame([
            {"service_id": "svc-a", "timestamp": "t1", "agent": "agent-1", "latency_ms": 100.0},
            {"service_id": "svc-a", "timestamp": "t2", "agent": "agent-1", "latency_ms": 100.0},
        ])
        result, report = deduplicate(df)
        assert len(result) == 2
        assert report["exact_duplicates_dropped"] == 0


class TestFlagInvalidStatus:
    def test_valid_codes_pass(self):
        df = pd.DataFrame({"status_code": ["200", "500", "502", "503"]})
        result = flag_invalid_status(df)
        assert result["is_valid_status"].all()
        assert result["exclude_reason"].isna().all()

    def test_out_of_range_code_flagged(self):
        df = pd.DataFrame({"status_code": ["200", "999"]})
        result = flag_invalid_status(df)
        assert result["is_valid_status"].tolist() == [True, False]
        assert result.loc[1, "exclude_reason"] == "invalid_status_code"


class TestFlagInvalidLatency:
    def test_valid_latency_passes(self):
        df = pd.DataFrame({"latency_ms": [100.0, 250.5]})
        result = flag_invalid_latency(df)
        assert result["is_valid_latency"].all()

    def test_missing_and_negative_flagged(self):
        df = pd.DataFrame({"latency_ms": [100.0, float("nan"), -223.0]})
        result = flag_invalid_latency(df)
        assert result["is_valid_latency"].tolist() == [True, False, False]


class TestCleanDataframePipeline:
    def test_full_pipeline_runs_and_reports_match(self):
        raw = pd.DataFrame([
            {  # normal row
                "service_id": "svc-a", "service_name": "svc-a-api",
                "timestamp": "2025-04-11T17:30:00Z", "status_code": "200",
                "latency": "245", "latency_unit": "ms",
                "agent": "agent-1", "region": "ap-south-1",
            },
            {  # exact duplicate of the row above
                "service_id": "svc-a", "service_name": "svc-a-api",
                "timestamp": "2025-04-11T17:30:00Z", "status_code": "200",
                "latency": "245", "latency_unit": "ms",
                "agent": "agent-1", "region": "ap-south-1",
            },
            {  # invalid status
                "service_id": "svc-b", "service_name": "svc-b-api",
                "timestamp": "2025-04-11T18:00:00Z", "status_code": "999",
                "latency": "150", "latency_unit": "ms",
                "agent": "agent-1", "region": "ap-south-1",
            },
        ])
        cleaned, report = clean_dataframe(raw)

        assert report["input_rows"] == 3
        assert report["exact_duplicates_dropped"] == 1
        assert report["output_rows"] == 2
        assert report["invalid_status_rows"] == 1
        assert len(cleaned) == report["output_rows"]