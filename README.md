# SLA Monitoring Dashboard

> **Note:** Supabase PostgREST silently caps plain `SELECT` responses at 1000 rows. `get_dashboard_data()` paginates in 1000-row chunks to ensure SLA stats are always computed on the full dataset.

## 1. Architecture

| Piece | Choice | Why |
|---|---|---|
| Upload UI + Dashboard | Next.js, deployed on Vercel | Single app hosts both the upload screen and the dashboard; Vercel's free tier gives an instant, stable live URL with zero config |
| Stateless cloud function | Plain Python Lambda handler (no FastAPI) on AWS Lambda | Three routes, so a framework's routing/middleware would be pure overhead; a plain handler is smaller to deploy, has a faster cold start, and is easier to explain line-by-line than a framework with an ASGI adapter layer |
| Persistence | Supabase (Postgres) | Free tier, no card required; a relational schema fits the query pattern (filter by date range, aggregate by service) much more naturally than a NoSQL store would |
| Repos | Two separate repos (frontend / backend) | Vercel deploys per-repo; keeping them separate avoids backend commits triggering unnecessary frontend redeploys and keeps commit history clean per service |

### API routes (all served by one Lambda function)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/upload` | Accepts a `multipart/form-data` CSV upload, runs the cleaning pipeline, upserts rows into Supabase, returns a JSON summary |
| `GET` | `/dashboard` | Returns aggregated SLA stats (availability, avg/p95 latency, per-service breakdown). Supports `?from_date=` and `?to_date=` filters |
| `GET` | `/checks` | Returns paginated raw check records. Supports `?service_id=`, `?from_date=`, `?to_date=`, `?limit=` (max 200), `?offset=` |
| `OPTIONS` | `*` | CORS preflight — returns 200 immediately with CORS headers so browsers allow cross-origin requests from the Vercel frontend |

Data flow: browser → multipart/form-data `POST /upload` → API Gateway → Lambda (`app/handler.py`) → cleaning pipeline (`app/cleaning.py`) → Supabase upsert (`app/db.py`) → JSON summary back to the frontend.

Dashboard/logs flow: Next.js → `GET /dashboard` or `GET /checks` → API Gateway → Lambda → Supabase query → JSON response.

*(Chose direct multipart upload over an S3-mediated upload — file sizes here are small (~1MB max), so the extra S3 bucket / presigned URL / event trigger plumbing wasn't worth it for no real benefit at this scale.)*

---

## 2. Data findings

Discovered by inspecting all 5 provided CSVs directly (not told in advance):

- **Mixed timestamp formats**: ISO 8601 UTC (majority), Unix epoch (seconds, as a string), and ISO 8601 with a `+05:30` offset. All normalized to UTC — the epoch and offset cases are silent corruption risks if not converted, not just relabeled.
- **Mixed latency units**: `latency_unit` column is `ms` or `s`. Normalized everything to milliseconds.
- **Missing latency**: ~1–1.5% of rows have an empty `latency` field.
- **Negative latency**: at least one row per file with a physically invalid negative value (e.g. `-223`).
- **Invalid status code (`999`)**: exactly one per file. Falls outside every documented incident window and has a plausible latency value — pointing to an agent-side reporting glitch, not a real failure.
- **Duplicate check records**: same `(service_id, timestamp, agent)` key appears more than once in every file. Mostly exact duplicate rows; a small number have conflicting values (e.g. one copy has a real latency, the other has it missing).

---

## 3. Assumptions

- **Uniqueness key**: `(service_id, timestamp, agent)` is assumed to uniquely identify one check. Used both for de-duplication and as the DB's upsert conflict key (makes re-uploading a file idempotent rather than creating duplicate rows).
- **Conflicting duplicates**: when two rows share a key but differ, the more *complete* row (fewer missing/invalid fields) is kept, not simply "first row wins" — avoids discarding the only good copy of a value.
- **`999` status handling**: excluded from the uptime/SLA calculation (neither counted as success nor failure, since we can't confirm what actually happened), but kept visible in the logs view, flagged.
- **SLA/availability definition**: success = HTTP 2xx (200–299) only. Rows with invalid status codes (e.g. `999`) are excluded from the denominator entirely. 4xx/5xx count as failures. SLA threshold is 99.9% — the `sla_met` field in the dashboard response reflects this per service and globally.
- **Missing/negative latency**: does *not* affect SLA/uptime inclusion (status_code alone determines success/failure) — only excluded from latency-based stats (avg/p95).
- **Epoch timestamps**: assumed to represent UTC seconds (the standard meaning of Unix epoch), since no timezone is attached to disambiguate.
- **Stats shown on dashboard**: availability %, global and per-service; avg latency ms; p95 latency ms; `sla_met` boolean; total / success / invalid check counts. Chosen to reflect what an on-call engineer or billing team would actually need — worst-performing services appear first.

---

## 4. Live URL & running locally

**Live URL:** https://earthre-case-study-frontend.vercel.app/upload

**Running locally:**

```bash
git clone <backend-repo-url>
cd sla-dashboard-backend
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements-dev.txt

cp .env.example .env            # then fill in real SUPABASE_URL / SUPABASE_KEY
```

Run the automated tests (no network/Supabase needed — these only test the pure cleaning logic):
```bash
pytest tests/test_cleaning.py -v
```

Test the full pipeline end-to-end against a real Supabase project (requires `.env` filled in, and `db/schema.sql` already run in Supabase):
```bash
python3 -m app.handler
```
This runs `handler.py`'s own `__main__` block, which fires a synthetic `POST /upload`, `GET /dashboard`, and `GET /checks` against a local event — the same code path Lambda uses, run locally. Row counts printed should match what lands in Supabase's Table Editor.

**Redeploying to Lambda** (if the live URL has gone stale):

```bash
# Linux / macOS
pip install -r requirements.txt -t package/
cp -r app package/
cd package && zip -r ../function.zip . && cd ..

# Windows (PowerShell)
pip install -r requirements.txt -t package/
Copy-Item -Recurse app package/
Compress-Archive -Path package/* -DestinationPath function.zip -Force
```

Upload `function.zip` via the Lambda console (Code → Upload from .zip file). Confirm:
- Handler is set to `app.handler.handler`
- `SUPABASE_URL` and `SUPABASE_KEY` environment variables are set (Configuration → Environment variables)
- API Gateway has `POST /upload`, `GET /dashboard`, `GET /checks`, and `OPTIONS` routes all pointing to this Lambda with **Lambda Proxy integration** enabled
- API Gateway is **deployed** after any route change (easy to forget)

---

## 5. What I'd do differently with more time

- **Aggregate stats in the DB, not in Python** — `get_dashboard_data()` currently does `SELECT *` and aggregates in memory. For larger datasets this would be slow; a Postgres view or RPC function would push the aggregation to the DB where it belongs.
- **Streaming / chunked CSV processing** — currently the entire CSV is read into a pandas DataFrame in memory. For very large files a chunked reader would keep Lambda memory usage flat.
- **Structured error logging** — errors are returned as JSON strings to the frontend but not logged anywhere persistent. CloudWatch structured logs + an alert on Lambda errors would be the first thing to add.
- **Input validation before cleaning** — currently an uploaded file that is missing expected columns (e.g. no `service_id` column) will produce a confusing pandas KeyError rather than a clean 400 with a helpful message.
- **File size limit** — no guard against someone uploading a 500MB file. API Gateway has a 10MB body limit, but making that explicit in the upload handler with a clear error is better UX.
- **Rate limiting** — the upload endpoint has no throttling; API Gateway usage plans would be the lightweight fix.