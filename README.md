# Acme Leads

A small lead-management web app for a sales team: lead list with filters, "Today's Calls" queue with
automatic follow-up cadence, call logging, purchases and payment ledger, sales reports (XLSX/PDF export),
marketing-channel dashboard, and admin tools (team overview, reassignment, assignment rules).

## Stack
- Python 3 standard library only (no pip dependencies): `http.server` JSON API + hand-written XLSX/PDF writers
- Single-page vanilla JS front-end (`parts.html` + `base.html` assembled by `build.py` into `index.html`)
- Backend is a Frappe-style CRM REST API. In demo mode an embedded SQLite emulation (`demo_store.py`) replaces it.
- All money maths lives in `payments.py` (Decimal, unit-tested in `tests_payments.py`)

## Run in demo mode (no external services)
```
cp .env.example .env        # optional; defaults already run the demo
python3 build.py            # optional; app.py builds index.html on first request
python3 app.py              # http://localhost:8090
```
The first start creates `data/demo.db` and seeds it with invented data (`seed_demo.py`; re-seed with `python3 seed_demo.py --reset`).

Demo logins (DEMO ONLY: every seeded user shares the published password `demo1234`; never use it outside a local demo):
`admin@example.com` (admin), `alice@example.com`, `bob@example.com`, `carol@example.com` (sales reps).
Set `SESSION_SECRET` in `.env` for stable sessions (otherwise a random secret is generated at each start) and `COOKIE_SECURE=1` when serving over HTTPS.
All amounts are in USD.

Tests: `python3 -m unittest tests_payments`

## Real CRM mode
Set `DEMO_MODE=0`, `CRM_URL`, `CRM_TOKEN` in `.env`. `jobs/followup.py` (a scheduled job creating follow-up tasks) and `tools/fix_names.py`
(one-off data clean-up) use the same variables. Optional `SOURCE_WEBHOOK_URL` mirrors status changes to an upstream system.

## Layout
`app.py` server/API - `demo_store.py` + `seed_demo.py` demo backend - `payments.py`, `exporters.py` - `build.py`, `base.html`, `parts.html` UI - `mockups/` static design prototypes - `jobs/`, `tools/` ops scripts.

Scheduled reassignments are queued by the app but must be applied by an external scheduler (not included).
All data in this repository is fictional.
