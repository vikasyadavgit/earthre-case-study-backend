-- Run this in the Supabase SQL editor to create the table.
-- Matches the columns produced by app/cleaning.py's clean_dataframe().

create table checks (
    id                 bigserial primary key,
    service_id         text not null,
    service_name       text,
    timestamp_utc      timestamptz not null,
    status_code        integer not null,
    latency_ms         double precision,
    is_valid_status    boolean not null,
    is_valid_latency   boolean not null,
    exclude_reason     text,
    agent              text not null,
    region             text,
    source_file        text,          -- original uploaded filename, for traceability across uploads
    uploaded_at        timestamptz not null default now(),

    -- Prevents duplicate rows if the same file (or overlapping data) is
    -- uploaded more than once — re-uploading is idempotent rather than
    -- creating duplicate check records.
    unique (service_id, timestamp_utc, agent)
);

-- Indexes for the dashboard's expected query patterns: filtering by date
-- range, and aggregating stats per service.
create index idx_checks_timestamp on checks (timestamp_utc);
create index idx_checks_service_timestamp on checks (service_id, timestamp_utc);