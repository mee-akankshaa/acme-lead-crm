# Acme Leads

A lightweight lead-management tool for a small sales team. The idea was simple: a salesperson opens it in the morning and sees exactly who to call today, and the system takes care of when to follow up next.

It covers a lead list with filters, a "Today's Calls" queue with automatic follow-up timing, call logging, purchases and a payment ledger, sales reports with XLSX and PDF export, a marketing-channel dashboard, and admin tools for team overview, reassignment and assignment rules.

All data here is invented. It's a demo rebuild of a tool I originally worked on, redone so the code can be shown publicly.

## Choices I'm happy with

- **No dependencies.** The backend is plain Python 3 (`http.server`), and I wrote the XLSX and PDF writers by hand. Nothing to `pip install`.
- **Money is handled carefully.** All payment maths lives in `payments.py` using `Decimal`, with unit tests, so totals and balances never drift through float rounding.
- **Runs standalone.** The real app talks to a Frappe-style CRM over REST. For this repo, `demo_store.py` emulates those calls on SQLite, so you can run the whole thing with no outside services.
- **Server-side checks.** Roles are enforced on the server (reps can't see other reps' leads or admin pages), lead status is validated against a known list, login attempts are throttled, and the session secret is random per process unless you set one.
- **Plain front end.** A single-page vanilla JS UI, assembled from `base.html` and `parts.html` by `build.py`.

## Run it

```bash
cp .env.example .env     # optional, defaults run the demo
python3 app.py           # http://localhost:8090
```

The first start creates `data/demo.db` and fills it with invented leads, tasks and purchases. Reset it with `python3 seed_demo.py --reset`.

Demo logins share one password, `demo1234`, and exist only for this demo:

- `admin@example.com` (admin)
- `alice@example.com`, `bob@example.com`, `carol@example.com` (sales reps)

Set `SESSION_SECRET` for stable sessions, and `COOKIE_SECURE=1` if you serve it over HTTPS. All amounts are in USD.

## Tests

```bash
python3 -m unittest tests_payments
```

## Connecting a real CRM

Set `DEMO_MODE=0`, `CRM_URL` and `CRM_TOKEN` in `.env`. The follow-up job in `jobs/followup.py` and the clean-up script in `tools/fix_names.py` use the same variables. If you set `SOURCE_WEBHOOK_URL`, status changes are mirrored to that endpoint. Scheduled reassignments are queued by the app but need an external scheduler to apply them, which isn't included here.

## Layout

```
app.py                      HTTP server and API
demo_store.py, seed_demo.py demo backend and invented data
payments.py, exporters.py   money maths, XLSX/PDF output
base.html, parts.html       UI source, built into index.html by build.py
mockups/                    early static design prototypes
jobs/, tools/               scheduled and one-off scripts
```

## How it was built

I built this with AI-assisted development (Claude Code), then reviewed, tested and hardened it myself.
