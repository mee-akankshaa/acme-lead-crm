#!/usr/bin/env python3
"""Auto-create follow-up tasks in the CRM - staged cadence version.

Runs from cron (e.g. every 15 minutes) against the CRM REST API. Deliberately built on the REST
API instead of a server-side script, so nothing is installed inside the CRM.

Every lead in a chaseable status gets a sequence of check-ins, not one. The stage is simply how
many tasks that lead has already had, so the state lives in the CRM itself - nothing to keep in
sync, and it survives this script being reinstalled.

A DAILY_CAP stops a large backlog arriving all at once; STATUS_RANK decides which leads are chased
first when the cap bites (warm and un-called leads first, newest first inside each status).

Idempotent: a lead with an open task is skipped, and a lead that already got a task today is skipped.

Environment: CRM_URL, CRM_TOKEN (see .env.example). Needs a real Frappe-style CRM; the embedded demo
store only emulates the endpoints used by app.py.

  followup.py             normal run
  followup.py --dry-run   report only, create nothing
  followup.py --plan      show the backlog and how many days it will take
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
CRM = os.environ.get("CRM_URL", "http://localhost:8080").rstrip("/")
AUTH = os.environ.get("CRM_TOKEN", "")          # "token <key>:<secret>"
DRY = "--dry-run" in sys.argv
PLAN = "--plan" in sys.argv

# ---- the business rules. Change these, nothing else. ----------------------
# status -> (gaps in days before each successive check-in, priority)
# len(gaps) is the maximum number of attempts; after that the lead is left alone.
CADENCE = {
    "Interested":    ([2, 2, 2],   "High",   "Follow up with {name} — interested"),
    "Follow Up":     ([2, 2, 2],   "Medium", "Follow up with {name}"),
    "Contacted":     ([3, 7, 14],  "Medium", "Check in with {name}"),
    "Not Available": ([1, 1, 1],   "Low",    "Try {name} again - not reached earlier"),
    "New":           ([1, 1, 1],   "Medium", "First call pending — {name}"),
}
DAILY_CAP = 20        # new tasks per day
DUE_HOUR = 11         # tasks fall due at 11:00

# Which leads get chased FIRST when the cap bites. Lower number = sooner.
# The cap means most of the backlog waits, so this ordering decides what the rep actually
# works on - it matters more than the cadence itself. Warm and un-called leads go first;
# leads that already said "not available" wait, because they are the least likely to convert.
STATUS_RANK = {"Interested": 0, "New": 1, "Follow Up": 2, "Contacted": 3, "Not Available": 4}
# --------------------------------------------------------------------------

CLOSED = ("Done", "Canceled")


def _neg(s):
    """Sort key that reverses a date string, so newest comes first while the
    status rank still sorts ascending."""
    return tuple(-ord(c) for c in s)


def log(m):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {m}"
    print(line)
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "followup.log"), "a") as f:
        f.write(line + "\n")


def api(method, path, params=None, body=None):
    url = CRM + urllib.parse.quote(path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": AUTH,
                                          "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        msg = body
        try:
            j = json.loads(body)
            msgs = json.loads(j.get("_server_messages") or "[]")
            msg = " / ".join(json.loads(m).get("message", str(m)) for m in msgs) or j.get("exception") or body
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {str(msg)[:250]}") from None


def created_today():
    """How many tasks this script already made today — the cap is per day, not per run."""
    midnight = datetime.datetime.combine(datetime.date.today(),
                                         datetime.time.min).strftime("%Y-%m-%d %H:%M:%S")
    rows = api("GET", "/api/resource/CRM Task",
               {"filters": json.dumps([["creation", ">=", midnight],
                                       ["reference_doctype", "=", "CRM Lead"]]),
                "fields": json.dumps(["name"]), "limit_page_length": 0}).get("data") or []
    return len(rows)


def main():
    # 1. every lead in a chaseable status
    leads = {}
    for status in CADENCE:
        res = api("GET", "/api/resource/CRM Lead",
                  {"filters": json.dumps([["status", "=", status]]),
                   "fields": json.dumps(["name", "lead_name", "lead_owner",
                                         "mobile_no", "status", "creation"]),
                   "limit_page_length": 0})
        for row in res.get("data") or []:
            leads[row["name"]] = row
    if not leads:
        log("nothing to chase")
        return

    # 2. task history — this is what tells us the stage each lead is at.
    # Fetched WITHOUT an "in" filter on purpose: passing ~270 lead names made the
    # query string long enough for nginx to return 414. Every lead task is pulled
    # once and indexed here instead.
    tasks = api("GET", "/api/resource/CRM Task",
                {"filters": json.dumps([["reference_doctype", "=", "CRM Lead"]]),
                 "fields": json.dumps(["name", "reference_docname", "status", "creation"]),
                 "limit_page_length": 0}).get("data") or []
    by_lead = {}
    for t in tasks:
        by_lead.setdefault(t["reference_docname"], []).append(t)

    today = datetime.date.today().isoformat()
    due_now, open_tasks, maxed, done_today = [], 0, 0, 0

    for lead, row in leads.items():
        gaps, priority, title = CADENCE[row["status"]]
        mine = by_lead.get(lead, [])
        if any(t["status"] not in CLOSED for t in mine):
            open_tasks += 1
            continue                                  # already being chased
        if any(str(t["creation"])[:10] == today for t in mine):
            done_today += 1
            continue                                  # one per lead per day, max
        attempts = len(mine)
        if attempts >= len(gaps):
            maxed += 1
            continue                                  # cadence finished, leave them alone
        due_now.append((lead, row, gaps[attempts], priority, title, attempts + 1))

    # Warmest status first, and newest lead first inside each status. Chasing the
    # oldest rows first (the obvious choice) turned out to be backwards: it put March's
    # cold leads ahead of a lead that came in today, which with a daily cap means the
    # fresh ones wait weeks.
    due_now.sort(key=lambda x: (STATUS_RANK.get(x[1]["status"], 9),
                                _neg(x[1].get("creation") or "")))

    already = created_today()
    room = max(0, DAILY_CAP - already)

    if PLAN:
        log(f"PLAN: {len(due_now)} lead(s) chaseable now | open tasks {open_tasks} | "
            f"cadence finished {maxed} | created today {already}/{DAILY_CAP}")
        if due_now:
            days = -(-len(due_now) // DAILY_CAP)
            log(f"      at {DAILY_CAP}/day the backlog clears in ~{days} day(s)")
        for lead, row, gap, prio, title, n in due_now[:10]:
            log(f"      {lead} {row['status']:<14} attempt {n}, due +{gap}d  "
                f"{(row.get('lead_name') or '')[:30]}")
        return

    created, failed, errors = 0, 0, []
    for lead, row, gap, priority, title, n in due_now[:room]:
        due = (datetime.datetime.now() + datetime.timedelta(days=gap)).replace(
            hour=DUE_HOUR, minute=0, second=0, microsecond=0)
        name = (row.get("lead_name") or "lead").strip()[:60]
        doc = {"doctype": "CRM Task",
               "title": title.format(name=name)[:140],
               "status": "Todo", "priority": priority,
               "start_date": datetime.date.today().isoformat(),
               "due_date": due.strftime("%Y-%m-%d %H:%M:%S"),
               "assigned_to": row.get("lead_owner") or "Administrator",
               "reference_doctype": "CRM Lead", "reference_docname": lead,
               "description": (f"Auto-created — lead is in status <b>{row.get('status')}</b>, "
                               f"attempt {n} of {len(CADENCE[row['status']][0])}. "
                               f"Phone: {row.get('mobile_no') or '-'}")}
        if DRY:
            log(f"  DRY would create '{doc['title']}' due {doc['due_date']} "
                f"(attempt {n}) -> {doc['assigned_to']}")
            created += 1
            continue
        try:
            api("POST", "/api/resource/CRM Task", body=doc)
            created += 1
        except Exception as e:
            failed += 1
            errors.append(f"{lead}: {str(e)[:140]}")

    held = max(0, len(due_now) - room)
    log(f"{'DRY ' if DRY else ''}chaseable {len(due_now)} | open {open_tasks} | "
        f"cadence-finished {maxed} | created {created} | failed {failed} | "
        f"held for tomorrow {held} (cap {DAILY_CAP}/day, {already} already today)")
    for e in errors[:5]:
        log(f"  ERROR {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
