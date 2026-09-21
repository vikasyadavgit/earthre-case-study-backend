# Take-Home Assignment — SLA Monitoring Dashboard

**Role:** Full Stack Developer
**Expected effort:** 6–8 focused hours over the weekend.
**Submission:** A GitHub repo link, with the app **deployed and reachable via a live URL** (see Hosting below).

---

## Background

Cloud providers offer SLAs: *"If a service's monthly availability drops below 99.9%, the customer gets a billing credit."* The credit is computed automatically from monitoring data — nobody manually reviews it, the data itself decides the outcome.

That means the pipeline that turns raw logs into numbers has to be trustworthy, and someone (an engineer, a support team) needs a place to actually look at what happened. That's what you're building.

## What you're given

A CSV of health-check logs for 5 services, one check every 15 minutes per service, collected by monitoring agents, spanning **multiple days** (the exact range varies — check the data itself, don't assume). Each row has a timestamp, HTTP status code, response latency, and which agent/region reported it.

**The data is messy**, the way real multi-agent, multi-day logs are messy. Finding and handling its problems is part of the assignment — we won't tell you what's wrong with it, and we won't tell you how many days it covers either.

## The flow we want to see

This is the part we care about most — not just "does it compute the right number," but **how data gets from a file into a dashboard**:

1. **Upload UI** — a screen where a user uploads the CSV.
2. **Stateless processing** — the upload is handed off to a real, deployed stateless serverless function on a cloud provider (AWS Lambda, GCP Cloud Function, Azure Function, Cloudflare Worker, or any equivalent). This function parses, validates, and cleans the data. It must actually run in the cloud, not locally or in a container standing in for one — this is a required part of the architecture, not a nice-to-have.
3. **Persistence** — cleaned data is saved to a database of your choice (Postgres, DynamoDB, Firestore, SQLite — your call), from which it can be queried later. Don't just hold it in memory; it has to be re-queryable after the upload finishes.
4. **A single-screen dashboard UI** with two sections on one page:
   - **Top: a stats section that can be collapsed/expanded.** You decide what stats matter for this use case (this is a real design decision, not a checklist — think about what someone on-call or in billing would actually want to see).
   - **Below: a logs view**, filterable by a single date *or* a date range, showing the underlying check records.

## Constraints & rules

- **Use only free-tier / no-cost resources.** Nothing that requires a paid plan or credit card commitment beyond a provider's free tier.
- **You must deploy and host this for real** — the upload UI, the cloud function, the database, and the dashboard all need to be reachable at live URLs, not run on your own machine. Share the working link alongside your GitHub repo. If it can't stay up indefinitely on a free tier, tell us in the README when it was last verified live and how to redeploy it on demand — but it needs to actually be live at review time.
- **Do NOT build:** authentication/user accounts, multi-tenant support, CI pipelines. Explicitly out of scope.
- **AI tools (Copilot, Claude, ChatGPT, etc.) are allowed.** We use them too. But you must be able to explain and defend every line in a follow-up discussion, and your README's assumptions/decisions must reflect *your* thinking. Working code that can't be explained is treated as a failure.
- Commit as you go. We read commit history.

## Your README must include

1. Architecture: what runs where (upload UI → function → DB → dashboard), and why you chose each piece
2. **Data findings:** every data-quality issue you discovered and how you handled it
3. **Assumptions:** anywhere the spec was ambiguous, the choice you made and why (this includes your choice of stats to show)
4. Live URL, and how to run/redeploy locally
5. What you'd do differently with more time

Good luck. Break something interesting.
