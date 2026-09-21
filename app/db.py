"""
SLA Monitoring — Supabase persistence.

Isolated from handler.py and cleaning.py deliberately: if the DB choice
ever changed, only this file would need to.
"""

import os

from supabase import create_client, Client


def get_client() -> Client:
    """
    Reads connection details from environment variables (set via .env
    locally, and as Lambda environment variables in production — never
    hardcoded, never committed).
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def _rows_to_records(df, source_file: str) -> list[dict]:
    """
    Convert the cleaned DataFrame into the list-of-dicts shape the Supabase
    client expects, matching the `checks` table columns exactly.

    Uses upsert semantics downstream (on the unique service_id/timestamp/
    agent constraint), so re-uploading the same file is safe — it updates
    existing rows rather than duplicating them.
    """
    records = []
    for _, row in df.iterrows():
        records.append({
            "service_id": row["service_id"],
            "service_name": row.get("service_name"),
            "timestamp_utc": row["timestamp_utc"].isoformat(),
            "status_code": int(row["status_code"]),
            "latency_ms": None if pd_isna(row["latency_ms"]) else float(row["latency_ms"]),
            "is_valid_status": bool(row["is_valid_status"]),
            "is_valid_latency": bool(row["is_valid_latency"]),
            "exclude_reason": row.get("exclude_reason"),
            "agent": row["agent"],
            "region": row.get("region"),
            "source_file": source_file,
        })
    return records


def pd_isna(value) -> bool:
    """Tiny local wrapper so this module doesn't need a top-level pandas
    import just for one null check — keeps db.py's dependency surface small."""
    import pandas as pd
    return pd.isna(value)


def insert_rows(df, source_file: str, batch_size: int = 500) -> dict:
    """
    Upsert cleaned rows into Supabase, chunked into batches (Postgres/
    Supabase can reject very large single inserts, and chunking also means
    a failure partway through doesn't lose the rows already written).

    Returns a small report: how many rows were written, in how many batches.
    """
    client = get_client()
    records = _rows_to_records(df, source_file)

    written = 0
    batches = 0
    for i in range(0, len(records), batch_size):
        chunk = records[i : i + batch_size]
        client.table("checks").upsert(chunk, on_conflict="service_id,timestamp_utc,agent").execute()
        written += len(chunk)
        batches += 1

    return {"rows_written": written, "batches": batches}