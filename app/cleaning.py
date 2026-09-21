"""
SLA Monitoring — data cleaning logic.

Pure pandas, no AWS/Lambda knowledge — kept independently testable.
Each function does exactly one job; clean_dataframe() runs them in order.
"""

import pandas as pd


def normalize_timestamp(raw_ts: str) -> pd.Timestamp:
    """
    Convert a single timestamp value into a timezone-aware UTC pandas Timestamp.

    Handles three formats found in the data:
      1. ISO 8601 UTC:         "2025-04-11T17:30:00Z"
      2. Unix epoch string:    "1748729700"
      3. ISO 8601 with offset: "2025-04-26T04:00:00+05:30"

    Epoch values are assumed to represent UTC seconds (the standard meaning
    of Unix epoch) — there's no timezone info attached to disambiguate.
    """
    raw_ts = str(raw_ts).strip()

    if raw_ts.isdigit():
        return pd.to_datetime(int(raw_ts), unit="s", utc=True)

    # pandas' to_datetime with utc=True parses "...Z" directly as UTC, and
    # CONVERTS "+05:30" offsets to UTC (not just relabels them)
    return pd.to_datetime(raw_ts, utc=True)


def normalize_latency_ms(raw_latency, unit: str) -> float:
    """
    Convert a latency value into milliseconds, given its unit ('ms' or 's').

    Returns NaN for missing/unparseable values or unrecognized units —
    never guesses a number. Downstream, flag_invalid_latency() decides
    what to do with NaN/negative values.
    """
    try:
        value = float(raw_latency)
    except (TypeError, ValueError):
        return float("nan")

    unit = str(unit).strip().lower()
    if unit == "ms":
        return value
    elif unit == "s":
        return value * 1000
    else:
        return float("nan")


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Remove duplicate check records, keyed on (service_id, timestamp, agent).

      1. Exact duplicates (every column matches) — drop the copy.
      2. Conflicting duplicates (same key, differing values) — keep the
         more COMPLETE row (fewest missing fields); "first row wins" would
         risk silently discarding the only good copy.

    Requires df to already have a numeric 'latency_ms' column.
    Returns (deduplicated_df, report_dict) — the report feeds the README's
    data-findings numbers.
    """
    key_cols = ["service_id", "timestamp", "agent"]
    report = {
        "input_rows": len(df),
        "exact_duplicates_dropped": 0,
        "conflicting_duplicates_resolved": 0,
    }

    df = df.assign(_missing_count=df.isna().sum(axis=1))

    df = df.drop_duplicates(
        subset=df.columns.difference(["_missing_count"]).tolist() + key_cols,
        keep="first",
    )
    before_conflict = len(df)

    df = df.sort_values("_missing_count").drop_duplicates(subset=key_cols, keep="first")
    after = len(df)

    report["exact_duplicates_dropped"] = report["input_rows"] - before_conflict
    report["conflicting_duplicates_resolved"] = before_conflict - after

    df = df.drop(columns=["_missing_count"]).sort_index()
    return df, report


def flag_invalid_status(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flag rows whose status_code falls outside the valid HTTP range (100-599)
    — generic check, not hardcoded to the 999 sentinel we found.

    These rows are NOT dropped: every observed 999 row has a plausible
    latency and falls outside every documented incident window, pointing to
    an agent-side reporting glitch rather than a real failure. We exclude
    them from the uptime/SLA calc (neither success nor failure — unknown)
    but keep them visible in the logs, flagged.

    Adds: is_valid_status (bool), exclude_reason (str|None)
    """
    status_numeric = pd.to_numeric(df["status_code"], errors="coerce")
    is_valid = status_numeric.between(100, 599)

    df = df.assign(
        is_valid_status=is_valid,
        exclude_reason=df.get("exclude_reason"),
    )
    df.loc[~is_valid, "exclude_reason"] = "invalid_status_code"
    return df


def flag_invalid_latency(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flag rows with a physically invalid latency (negative or missing).

    Does NOT affect SLA/uptime inclusion — status_code already determines
    success/failure independent of whether the latency number is trustworthy.
    Only affects which rows count toward latency stats (avg/p95).

    Adds: is_valid_latency (bool)
    """
    is_missing = df["latency_ms"].isna()
    is_negative = df["latency_ms"] < 0
    return df.assign(is_valid_latency=~(is_missing | is_negative))


def clean_dataframe(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Full pipeline, run in order: timestamp -> latency -> dedup -> status flag
    -> latency flag. This is the single entry point handler.py should call.

    Returns (cleaned_df, report) where report merges the dedup stats with
    counts of flagged rows, for logging / the upload response.
    """
    df = raw.copy()
    df["timestamp_utc"] = df["timestamp"].apply(normalize_timestamp)
    df["latency_ms"] = df.apply(
        lambda r: normalize_latency_ms(r["latency"], r["latency_unit"]), axis=1
    )

    df, dedup_report = deduplicate(df)
    df = flag_invalid_status(df)
    df = flag_invalid_latency(df)

    report = {
        **dedup_report,
        "output_rows": len(df),
        "invalid_status_rows": int((~df.is_valid_status).sum()),
        "invalid_latency_rows": int((~df.is_valid_latency).sum()),
    }
    return df, report