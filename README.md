# SLA Monitoring Dashboard

## 1. Architecture

| Piece | Choice | Why |
|---|---|---|
| Upload UI + Dashboard | Next.js, deployed on Vercel | Single app hosts both the upload screen and the dashboard; Vercel's free tier gives an instant, stable live URL with zero config |
| Stateless cloud function | Plain Python Lambda handler (no FastAPI) on AWS Lambda | Single endpoint, so a framework's routing/middleware would be pure overhead; a plain handler is smaller to deploy, has a faster cold start, and is easier to explain line-by-line than a framework with an ASGI adapter layer |
| Persistence | Supabase (Postgres) | Free tier, no card required; a relational schema fits the query pattern (filter by date range, aggregate by service) much more naturally than a NoSQL store would |
| Repos | Two separate repos (frontend / backend) | Vercel deploys per-repo; keeping them separate avoids backend commits triggering unnecessary frontend redeploys and keeps commit history clean per service |

CSV upload path: browser → multipart/form-data POST to API Gateway → Lambda (`app/handler.py`) → cleaning pipeline (`app/cleaning.py`) → Supabase upsert (`app/db.py`) → JSON summary back to the frontend.

*(Chose direct multipart upload over an S3-mediated upload — file sizes here are small (~1MB max), so the extra S3 bucket / presigned URL / event trigger plumbing wasn't worth it for no real benefit at this scale.)*

## 2. Data findings

Discovered by inspecting all 5 provided CSVs directly (not told in advance):

- **Mixed timestamp formats**: ISO 8601 UTC (majority), Unix epoch (seconds, as a string), and ISO 8601 with a `+05:30` offset. All normalized to UTC — the epoch and offset cases are silent corruption risks if not converted, not just relabeled.
- **Mixed latency units**: `latency_unit` column is `ms` or `s`. Normalized everything to milliseconds.
- **Missing latency**: ~1–1.5% of rows have an empty `latency` field.
- **Negative latency**: at least one row per file with a physically invalid negative value (e.g. `-223`).
- **Invalid status code (`999`)**: exactly one per file. Falls outside every documented incident window and has a plausible latency value — pointing to an agent-side reporting glitch, not a real failure.
- **Duplicate check records**: same `(service_id, timestamp, agent)` key appears more than once in every file. Mostly exact duplicate rows; a small number have conflicting values (e.g. one copy has a real latency, the other has it missing).

## 3. Assumptions

- **Uniqueness key**: `(service_id, timestamp, agent)` is assumed to uniquely identify one check. Used both for de-duplication and as the DB's upsert conflict key (makes re-uploading a file idempotent rather than creating duplicate rows).
- **Conflicting duplicates**: when two rows share a key but differ, the more *complete* row (fewer missing/invalid fields) is kept, not simply "first row wins" — avoids discarding the only good copy of a value.
- **`999` status handling**: excluded from the uptime/SLA calculation (neither counted as success nor failure, since we can't confirm what actually happened), but kept visible in the logs view, flagged.
- **Missing/negative latency**: does *not* affect SLA/uptime inclusion (status_code alone determines success/failure) — only excluded from latency-based stats (avg/p95).
- **Epoch timestamps**: assumed to represent UTC seconds (the standard meaning of Unix epoch), since no timezone is attached to disambiguate.

## 4. Live URL & running locally

**Live URL:** *(fill in once your API Gateway URL is confirmed working)*

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
This runs `handler.py`'s own `__main__` block, which builds a synthetic API Gateway event from one of the sample CSVs and calls `handler()` exactly as Lambda would — the same code path, run locally instead of deployed. Row counts printed at the end should match what lands in Supabase's Table Editor.

> Note: the sample CSV path in `handler.py`'s `__main__` block was set during development against local test data — update it to point at wherever you keep the provided CSVs in your own checkout before running this.

**Redeploying to Lambda** (if the live URL has gone stale — see the constraints note in the assignment about free-tier uptime):
```bash
pip install -r requirements.txt -t package/
cp -r app package/
cd package && zip -r ../function.zip . && cd ..
```
Upload `function.zip` via the Lambda console (Code → Upload from .zip file), confirm the handler is still set to `app.handler.handler`, and confirm `SUPABASE_URL`/`SUPABASE_KEY` environment variables are still set.

## 5. What I'd do differently with more time

*(To be filled in at the end.)*