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
