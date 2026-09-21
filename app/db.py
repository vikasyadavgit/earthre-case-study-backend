"""
SLA Monitoring — Supabase persistence.

Isolated from handler.py and cleaning.py deliberately: if the DB choice
ever changed, only this file would need to.
"""

import os

from supabase import create_client, Client

SLA_THRESHOLD = 99.9   # percent — credit kicks in below this


def get_client() -> Client:
    """
    Reads connection details from environment variables (set via .env
    locally, and as Lambda environment variables in production — never
    hardcoded, never committed).
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def pd_isna(value) -> bool:
    """Tiny local wrapper so this module doesn't need a top-level pandas
    import just for one null check — keeps db.py's dependency surface small."""
    import pandas as pd
    return pd.isna(value)


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
            "latency_ms": (
                None if pd_isna(row["latency_ms"]) else float(row["latency_ms"])
            ),
            "is_valid_status": bool(row["is_valid_status"]),
            "is_valid_latency": bool(row["is_valid_latency"]),
            "exclude_reason": row.get("exclude_reason"),
            "agent": row["agent"],
            "region": row.get("region"),
            "source_file": source_file,
        })
    return records


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
        chunk = records[i: i + batch_size]
        client.table("checks").upsert(
            chunk, on_conflict="service_id,timestamp_utc,agent"
        ).execute()
        written += len(chunk)
        batches += 1

    return {"rows_written": written, "batches": batches}


# -------------------------------------------------------------------
# Read APIs — Dashboard + Checks
# -------------------------------------------------------------------

def get_dashboard_data(from_date: str | None = None, to_date: str | None = None) -> dict:
    """
    Return aggregated SLA metrics for the dashboard stats panel.

    SLA/availability definition (matches README assumptions):
      - Rows with is_valid_status=False (e.g. 999) are EXCLUDED from the
        denominator entirely (neither success nor failure — unknown).
      - Among the remaining rows: success = HTTP 2xx (200-299).
      - availability = (2xx_count / valid_total) * 100

    Supports optional date range filtering via from_date / to_date
    (ISO 8601 strings, e.g. "2025-05-08T00:00:00Z").

    NOTE: Supabase PostgREST caps a plain SELECT at 1000 rows by default.
    We paginate in PAGE_SIZE chunks until we have every row, so stats are
    computed on the full dataset — not just the first 1000 records.
    """
    client = get_client()
    PAGE_SIZE = 1000

    rows: list[dict] = []
    offset = 0
    while True:
        query = client.table("checks").select("*")
        if from_date:
            query = query.gte("timestamp_utc", from_date)
        if to_date:
            query = query.lte("timestamp_utc", to_date)
        batch = query.range(offset, offset + PAGE_SIZE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break   # last page — we have everything
        offset += PAGE_SIZE

    total_checks = len(rows)

    # Rows with a valid HTTP status code (excludes 999-type sentinels)
    valid_status_rows = [r for r in rows if r.get("is_valid_status") is True]

    # Success = 2xx among valid-status rows only
    success_rows = [r for r in valid_status_rows if 200 <= (r.get("status_code") or 0) <= 299]

    # Latency stats — only from rows where latency is valid (not missing/negative)
    latency_values = sorted(
        float(r["latency_ms"])
        for r in rows
        if r.get("latency_ms") is not None and r.get("is_valid_latency") is True
    )

    avg_latency = sum(latency_values) / len(latency_values) if latency_values else 0

    if latency_values:
        p95_idx = max(0, int(len(latency_values) * 0.95) - 1)
        p95_latency = latency_values[p95_idx]
    else:
        p95_latency = 0

    availability = (
        len(success_rows) / len(valid_status_rows) * 100
        if valid_status_rows else 0
    )

    # Per-service breakdown
    service_map: dict = {}
    for row in rows:
        sid = row.get("service_id")
        if sid not in service_map:
            service_map[sid] = {
                "service_id": sid,
                "service_name": row.get("service_name"),
                "total": 0,
                "valid_status": 0,
                "success": 0,
                "latencies": [],
            }
        s = service_map[sid]
        s["total"] += 1
        if row.get("is_valid_status") is True:
            s["valid_status"] += 1
            if 200 <= (row.get("status_code") or 0) <= 299:
                s["success"] += 1
        if row.get("latency_ms") is not None and row.get("is_valid_latency") is True:
            s["latencies"].append(float(row["latency_ms"]))

    services = []
    for s in service_map.values():
        svc_availability = (
            s["success"] / s["valid_status"] * 100 if s["valid_status"] else 0
        )
        svc_latencies = s["latencies"]
        svc_avg = sum(svc_latencies) / len(svc_latencies) if svc_latencies else 0
        services.append({
            "service_id": s["service_id"],
            "service_name": s["service_name"],
            "total_checks": s["total"],
            "availability": round(svc_availability, 2),
            "avg_latency_ms": round(svc_avg, 2),
            "sla_met": svc_availability >= SLA_THRESHOLD,
        })

    services.sort(key=lambda x: x["availability"])  # worst first — on-call-friendly

    return {
        "total_checks": total_checks,
        "valid_status_checks": len(valid_status_rows),
        "success_checks": len(success_rows),
        "invalid_checks": total_checks - len(valid_status_rows),
        "availability": round(availability, 2),
        "sla_met": availability >= SLA_THRESHOLD,
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency_ms": round(p95_latency, 2),
        "services": services,
    }


def get_checks(
    service_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Return paginated check records, optionally filtered by service and/or
    date range. Ordered newest-first (most relevant for on-call triage).

    from_date / to_date: ISO 8601 strings, e.g. "2025-05-08" or
    "2025-05-08T00:00:00Z" — Postgres accepts both for timestamptz.
    """
    client = get_client()

    query = (
        client.table("checks")
        .select("*", count="exact")
        .order("timestamp_utc", desc=True)
        .range(offset, offset + limit - 1)
    )

    if service_id:
        query = query.eq("service_id", service_id)
    if from_date:
        query = query.gte("timestamp_utc", from_date)
    if to_date:
        query = query.lte("timestamp_utc", to_date)

    response = query.execute()

    return {
        "total": response.count or 0,
        "limit": limit,
        "offset": offset,
        "checks": response.data or [],
    }