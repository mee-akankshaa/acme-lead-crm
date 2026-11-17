#!/usr/bin/env python3
"""Acme Leads - a small lead-management app: a thin web front-end over a Frappe-style CRM REST API.

Pure Python stdlib (no pip deps). Serves index.html plus a small JSON API that reads/writes
Lead / Task / Comment documents through the CRM REST API. Secrets stay server-side.

Two backends, selected by DEMO_MODE:
  DEMO_MODE=1 (default)  an embedded SQLite emulation of the CRM API (demo_store.py), seeded with
                         invented data by seed_demo.py. Zero external services.
  DEMO_MODE=0            talks to a real Frappe CRM at CRM_URL using CRM_TOKEN.

Run:  python3 app.py        (configuration comes from environment variables / a .env file, see .env.example)
"""
import base64, datetime, hashlib, hmac, json, os, re, threading, time, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import exporters  # stdlib-only .xlsx / .pdf writers (same folder)
import payments   # payment ledger: ALL money maths lives there (unit-tested), never in the browser
PURCHASE_LOCK = threading.Lock()   # purchase JSON is read-modify-write: two saves at once must not lose a payment

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env reader (KEY=VALUE per line); real environment variables win."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(HERE, ".env"))
DEMO = os.environ.get("DEMO_MODE", "1") != "0"
CRM = os.environ.get("CRM_URL", "http://localhost:8080").rstrip("/")   # base URL of the CRM REST API
AUTH = os.environ.get("CRM_TOKEN", "")                                  # "token <key>:<secret>"
_sec = os.environ.get("SESSION_SECRET", "")
if not _sec or _sec.startswith("change-me"):
    # no usable secret configured: use a random per-process one (sessions reset on restart)
    # rather than a guessable default that would let anyone forge a session cookie
    _sec = os.urandom(32).hex()
SECRET = _sec.encode()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"   # set to 1 when served over HTTPS
PORT = int(os.environ.get("PORT", "8090"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
COOKIE_TTL = 7 * 24 * 3600             # 7 days
MAX_BODY = 1024 * 1024                 # request body cap (1 MB)
KIOSK_KEY = os.environ.get("KIOSK_KEY", "")   # optional desktop-shortcut auto-login key
KIOSK_USER = os.environ.get("KIOSK_USER", "admin@example.com")
os.makedirs(DATA_DIR, exist_ok=True)

if DEMO:
    import demo_store
    demo_store.init(os.path.join(DATA_DIR, "demo.db"))

# Optional outbound webhook: when a lead's status changes, POST it so an upstream
# system (e.g. the website the lead came from) can mirror it. Disabled when unset.
SOURCE_WEBHOOK_URL = os.environ.get("SOURCE_WEBHOOK_URL", "")
SOURCE_WEBHOOK_TOKEN = os.environ.get("SOURCE_WEBHOOK_TOKEN", "")

# ---- admin / team management ----
# Anyone holding this role sees the whole team (Admin section, no lead_owner
# filter); everyone else only sees their own leads. Deliberately NOT
# "Sales Manager", so a plain sales manager does not silently become an admin;
# "System Manager" is the unambiguous admin marker.
ADMIN_ROLES = {"System Manager"}
# Service/API accounts that must never show up as a "sales rep" in the Team
# Overview or be eligible for round-robin lead assignment.
SYSTEM_ACCOUNTS = {"Administrator", "service-bot@example.com"}
RULES_PATH = os.path.join(DATA_DIR, "assignment_rules.json")
# Future-dated reassignments are only QUEUED here (scheduled_reassignments.json); applying them
# needs an external scheduler (not included in this showcase). reassign_log.json is the audit
# trail of reassignments shown on the Admin page.
SCHEDULE_PATH = os.path.join(DATA_DIR, "scheduled_reassignments.json")
REASSIGN_LOG_PATH = os.path.join(DATA_DIR, "reassign_log.json")


def get_user_roles(email):
    try:
        d = frappe("GET", docpath("User", email)).get("data", {})
        return [r.get("role") for r in d.get("roles", [])]
    except Exception:
        return []


def is_admin(email):
    return bool(ADMIN_ROLES & set(get_user_roles(email)))


def get_sales_users():
    """Every enabled System User holding Sales Manager/Sales User, minus
    service accounts — used for the Team Overview and the reassign/rules
    dropdowns. N+1 role lookups are fine at this team size (single digits)."""
    users = frappe("GET", "/api/resource/User", params={
        "fields": json.dumps(["name", "full_name"]),
        "filters": json.dumps([["enabled", "=", 1], ["user_type", "=", "System User"]]),
        "limit_page_length": 0,
    }).get("data", [])
    out = []
    for u in users:
        email = u["name"]
        if email in SYSTEM_ACCOUNTS:
            continue
        roles = get_user_roles(email)
        if "Sales Manager" in roles or "Sales User" in roles:
            out.append({"email": email, "name": u.get("full_name") or email.split("@")[0].title(),
                        "admin": bool(ADMIN_ROLES & set(roles))})
    return out


def load_rules():
    try:
        r = json.load(open(RULES_PATH))
    except Exception:
        r = {}
    r.setdefault("source_rules", {})
    r.setdefault("active_users", [])
    r.setdefault("daily_targets", {})
    return r


def save_rules(r):
    tmp = RULES_PATH + ".tmp"
    json.dump(r, open(tmp, "w"), indent=2)
    os.replace(tmp, RULES_PATH)


def load_schedule():
    try:
        s = json.load(open(SCHEDULE_PATH))
    except Exception:
        s = {}
    s.setdefault("items", [])
    return s


def save_schedule(s):
    tmp = SCHEDULE_PATH + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, SCHEDULE_PATH)


def load_reassign_log():
    try:
        l = json.load(open(REASSIGN_LOG_PATH))
    except Exception:
        l = {}
    l.setdefault("items", [])
    return l


def log_reassign(by, from_email, to_email, count, kind, filter_desc):
    """Append one entry to the reassignment audit trail (capped at 200 - a history
    view, not a permanent ledger; the Lead docs are the source of truth for ownership)."""
    l = load_reassign_log()
    l["items"].insert(0, {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "by": by, "from_email": from_email, "to_email": to_email,
        "count": count, "kind": kind, "filter_desc": filter_desc,
    })
    l["items"] = l["items"][:200]
    tmp = REASSIGN_LOG_PATH + ".tmp"
    json.dump(l, open(tmp, "w"), indent=2)
    os.replace(tmp, REASSIGN_LOG_PATH)

# statuses that mean a task/lead is still open
CLOSED_TASK = {"Done", "Cancelled", "Canceled"}
# Frappe rejects a "Lost"-type status without a reason ("Please specify a reason
# for losing the lead"). lost_reason "Other" additionally requires lost_notes.
LOST_REASON = {"Not Interested": "Poor Fit", "Junk": "Other", "Unqualified": "Poor Fit"}
LOST_NOTES = {"Junk": "Wrong number / invalid lead"}
VALID_STATUSES = ["New", "Contacted", "Interested", "Follow Up", "Not Available",
                  "Not Interested", "Junk", "Converted", "Qualified", "Nurture", "Unqualified"]
# priority for Today's Calls — interested first
STATUS_RANK = {"Interested": 0, "New": 1, "Follow Up": 2, "Contacted": 3,
               "Not Available": 4, "Nurture": 5}
# Same cadence as jobs/followup.py — keep the two in step.
# status -> (gaps in days before each successive check-in, priority, task title)
CADENCE = {
    "Interested":    ([2, 2, 2],  "High",   "Follow up with {name} - interested"),
    "Follow Up":     ([2, 2, 2],  "Medium", "Follow up with {name}"),
    "Contacted":     ([3, 7, 14], "Medium", "Check in with {name}"),
    "Not Available": ([1, 1, 1],  "Low",    "Try {name} again"),
    "New":           ([1, 1, 1],  "Medium", "First call pending - {name}"),
}
DUE_HOUR = 11

def push_status_to_sources(lead_id, crm_status):
    """Best-effort: tell an upstream system that a lead's status changed (generic webhook).
    No-op unless SOURCE_WEBHOOK_URL is set. Never raises - it must never block the actual CRM
    status update, it is a convenience sync, not a critical path."""
    if not SOURCE_WEBHOOK_URL:
        return
    try:
        d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
        payload = {"lead_id": lead_id, "status": crm_status,
                   "source_history": d.get("custom_source_history") or ""}
        headers = {"Content-Type": "application/json"}
        if SOURCE_WEBHOOK_TOKEN:
            headers["Authorization"] = f"Bearer {SOURCE_WEBHOOK_TOKEN}"
        req = urllib.request.Request(SOURCE_WEBHOOK_URL, data=json.dumps(payload).encode(),
                                     method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[webhook] lead {lead_id} -> {crm_status} failed: {e}")


# UI status buckets -> css class handled client-side; here just the canonical list
LEAD_FIELDS = ["name", "lead_name", "first_name", "status", "mobile_no", "email",
               "source", "custom_brand", "custom_requirement", "custom_billing_city", "custom_referral",
               "custom_product_model", "custom_budget", "lead_owner", "creation", "modified",
               "custom_last_contacted"]


# ---------------- Frappe helpers ----------------
def frappe(method, path, params=None, body=None):
    if DEMO:
        return demo_store.handle(method, path, params, body)
    # doctype names contain spaces ("CRM Task"); encode them, but leave an
    # already-encoded path (%20) alone so we never double-encode.
    if " " in path:
        path = urllib.parse.quote(path, safe="/%")
    url = CRM + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", AUTH)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"frappe {method} {path} -> {e.code}: {e.read()[:300].decode('utf-8','replace')}")


def flist(doctype, fields, filters=None, or_filters=None, order_by=None, limit=0, offset=0):
    p = {"fields": json.dumps(fields), "limit_page_length": limit}
    if offset:
        p["limit_start"] = offset
    if filters:
        p["filters"] = json.dumps(filters)
    if or_filters:
        p["or_filters"] = json.dumps(or_filters)
    if order_by:
        p["order_by"] = order_by
    return frappe("GET", f"/api/resource/{urllib.parse.quote(doctype)}", p).get("data", [])


def docpath(doctype, name):
    """URL for one document. Frappe names are not always strings — CRM Task uses
    integer autoincrement names — so always str() before quoting."""
    return f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(str(name))}"


def fcount(doctype, filters=None):
    p = {"doctype": doctype}
    if filters:
        p["filters"] = json.dumps(filters)
    return frappe("GET", "/api/method/frappe.client.get_count", p).get("message", 0)


# ---------------- lead shaping ----------------
def req_short(lead):
    r = (lead.get("custom_product_model") or "").strip()
    if r:
        return r
    txt = (lead.get("custom_requirement") or "").strip()
    # first non-bracketed meaningful line
    for line in txt.splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            return line[:80]
    return txt[:80] or "—"


def shape(lead):
    nm = (lead.get("lead_name") or lead.get("first_name") or "Unknown").strip()
    status = lead.get("status") or "New"
    return {
        "id": lead["name"],
        "name": nm,
        "phone": lead.get("mobile_no") or "",
        "email": lead.get("email") or "",
        "status": status,
        "source": lead.get("source") or "Other",
        "brand": lead.get("custom_brand") or "Acme",
        "city": lead.get("custom_billing_city") or "",
        "referral": lead.get("custom_referral") or "",
        "req": req_short(lead),
        "owner": lead.get("lead_owner") or "",
        "created": (lead.get("creation") or "")[:19],
        "last_contacted": (lead.get("custom_last_contacted") or "")[:10],
        # not in CADENCE => no follow-up will ever auto-schedule again for this
        # status (Won/Junk/Not Interested/etc.) — the tick should show a
        # permanent "closed" state here, not the daily called-today reset,
        # since there's no more work to track.
        "dead_end": status not in CADENCE,
    }


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# GET requests embed `filters` in the URL query string, and an `["x", "in", ids]` filter with
# many ids can exceed a reverse proxy's request-line limit, so id lookups are batched.
ENRICH_CHUNK = 80


def enrich_leads(leads):
    """Attach next_followup (earliest open task's due date), note_count and notes
    (last 5, newest first) to each shaped lead in place. `last_contacted` is NOT
    computed here — shape() already sets it straight from the lead's own
    custom_last_contacted field (see api_set_status/api_called, which write that
    field directly on every save). It used to be derived from a follow-up Task's
    Done timestamp, which silently broke for any lead with no open task to close
    (cadence exhausted, or no task ever scheduled) — a save could genuinely
    happen today and the tick would still show stale.

    Batches the CRM Task / Comment lookups in chunks of ENRICH_CHUNK ids —
    see the ENRICH_CHUNK note above for why."""
    ids = [l["id"] for l in leads]
    if not ids:
        return
    tasks = []
    for chunk in _chunks(ids, ENRICH_CHUNK):
        tasks += flist("CRM Task", ["reference_docname", "status", "due_date"],
                       filters=[["reference_doctype", "=", "CRM Lead"], ["reference_docname", "in", chunk],
                                ["status", "not in", list(CLOSED_TASK)]],
                       order_by="due_date asc", limit=0)
    next_due = {}
    for t in tasks:
        rid = t.get("reference_docname")
        if rid and rid not in next_due:
            next_due[rid] = (t.get("due_date") or "")[:10]
    comments = []
    for chunk in _chunks(ids, ENRICH_CHUNK):
        comments += flist("Comment", ["reference_name", "content", "creation"],
                          filters=[["reference_doctype", "=", "CRM Lead"], ["reference_name", "in", chunk],
                                   ["comment_type", "in", ["Comment", "Info"]]],
                          order_by="creation desc", limit=0)
    notes_by_lead = {}
    for c in comments:
        rid = c.get("reference_name")
        notes_by_lead.setdefault(rid, []).append({
            "date": (c.get("creation") or "")[:10], "text": _strip_html(c.get("content") or "")})
    for l in leads:
        l["next_followup"] = next_due.get(l["id"], "")
        notes = notes_by_lead.get(l["id"], [])
        l["note_count"] = len(notes)
        l["notes"] = notes[:5]


# ---------------- API handlers ----------------
def open_tasks_for(lead_id):
    rows = flist("CRM Task", ["name", "status", "due_date", "creation"],
                 filters=[["reference_doctype", "=", "CRM Lead"], ["reference_docname", "=", lead_id]],
                 order_by="creation asc", limit=0)
    return [t for t in rows if t.get("status") not in CLOSED_TASK], rows


def close_open_tasks(lead_id):
    """Mark every open task on this lead Done. Returns how many were closed."""
    opn, _ = open_tasks_for(lead_id)
    for t in opn:
        frappe("PUT", docpath("CRM Task", t["name"]), body={"status": "Done"})
    return len(opn)


def schedule_next(lead_id, lead_name, status, force_date=None):
    """Create the next follow-up task per CADENCE (same rules as followup.py).
    force_date wins. Returns the due date, or None if the cadence is finished /
    the status is a closed one."""
    if force_date:
        due, prio, title = force_date, "Medium", f"Follow up with {lead_name}"
    else:
        rule = CADENCE.get(status)
        if not rule:
            return None                      # Not Interested / Junk / Converted etc.
        gaps, prio, tmpl = rule
        _, alltasks = open_tasks_for(lead_id)
        stage = len(alltasks)                # how many check-ins this lead already had
        if stage >= len(gaps):
            return None                      # cadence finished, leave the lead alone
        due = (datetime.date.today() + datetime.timedelta(days=gaps[stage])).isoformat()
        title = tmpl.format(name=lead_name)
    frappe("POST", "/api/resource/CRM Task",
           body={"doctype": "CRM Task", "title": title[:140],
                 "reference_doctype": "CRM Lead", "reference_docname": lead_id,
                 "status": "Todo", "priority": prio,
                 "due_date": f"{due} {DUE_HOUR:02d}:00:00"})
    return due


def lead_display_name(lead_id):
    d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
    return d.get("lead_name") or d.get("first_name") or lead_id


# Manual-entry sources - for people who reach sales directly (chat/call/in person)
# rather than through a website form: returning clients, referrals, walk-ins.
MANUAL_SOURCES = ["Existing Customer", "Reference", "Cold Calling", "Walk In"]
# Validate emails ourselves first so a bad address gets a clean, readable error
# instead of a raw backend validation failure.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def api_create_lead(body, owner=""):
    """Manual lead entry (the 'New Lead' button) — enters the same CADENCE
    follow-up cycle as any other New lead via schedule_next() so it shows up
    in Today's Calls like every other lead, not just sits there silently.
    Owned by whoever is entering it (a walk-in/referral a specific rep
    personally brought in) — deliberately NOT round-robin'd like website
    leads."""
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not name or not phone:
        return {"ok": False, "error": "Name and phone are both required"}
    email = (body.get("email") or "").strip()
    if email and not EMAIL_RE.match(email):
        return {"ok": False, "error": "Email is not in a valid format — double-check the client's email"}
    brand = body.get("brand") or "Acme"
    source = body.get("source") or "Existing Customer"
    if source not in MANUAL_SOURCES:
        source = "Existing Customer"
    doc = {"first_name": name, "mobile_no": phone, "status": "New",
           "source": source, "custom_brand": brand}
    if owner:
        doc["lead_owner"] = owner
    if email:
        doc["email"] = email
    city = (body.get("city") or "").strip()
    if city:
        doc["custom_billing_city"] = city
    model = (body.get("model") or "").strip()
    if model:
        doc["custom_product_model"] = model
    req = (body.get("requirement") or "").strip()
    if req:
        doc["custom_requirement"] = req
    created = frappe("POST", "/api/resource/CRM Lead", body=doc)["data"]
    lead_id = created["name"]
    schedule_next(lead_id, name, "New")
    return {"ok": True, "id": lead_id}


def build_lead_filters(status="", source="", brand="", from_date="", to_date="", search="", owner=None):
    """Shared by api_leads (paginated fetch) and api_bulk_delete (delete
    everything matching) so the two can never silently drift apart on what
    counts as 'the current filter'. `owner`, when set
    (non-admin session), scopes every query to that sales rep's own leads —
    so two reps can never see/call the same lead."""
    filters = []
    if owner:
        filters.append(["lead_owner", "=", owner])
    if status and status not in ("all", "today"):
        filters.append(["status", "=", status])
    if source and source != "all":
        if source == "Manual":
            # virtual group for the frontend's combined "Manual" source
            # dropdown — not a real Frappe CRM Lead Source value itself
            filters.append(["source", "in", MANUAL_SOURCES])
        else:
            filters.append(["source", "=", source])
    if brand and brand != "all":
        filters.append(["custom_brand", "=", brand])
    if from_date:
        filters.append(["creation", ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append(["creation", "<=", f"{to_date} 23:59:59"])
    or_filters = None
    if search:
        or_filters = [["lead_name", "like", f"%{search}%"], ["mobile_no", "like", f"%{search}%"],
                      ["email", "like", f"%{search}%"]]
    return filters, or_filters


def api_leads(q, owner=None):
    filters, or_filters = build_lead_filters(
        status=q.get("status", [""])[0], source=q.get("source", [""])[0],
        brand=q.get("brand", [""])[0], from_date=q.get("from_date", [""])[0],
        to_date=q.get("to_date", [""])[0], search=q.get("q", [""])[0].strip(), owner=owner)
    limit = int(q.get("limit", ["60"])[0])
    offset = int(q.get("offset", ["0"])[0])
    rows = flist("CRM Lead", LEAD_FIELDS, filters=filters or None, or_filters=or_filters,
                 order_by="modified desc", limit=limit, offset=offset)
    total = fcount("CRM Lead", filters or None) if or_filters is None else None
    leads = [shape(r) for r in rows]
    enrich_leads(leads)
    return {"leads": leads, "total": total,
            "offset": offset, "has_more": len(rows) == limit}


def api_bulk_delete(body, owner=None):
    """Delete every CRM Lead matching the current list filters — backs the
    'All Delete' control next to the date filter. With no filters active
    this deletes literally everything, by design: the
    button works filter-or-no-filter, same as the count next to it. The
    frontend gates this behind a strong type-to-confirm since there's no
    undo. `owner` scopes a
    non-admin's delete-all to their own leads only """
    filters, or_filters = build_lead_filters(
        status=body.get("status", ""), source=body.get("source", ""),
        brand=body.get("brand", ""), from_date=body.get("from_date", ""),
        to_date=body.get("to_date", ""), search=(body.get("q", "") or "").strip(), owner=owner)
    rows = flist("CRM Lead", ["name"], filters=filters or None, or_filters=or_filters, limit=0)
    for r in rows:
        api_delete_lead(r["name"])
    return {"ok": True, "deleted": len(rows)}


def api_today(owner=None):
    today_end = time.strftime("%Y-%m-%d") + " 23:59:59"
    tasks = flist("CRM Task",
                  ["name", "priority", "due_date", "reference_docname", "reference_doctype", "status"],
                  filters=[["reference_doctype", "=", "CRM Lead"], ["due_date", "<=", today_end]],
                  order_by="due_date asc", limit=0)
    seen, ids, due, tid = set(), [], {}, {}
    for t in tasks:
        if t.get("status") in CLOSED_TASK:
            continue
        rid = t.get("reference_docname")
        if rid and rid not in seen:
            seen.add(rid); ids.append(rid)
            due[rid] = (t.get("due_date") or "")[:10]
            tid[rid] = t.get("name")
    leads = []
    if ids:
        lead_filters = [["name", "in", ids]]
        if owner:
            lead_filters.append(["lead_owner", "=", owner])
        rows = flist("CRM Lead", LEAD_FIELDS, filters=lead_filters, limit=0)
        leads = [shape(r) for r in rows]
        for l in leads:
            l["due"] = due.get(l["id"], "")
            l["task"] = tid.get(l["id"], "")
        enrich_leads(leads)
        leads.sort(key=lambda l: (STATUS_RANK.get(l["status"], 9), l.get("due", "")))
    return {"leads": leads, "count": len(leads)}


def api_lead(lead_id):
    d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
    lead = shape(d)
    lead["requirement_full"] = (d.get("custom_requirement") or "").strip()
    try:
        _pl = payments.purchases_of(d.get("custom_purchase_details"))
        lead["purchases"] = [payments.out(x) for x in _pl]
        lead["purchase"] = lead["purchases"][0] if len(_pl) == 1 else None   # old browser tabs only understand one
    except Exception:
        lead["purchases"], lead["purchase"] = [], None
    # timeline: comments + open tasks
    comments = flist("Comment", ["content", "creation", "comment_type", "comment_by"],
                     filters=[["reference_doctype", "=", "CRM Lead"], ["reference_name", "=", lead_id],
                              ["comment_type", "in", ["Comment", "Info"]]],
                     order_by="creation desc", limit=15)
    tasks = flist("CRM Task", ["title", "status", "priority", "due_date", "creation"],
                  filters=[["reference_doctype", "=", "CRM Lead"], ["reference_docname", "=", lead_id]],
                  order_by="creation desc", limit=10)
    tl = []
    for t in tasks:
        tl.append({"kind": "task", "title": t.get("title") or "Follow-up task",
                   "sub": f"{t.get('status','')} · due {(t.get('due_date') or '')[:10]}",
                   "time": (t.get("creation") or "")[:16].replace("T", " ")})
    for c in comments:
        tl.append({"kind": "note", "title": "Name updated" if c.get("comment_type") == "Info" else "Note",
                   "sub": _strip_html(c.get("content") or ""),
                   "time": (c.get("creation") or "")[:16].replace("T", " ")})
    tl.append({"kind": "created", "title": "Lead created",
               "sub": f"From {lead['source']}", "time": d.get("creation", "")[:16].replace("T", " ")})
    tl.sort(key=lambda x: x["time"], reverse=True)
    lead["timeline"] = tl
    return lead


def _strip_html(s):
    return re.sub("<[^>]+>", "", s).strip()[:200]


def api_set_status(lead_id, status, next_date=None):
    """Set the status AND keep the follow-up loop in step: close whatever task is
    open on this lead and schedule the next one per the cadence."""
    if status not in VALID_STATUSES:
        return {"ok": False, "error": "unknown status"}
    # custom_last_contacted drives the "called today" tick in the UI — set it
    # unconditionally here, independent of whether a follow-up task exists to
    # close (the tick must reflect "a save happened today",
    # not "a task happened to be open today").
    body = {"status": status, "custom_last_contacted": datetime.date.today().isoformat()}
    if status in LOST_REASON:
        body["lost_reason"] = LOST_REASON[status]
        if status in LOST_NOTES:
            body["lost_notes"] = LOST_NOTES[status]
    frappe("PUT", docpath("CRM Lead", lead_id), body=body)
    push_status_to_sources(lead_id, status)
    closed = close_open_tasks(lead_id)
    nxt = schedule_next(lead_id, lead_display_name(lead_id), status, next_date)
    return {"ok": True, "status": status, "closed": closed, "next": nxt}


def api_called(lead_id, status, note, next_date, user):
    """One step after a call: log the note, set the outcome status, close the open
    task, and schedule the next call. This is what keeps Today's Calls draining."""
    if status and status not in VALID_STATUSES:
        return {"ok": False, "error": "unknown status"}
    nm = lead_display_name(lead_id)
    if (note or "").strip():
        api_add_note(lead_id, note, user)
    # custom_last_contacted always gets set here — a logged call is a logged
    # call whether or not it also changed status — see api_set_status's note.
    body = {"custom_last_contacted": datetime.date.today().isoformat()}
    if status:
        body["status"] = status
        if status in LOST_REASON:
            body["lost_reason"] = LOST_REASON[status]
            if status in LOST_NOTES:
                body["lost_notes"] = LOST_NOTES[status]
    frappe("PUT", docpath("CRM Lead", lead_id), body=body)
    if status:
        push_status_to_sources(lead_id, status)
    closed = close_open_tasks(lead_id)
    nxt = schedule_next(lead_id, nm, status or "Contacted", next_date)
    return {"ok": True, "status": status, "closed": closed, "next": nxt}


def lead_access(lead_id, user, admin):
    """Sales privacy: an admin may open/edit every lead; a sales rep only the
    leads they OWN. Before this, /api/lead/<id> and every /api/lead/<id>/<action> trusted any
    logged-in user who knew a lead id."""
    if admin:
        return True
    if not user:
        return False
    try:
        d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
    except Exception:
        return False
    return (d.get("lead_owner") or "").strip().lower() == str(user).strip().lower()


def _load_state(lead_id):
    """-> (purchases list, removed list, lead status, lead owner) read fresh from Frappe."""
    d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
    raw = d.get("custom_purchase_details")
    return (payments.purchases_of(raw), payments.removed_of(raw),
            d.get("status") or "", (d.get("lead_owner") or "").strip())


def _save_state(lead_id, purchases, removed, extra=None):
    pbody = {"custom_purchase_details": json.dumps(payments.pack(purchases, removed))}
    pbody.update(extra or {})
    frappe("PUT", docpath("CRM Lead", lead_id), body=pbody)


def _credit_for(user, lead_owner, is_admin):
    """Who a NEW purchase counts for. An admin keying a
    sale in on someone else's lead must not steal the rep's credit, so that case stays with the
    lead's owner."""
    if is_admin and lead_owner:
        return lead_owner
    return user or lead_owner


def api_set_purchase(lead_id, body, user, is_admin=False):
    """Save one purchase of a lead: buying date, product lines (qty x price) and the first
    payment choice (full / advance / nothing yet). The net total and every payment figure are
    computed in payments.py, never trusted from the browser. Later payments go through
    api_payment(). A lead can hold SEVERAL purchases (client buys again months later):
      body.purchase_id  -> edit that purchase
      body.new_purchase -> append a new purchase (repeat sale, counts for `user`)
      neither           -> edit the only purchase, or create the first one
    Marks the lead Converted in the same write when set_status is truthy and it is not Converted
    yet (website push-back / task closing happen only on that first conversion)."""
    with PURCHASE_LOCK:
        purchases, removed, status, owner = _load_state(lead_id)
        try:
            pid = str(body.get("purchase_id") or "").strip()
            if body.get("new_purchase"):
                if not purchases:
                    return {"ok": False, "error": "Record the first purchase before adding a repeat purchase"}
                cur = None
            else:
                cur = payments.pick(purchases, pid)
            purchase = payments.save_purchase(cur or {}, body.get("buying_date"), body.get("items"), body.get("pay"), user, pay_rows=body.get("payments"))
        except payments.PayError as e:
            return {"ok": False, "error": str(e)}
        if cur is None:
            purchase["id"] = payments.new_id(purchases)
            purchase["credit_to"] = _credit_for(user, owner, is_admin)
            purchases.append(purchase)
        else:
            purchase["id"] = cur["id"]
            purchases[purchases.index(cur)] = purchase
        first_conversion = bool(body.get("set_status")) and status != "Converted"
        extra = {}
        if first_conversion:
            extra["status"] = "Converted"
            extra["custom_last_contacted"] = datetime.date.today().isoformat()
        _save_state(lead_id, purchases, removed, extra)
    if first_conversion:
        push_status_to_sources(lead_id, "Converted")
        close_open_tasks(lead_id)
        schedule_next(lead_id, lead_display_name(lead_id), "Converted", None)
    return {"ok": True, "purchase": payments.out(purchase), "purchases": [payments.out(x) for x in purchases]}


def api_payment(lead_id, body, user):
    """Add / edit / delete one payment of one purchase (body.purchase_id; optional while the lead
    has a single purchase). Who may call it (admin, or the lead's own sales rep) is decided by the
    router before this runs."""
    action = str(body.get("action") or "add")
    with PURCHASE_LOCK:
        purchases, removed, _st, _ow = _load_state(lead_id)
        try:
            existing = payments.pick(purchases, str(body.get("purchase_id") or "").strip())
            if not existing:
                return {"ok": False, "error": "This lead has no purchase recorded yet"}
            if action == "add":
                purchase = payments.add_payment(existing, body, user)
            elif action == "edit":
                purchase = payments.edit_payment(existing, str(body.get("payment_id") or ""), body, user)
            elif action == "delete":
                purchase = payments.delete_payment(existing, str(body.get("payment_id") or ""), user)
            else:
                return {"ok": False, "error": "unknown action"}
        except payments.PayError as e:
            return {"ok": False, "error": str(e)}
        purchase["id"] = existing["id"]
        purchases[purchases.index(existing)] = purchase
        _save_state(lead_id, purchases, removed)
    return {"ok": True, "purchase": payments.out(purchase), "purchases": [payments.out(x) for x in purchases]}


def api_delete_purchase(lead_id, body, user, is_admin=False):
    """Remove a wrongly entered REPEAT purchase. Allowed for an admin or the person who recorded it;
    the first/only purchase can't be deleted (a Converted lead must keep its sale). The removed
    purchase is kept in `removed` for the audit trail."""
    with PURCHASE_LOCK:
        purchases, removed, _st, _ow = _load_state(lead_id)
        try:
            cur = payments.pick(purchases, str(body.get("purchase_id") or "").strip()) if body.get("purchase_id") else None
        except payments.PayError as e:
            return {"ok": False, "error": str(e)}
        if not cur:
            return {"ok": False, "error": "Purchase not found"}
        if len(purchases) < 2:
            return {"ok": False, "error": "This is the only purchase of this lead - edit it instead of deleting"}
        if not (is_admin or str(cur.get("recorded_by") or "").lower() == str(user or "").lower()):
            return {"ok": False, "error": "Only an admin or the person who recorded this purchase can delete it"}
        gone = dict(cur)
        gone["deleted_by"], gone["deleted_at"] = user, payments._now()
        purchases = [x for x in purchases if x is not cur]
        removed = (removed + [gone])[-50:]
        _save_state(lead_id, purchases, removed)
    return {"ok": True, "purchases": [payments.out(x) for x in purchases]}


def api_add_note(lead_id, content, user):
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "empty"}
    frappe("POST", "/api/resource/Comment",
           body={"doctype": "Comment", "comment_type": "Comment",
                 "reference_doctype": "CRM Lead", "reference_name": lead_id,
                 "content": content, "comment_email": user, "comment_by": user})
    return {"ok": True}


def api_update_name(lead_id, name, user):
    """Sales team often gets the name wrong at first (enquiry form / call-center
    mishear); once the real name comes out on a call, this lets them correct it
    on the lead so nobody has to re-ask next time. Old name is kept in the
    timeline as an Info comment, not silently overwritten, so the correction
    itself is visible/auditable."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "empty"}
    old = lead_display_name(lead_id)
    if name == old:
        return {"ok": True, "name": name}
    # CRM Lead's lead_name is a derived field Frappe recomputes from
    # first_name+last_name on save — writing lead_name directly gets silently
    # discarded . Put the whole corrected name in
    # first_name and clear last_name, same shape shape()/lead_display_name()
    # already expect ("Someone" was stored as first_name="Someone", last_name="").
    frappe("PUT", docpath("CRM Lead", lead_id), body={"first_name": name, "last_name": ""})
    frappe("POST", "/api/resource/Comment",
           body={"doctype": "Comment", "comment_type": "Info",
                 "reference_doctype": "CRM Lead", "reference_name": lead_id,
                 "content": f"Name updated: “{old}” → “{name}”",
                 "comment_email": user, "comment_by": user})
    return {"ok": True, "name": name}


# Whitelisted lead fields sales can correct after talking to the customer
# ("customer said their number/email/city changed") — key here is what the
# frontend sends, value is (actual Frappe fieldname, human label for the
# audit-comment text). Deliberately small and explicit rather than accepting
# an arbitrary fieldname from the client.
EDITABLE_LEAD_FIELDS = {
    "phone": ("mobile_no", "Phone"),
    "email": ("email", "Email"),
    "city": ("custom_billing_city", "City"),
}


def api_update_field(lead_id, field_key, value, user):
    """Generic correct-a-contact-detail edit for phone/email/city — same audit
    pattern as api_update_name: old value kept as a visible Info comment on
    the timeline, not silently overwritten. Phone can't be cleared blank (it
    drives the Call/Chat buttons); email and city can be, since not every
    lead has both to begin with."""
    if field_key not in EDITABLE_LEAD_FIELDS:
        return {"ok": False, "error": "not editable"}
    frappe_field, label = EDITABLE_LEAD_FIELDS[field_key]
    value = (value or "").strip()
    if field_key == "phone" and not value:
        return {"ok": False, "error": "Phone number can't be empty"}
    d = frappe("GET", docpath("CRM Lead", lead_id)).get("data", {})
    old = (d.get(frappe_field) or "").strip()
    if value == old:
        return {"ok": True, field_key: value}
    frappe("PUT", docpath("CRM Lead", lead_id), body={frappe_field: value})
    frappe("POST", "/api/resource/Comment",
           body={"doctype": "Comment", "comment_type": "Info",
                 "reference_doctype": "CRM Lead", "reference_name": lead_id,
                 "content": f"{label} updated: “{old or '(empty)'}” → “{value or '(empty)'}”",
                 "comment_email": user, "comment_by": user})
    return {"ok": True, field_key: value}


def api_followup(lead_id, date, user):
    """Explicit 'call them on this date'. Replaces whatever was open so a lead
    never carries two open tasks."""
    date = (date or "").strip() or (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    closed = close_open_tasks(lead_id)
    schedule_next(lead_id, lead_display_name(lead_id), None, force_date=date)
    return {"ok": True, "date": date, "closed": closed}


def api_delete_lead(lead_id):
    """Permanently remove a lead — sales use this to clear out junk/duplicate/
    test entries. Delete linked CRM Tasks and Comments first so the lead
    delete itself never fails on a leftover reference. CRM Notification
    (auto-created on every assignment) also links the lead, and Frappe
    refuses the delete with LinkExistsError while any remain."""
    for n in flist("CRM Notification", ["name"],
                    filters=[["reference_doctype", "=", "CRM Lead"], ["reference_name", "=", lead_id]]):
        frappe("DELETE", docpath("CRM Notification", n["name"]))
    for t in flist("CRM Task", ["name"],
                    filters=[["reference_doctype", "=", "CRM Lead"], ["reference_docname", "=", lead_id]]):
        frappe("DELETE", docpath("CRM Task", t["name"]))
    for c in flist("Comment", ["name"],
                    filters=[["reference_doctype", "=", "CRM Lead"], ["reference_name", "=", lead_id]]):
        frappe("DELETE", docpath("Comment", c["name"]))
    frappe("DELETE", docpath("CRM Lead", lead_id))
    return {"ok": True}


def api_dashboard(owner=None):
    statuses = VALID_STATUSES
    owner_f = [["lead_owner", "=", owner]] if owner else []
    by_status = {s: fcount("CRM Lead", [["status", "=", s]] + owner_f) for s in statuses}
    total = fcount("CRM Lead", owner_f or None)
    # source + brand breakdown — fetch the two fields for all leads and count in Python
    # (Frappe rejects "count(name)" as a string in fields; this is simple + reliable)
    from collections import Counter
    allrows = flist("CRM Lead", ["source", "custom_brand"], filters=owner_f or None, limit=0)
    src_c = Counter((r.get("source") or "Other") for r in allrows)
    brand_c = Counter((r.get("custom_brand") or "Other") for r in allrows)
    # Website is the one source shared by both brands' sites — the source
    # filter's brand sub-menu needs its own counts, not just the combined
    # Website total.
    src_brand_c = Counter((r.get("source") or "Other", r.get("custom_brand") or "Other") for r in allrows)
    website_by_brand = {b: src_brand_c.get(("Website", b), 0) for b in ("Acme", "Acme Pro")}

    def grouped(counter):
        return [[k, v] for k, v in sorted(counter.items(), key=lambda x: -x[1])]
    today_end = time.strftime("%Y-%m-%d") + " 23:59:59"
    due_today = 0
    due_filters = [["reference_doctype", "=", "CRM Lead"], ["due_date", "<=", today_end]]
    owned_ids = None
    if owner:
        owned_ids = {r["name"] for r in flist("CRM Lead", ["name"], filters=owner_f, limit=0)}
    for t in flist("CRM Task", ["reference_docname", "status", "due_date"],
                   filters=due_filters, limit=0):
        if t.get("status") in CLOSED_TASK:
            continue
        if owned_ids is not None and t.get("reference_docname") not in owned_ids:
            continue
        due_today += 1
    month_start = datetime.date.today().replace(day=1).isoformat()
    today_iso = datetime.date.today().isoformat()
    my_sales = api_admin_sales_summary(month_start, today_iso, owner=owner)
    return {"total": total, "by_status": by_status, "by_source": grouped(src_c),
            "by_brand": grouped(brand_c), "due_today": due_today,
            "interested": by_status.get("Interested", 0), "website_by_brand": website_by_brand,
            "my_sales": {"count": my_sales["count"], "total_amount": my_sales["total_amount"]}}


def api_lead_timeseries(q, owner=None):
    """Dashboard's Lead Activity chart — per-day new-lead counts over an
    explicit [from, to] range the frontend computes for every preset (last
    7/30 days, last month, past 6 months/1 year, or a picked month), plus
    the same range's brand/source/status breakdown so the stat row under
    the chart always agrees with what's plotted."""
    from_date = q.get("from", [""])[0]
    to_date = q.get("to", [""])[0]
    for dv in (from_date, to_date):
        if dv and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dv):
            raise ValueError("bad date")
    if from_date and to_date and (datetime.date.fromisoformat(to_date) - datetime.date.fromisoformat(from_date)).days > 3660:
        raise ValueError("date range too large")
    filters = []
    if owner:
        filters.append(["lead_owner", "=", owner])
    if from_date:
        filters.append(["creation", ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append(["creation", "<=", f"{to_date} 23:59:59"])
    rows = flist("CRM Lead", ["creation", "custom_brand", "source", "status"],
                 filters=filters or None, limit=0)
    day_counts = {}
    total = brand_a = brand_b = meta = junk = 0
    for r in rows:
        day = (r.get("creation") or "")[:10]
        if not day:
            continue
        day_counts[day] = day_counts.get(day, 0) + 1
        total += 1
        if r.get("custom_brand") == "Acme":
            brand_a += 1
        elif r.get("custom_brand") == "Acme Pro":
            brand_b += 1
        if r.get("source") == "Meta Lead Ads":
            meta += 1
        if r.get("status") == "Junk":
            junk += 1
    # fill zero-count days so the line has no gaps — bounded by the explicit
    # range so this never silently returns an unbounded list
    days = []
    if from_date and to_date:
        d = datetime.date.fromisoformat(from_date)
        end = datetime.date.fromisoformat(to_date)
        while d <= end:
            key = d.isoformat()
            days.append([key, day_counts.get(key, 0)])
            d += datetime.timedelta(days=1)
    else:
        days = sorted(day_counts.items())
    return {"days": days, "total": total, "brand_a": brand_a, "brand_b": brand_b,
            "meta": meta, "junk": junk}


# ---------------- admin / team ----------------
DEAD_END_STATUSES = {"Converted", "Junk", "Not Interested", "Unqualified", "Qualified"}


def api_admin_team():
    """Team Overview — one flist call over every lead, aggregated in Python
    per rep: open leads owned, calls logged today, today's outcome
    breakdown, and a rough same-day-contact SLA (days between creation and
    last_contacted, only for leads contacted at least once). Doubles as the
    leaderboard (frontend sorts by calls_today) and daily-target-vs-actual
    (target comes from assignment_rules.json)."""
    reps = get_sales_users()
    rules = load_rules()
    targets = rules.get("daily_targets", {})
    today = datetime.date.today().isoformat()
    rows = flist("CRM Lead", ["lead_owner", "status", "creation", "custom_last_contacted"], limit=0)
    stats = {r["email"]: {"open": 0, "calls_today": 0, "today_outcomes": {}, "_sla": [], "total": 0}
              for r in reps}
    for row in rows:
        s = stats.get(row.get("lead_owner"))
        if s is None:
            continue  # owned by someone outside the current sales-rep list (e.g. a removed user)
        s["total"] += 1
        status = row.get("status") or "New"
        if status not in DEAD_END_STATUSES:
            s["open"] += 1
        lc = (row.get("custom_last_contacted") or "")[:10]
        if lc == today:
            s["calls_today"] += 1
            s["today_outcomes"][status] = s["today_outcomes"].get(status, 0) + 1
        created = (row.get("creation") or "")[:10]
        if lc and created:
            try:
                days = (datetime.date.fromisoformat(lc) - datetime.date.fromisoformat(created)).days
                if days >= 0:
                    s["_sla"].append(days)
            except ValueError:
                pass
    team = []
    for r in reps:
        s = stats[r["email"]]
        sla = round(sum(s["_sla"]) / len(s["_sla"]), 1) if s["_sla"] else None
        team.append({"email": r["email"], "name": r["name"], "admin": r["admin"],
                      "total": s["total"], "open": s["open"], "calls_today": s["calls_today"],
                      "today_outcomes": s["today_outcomes"], "target": targets.get(r["email"]),
                      "avg_contact_days": sla})
    team.sort(key=lambda t: -t["calls_today"])
    return {"team": team, "active_users": rules.get("active_users", [])}


def api_admin_sales_summary(from_date, to_date, owner=None):
    """Aggregates the per-lead purchase details captured on Convert (see
    api_set_purchase) into rep/brand totals for a date range — this is the
    "how much has each rep actually sold" view. Filters by the purchase's own buying_date, not
    the lead's creation/modified date, since a lead can convert well after it
    first came in. One flist call, everything else aggregated in Python —
    fine at this data volume, same approach as api_admin_team. `owner` scopes
    to one rep's own sales (used by the dashboard's "My Sales" tile so every
    rep sees their own number, not just admins) — omit for the full team."""
    reps = {u["email"]: u["name"] for u in get_sales_users()}
    want = (owner or "").strip().lower()
    rows = flist("CRM Lead", ["name", "lead_name", "first_name", "lead_owner", "custom_brand",
                               "custom_purchase_details"],
                 filters=[["status", "=", "Converted"]], limit=0)
    sales = []
    for r in rows:
        # a lead can hold several purchases (repeat buyer); each is its own sale, credited to
        # whoever entered it (older ones: the lead owner)
        for p in payments.purchases_of(r.get("custom_purchase_details")):
            bd = (p.get("buying_date") or "")[:10]
            if not bd or (from_date and bd < from_date) or (to_date and bd > to_date):
                continue
            own = payments.credit_of(p, r.get("lead_owner") or "")
            if want and own.lower() != want:
                continue
            nm = (r.get("lead_name") or r.get("first_name") or "Unknown").strip()
            sales.append({
                "lead_id": r["name"], "purchase_id": p["id"], "lead_name": nm, "owner": own,
                "owner_name": reps.get(own) or (own.split("@")[0].title() if own else "Unassigned"),
                "brand": r.get("custom_brand") or "Acme", "buying_date": bd,
                "items": p.get("items") or [], "total_amount": float(payments.view(p)["total"]),
            })
    sales.sort(key=lambda s: s["buying_date"], reverse=True)

    by_rep, by_brand, total_amount = {}, {}, 0.0
    for s in sales:
        rk = by_rep.setdefault(s["owner"], {"owner": s["owner"], "name": s["owner_name"], "count": 0, "total": 0.0})
        rk["count"] += 1
        rk["total"] += s["total_amount"]
        bk = by_brand.setdefault(s["brand"], {"brand": s["brand"], "count": 0, "total": 0.0})
        bk["count"] += 1
        bk["total"] += s["total_amount"]
        total_amount += s["total_amount"]

    return {"from": from_date, "to": to_date, "count": len(sales), "total_amount": total_amount,
            "by_rep": sorted(by_rep.values(), key=lambda x: -x["total"]),
            "by_brand": sorted(by_brand.values(), key=lambda x: -x["total"]),
            "sales": sales}


# ---- Marketing dashboard ---------------------------------
# "Where did you hear about us?" answers (custom_referral, filled from the
# website forms) vs. what the sales team later decided about each lead. Admin only.
MK_CHANNELS = ["ChatGPT", "Gemini", "Google", "Instagram", "Facebook", "Twitter / X",
               "Friends & Family", "Others"]
MK_NONE = "Not specified"
# sales verdict buckets, from the lead's CRM status
MK_BUCKET = {"Junk": "junk", "Interested": "interested", "Follow Up": "interested", "Qualified": "interested",
             "Not Interested": "not_interested", "Unqualified": "not_interested", "Converted": "converted"}


def mk_channel(ref):
    ref = (ref or "").strip()
    if not ref:
        return MK_NONE
    if ref.startswith("Others"):
        return "Others"
    return ref if ref in MK_CHANNELS else "Others"


def _mk_blank():
    return {"total": 0, "junk": 0, "interested": 0, "not_interested": 0, "converted": 0, "pending": 0, "sales": 0.0}


def _mk_finish(c):
    c["valid"] = c["total"] - c["junk"]
    c["junk_pct"] = round(100.0 * c["junk"] / c["total"], 1) if c["total"] else 0.0
    # interest rate = leads that turned out interested or bought, out of those the team has
    # actually judged (everything except still-pending ones)
    judged = c["total"] - c["pending"]
    c["judged"] = judged
    c["interest_pct"] = round(100.0 * (c["interested"] + c["converted"]) / judged, 1) if judged else 0.0
    c["sales"] = round(c["sales"])
    return c


def api_marketing(from_date, to_date, brand=""):
    filters = []
    if from_date:
        filters.append(["creation", ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append(["creation", "<=", f"{to_date} 23:59:59"])
    if brand in ("Acme", "Acme Pro"):
        filters.append(["custom_brand", "=", brand])
    rows = flist("CRM Lead", ["name", "status", "creation", "lead_owner", "custom_brand", "custom_referral",
                               "custom_purchase_details"], filters=filters or None, limit=0)
    reps = {u["email"]: u["name"] for u in get_sales_users()}
    chans = {k: _mk_blank() for k in MK_CHANNELS + [MK_NONE]}
    totals = _mk_blank()
    rep_rows = {}
    other_text = {}
    answered_since = None
    for r in rows:
        ref = (r.get("custom_referral") or "").strip()
        ch = mk_channel(ref)
        bucket = MK_BUCKET.get(r.get("status") or "New", "pending")
        amount = 0.0
        if bucket == "converted" and r.get("custom_purchase_details"):
            try:
                amount = float(payments.with_total(payments.purchases_of(r["custom_purchase_details"])))
            except Exception:
                amount = 0.0
        owner = r.get("lead_owner") or ""
        for tgt in (chans[ch], totals):
            tgt["total"] += 1
            tgt[bucket] += 1
            tgt["sales"] += amount
        if ref:
            ck = (r.get("creation") or "")[:10]
            if ck and (answered_since is None or ck < answered_since):
                answered_since = ck
            rp = rep_rows.setdefault(owner, {"owner": owner,
                                              "name": reps.get(owner) or (owner.split("@")[0].title() if owner else "Unassigned"),
                                              **_mk_blank()})
            rp["total"] += 1
            rp[bucket] += 1
            rp["sales"] += amount
        if ref.startswith("Others"):
            txt = ref.split(":", 1)[1].strip() if ":" in ref else "(no text)"
            other_text[txt.lower()] = other_text.get(txt.lower(), [txt, 0])
            other_text[txt.lower()][1] += 1
    out_ch = []
    for name in MK_CHANNELS + [MK_NONE]:
        c = _mk_finish(chans[name])
        c["channel"] = name
        out_ch.append(c)
    answered = totals["total"] - chans[MK_NONE]["total"]
    totals = _mk_finish(totals)
    reps_out = [_mk_finish(v) for v in rep_rows.values()]
    reps_out.sort(key=lambda x: -x["total"])
    # insight: best/worst channel among channels with enough judged leads to mean something
    ranked = [c for c in out_ch if c["channel"] != MK_NONE and c["judged"] >= 3]
    best = max(ranked, key=lambda c: c["interest_pct"]) if ranked else None
    worst = max(ranked, key=lambda c: c["junk_pct"]) if ranked else None
    return {"from": from_date, "to": to_date, "brand": brand or "all",
            "totals": totals, "answered": answered,
            "answered_pct": round(100.0 * answered / totals["total"], 1) if totals["total"] else 0.0,
            "channels": out_ch, "reps": reps_out,
            "others": sorted(({"text": v[0], "count": v[1]} for v in other_text.values()), key=lambda x: -x["count"])[:50],
            "answers_since": answered_since,
            "best": best["channel"] if best else None, "worst": worst["channel"] if worst else None}


# ---- Sales Report page (year -> sales person -> month -> month detail) ------
#"All Sales Report" page behind the dashboard's This Month's
# Sales tile: per sales person a 12-month table for the selected year, click a
# month for the full list of who bought what, plus Excel/PDF export. Built on
# the same per-lead purchase details as api_admin_sales_summary. Purchase
# capture only started on SALES_DATA_START, so earlier months simply have no data.
SALES_DATA_START = "2026-01-01"
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def _money_fmt(n):
    """US digit grouping (1,234,567) for exports."""
    return f"{int(round(float(n or 0))):,}"


def _num(x):
    x = float(x or 0)
    return int(x) if x == int(x) else x


def _collect_purchases(owner=None):
    """Every Converted lead split into (sales = ONE ROW PER PURCHASE, leads Converted but missing
    purchase details, rep name map). A lead can hold several purchases (repeat buyer); each counts
    in the month of its own buying date for the person it is credited to (payments.credit_of).
    owner=<email> keeps only the sales credited to that person. One flist call."""
    reps = {u["email"]: u["name"] for u in get_sales_users()}
    want = (owner or "").strip().lower()
    rows = flist("CRM Lead", ["name", "lead_name", "first_name", "lead_owner", "custom_brand",
                               "custom_purchase_details", "mobile_no", "email", "modified"],
                 filters=[["status", "=", "Converted"]], limit=0)
    sales, missing = [], []
    for r in rows:
        lown = r.get("lead_owner") or ""
        nm = (r.get("lead_name") or r.get("first_name") or "Unknown").strip()
        plist = [p for p in payments.purchases_of(r.get("custom_purchase_details")) if (p.get("buying_date") or "")[:10]]
        if not plist:
            if not want or lown.lower() == want:
                oname = reps.get(lown) or (lown.split("@")[0].title() if lown else "Unassigned")
                missing.append({"lead_id": r["name"], "lead_name": nm, "owner": lown, "owner_name": oname,
                                "modified": (r.get("modified") or "")[:10]})
            continue
        plist.sort(key=lambda p: ((p.get("buying_date") or "")[:10], p.get("recorded_at") or ""))
        for n, p in enumerate(plist, 1):
            own = payments.credit_of(p, lown)
            if want and own.lower() != want:
                continue
            oname = reps.get(own) or (own.split("@")[0].title() if own else "Unassigned")
            bd = (p.get("buying_date") or "")[:10]
            items = []
            for it in p.get("items") or []:
                q, pr = float(it.get("qty") or 0), float(it.get("price") or 0)
                items.append({"product": str(it.get("product") or "").strip(), "qty": _num(q),
                              "price": _num(pr), "line_total": q * pr})
            pv = payments.view(p)                    # ledger -> received / balance / status (single source of truth)
            tot = float(pv["total"])
            known = pv["payments_known"]
            sales.append({"lead_id": r["name"], "purchase_id": p["id"], "client": nm, "phone": r.get("mobile_no") or "",
                          "email": r.get("email") or "", "owner": own, "owner_name": oname,
                          "brand": r.get("custom_brand") or "Acme", "buying_date": bd,
                          "purchase_no": n, "purchase_count": len(plist),
                          "items": items, "total_amount": tot, "pay_status": pv["status"],
                          "payments": [{"amount": float(x.get("amount") or 0), "date": x.get("date") or "",
                                        "method": x.get("method") or ""} for x in pv["payments"]],
                          # `advance` is kept as the key name for the aggregations below but now means
                          # "received so far"; None = payment never recorded (legacy sale), not zero.
                          "advance": float(pv["received"]) if known else None,
                          "balance": float(pv["balance"]) if known else None})
    return sales, missing, reps


def _adv_sums(sales_subset):
    """Received and balance due over sales whose payment IS known; sales saved before payment
    tracking (never recorded) are counted separately - unknown is not zero and not full - so
    total == received + balance + unknown_amount always holds."""
    rec = [s for s in sales_subset if s.get("advance") is not None]
    unk = [s for s in sales_subset if s.get("advance") is None]
    return {"advance": sum(s["advance"] for s in rec), "balance": sum(s["balance"] for s in rec),
            "adv_recorded": len(rec), "adv_missing": len(unk),
            "unknown_amount": sum(s["total_amount"] for s in unk)}


def _year_months(sales_subset, year):
    """12 month rows. `future` months (after today's month) are not shown by the UI or
    exports; the `current` month is always shown, with zeros when nothing sold yet."""
    today = datetime.date.today()
    out = []
    for m in range(1, 13):
        ms = [s for s in sales_subset if int(s["buying_date"][5:7]) == m]
        cnt = len(ms)
        tot = sum(s["total_amount"] for s in ms)
        out.append({"month": m, "label": MONTH_NAMES[m - 1], "count": cnt, **_adv_sums(ms),
                    "converted": len({s["lead_id"] for s in ms}), "total": tot,
                    "avg": (tot / cnt if cnt else 0), "has_data": cnt > 0,
                    "future": (year, m) > (today.year, today.month),
                    "current": (year, m) == (today.year, today.month)})
    return out


def _year_total(months):
    cnt = sum(m["count"] for m in months)
    tot = sum(m["total"] for m in months)
    return {"count": cnt, "converted": sum(m["converted"] for m in months), "total": tot,
            "avg": (tot / cnt if cnt else 0),
            "advance": sum(m["advance"] for m in months), "balance": sum(m["balance"] for m in months),
            "adv_recorded": sum(m["adv_recorded"] for m in months), "adv_missing": sum(m["adv_missing"] for m in months),
            "unknown_amount": sum(m["unknown_amount"] for m in months)}


def api_sales_report(year, owner=None, admin=False):
    year = int(year)
    sales, _missing, reps = _collect_purchases(owner=owner)
    ys = [s for s in sales if s["buying_date"][:4] == f"{year:04d}"]
    if admin:
        keys = dict(reps)
        for s in ys:
            keys.setdefault(s["owner"], s["owner_name"])
    else:
        keys = {owner: reps.get(owner) or (owner or "").split("@")[0].title()}
    rep_rows = []
    for em, nm in keys.items():
        months = _year_months([s for s in ys if (s["owner"] or "").lower() == (em or "").lower()], year)
        rep_rows.append({"owner": em, "name": nm, "months": months, "year_total": _year_total(months)})
    rep_rows.sort(key=lambda r: (-r["year_total"]["total"], r["name"].lower()))
    team = None
    if admin:
        tm = _year_months(ys, year)
        team = {"name": "All Sales Persons", "months": tm, "year_total": _year_total(tm)}
    return {"year": year, "data_from": SALES_DATA_START, "scope": "all" if admin else "own",
            "current_year": datetime.date.today().year, "current_month": datetime.date.today().month,
            "has_any": bool(ys),
            "reps": rep_rows, "team": team}


def api_sales_report_month(year, month, owner=None):
    """owner=None -> every sales person; otherwise only that rep's sales."""
    year, month = int(year), int(month)
    if not 1 <= month <= 12:
        return {"error": "bad month"}
    prefix = f"{year:04d}-{month:02d}"
    sales, missing, reps = _collect_purchases(owner=owner)
    # oldest sale first (ascending by date); same-day sales by client name so the order is stable
    ms = sorted([s for s in sales if s["buying_date"].startswith(prefix)],
                key=lambda s: (s["buying_date"], s["client"].lower(), s["lead_id"]))
    total = sum(s["total_amount"] for s in ms)
    by_brand, by_rep = {}, {}
    for s in ms:
        b = by_brand.setdefault(s["brand"], {"brand": s["brand"], "count": 0, "total": 0.0, "advance": 0.0, "balance": 0.0, "unknown": 0.0})
        b["count"] += 1
        b["total"] += s["total_amount"]
        r = by_rep.setdefault(s["owner"], {"owner": s["owner"], "name": s["owner_name"], "count": 0, "total": 0.0, "advance": 0.0, "balance": 0.0, "unknown": 0.0})
        r["count"] += 1
        r["total"] += s["total_amount"]
        if s["advance"] is not None:
            b["advance"] += s["advance"]; b["balance"] += s["balance"]
            r["advance"] += s["advance"]; r["balance"] += s["balance"]
        else:
            b["unknown"] += s["total_amount"]; r["unknown"] += s["total_amount"]
    miss_n = 0
    if prefix >= SALES_DATA_START[:7]:
        miss_n = len([m for m in missing if m["modified"].startswith(prefix)])
    return {"year": year, "month": month, "label": f"{MONTH_NAMES[month - 1]} {year}",
            "owner": owner or "", "rep_name": (reps.get(owner) or owner.split("@")[0].title()) if owner else "All Sales Persons",
            "data_from": SALES_DATA_START, "count": len(ms),
            "converted": len({s["lead_id"] for s in ms}), "total": total, **_adv_sums(ms),
            "avg": (total / len(ms) if ms else 0),
            "by_brand": sorted(by_brand.values(), key=lambda x: -x["total"]),
            "by_rep": sorted(by_rep.values(), key=lambda x: -x["total"]),
            "missing_details": miss_n, "sales": ms}


def api_pending_balances(owner=None):
    """Every sale whose balance is still due, oldest first - the 'who do I chase' list.
    owner=None (admin) -> all sales persons; otherwise only that rep's own sales."""
    sales, _missing, _reps = _collect_purchases(owner=owner)
    today = datetime.date.today()
    rows = []
    for s in sales:
        if s["balance"] is None or s["balance"] <= 0:
            continue
        try:
            days = (today - datetime.date.fromisoformat(s["buying_date"][:10])).days
        except ValueError:
            days = 0
        last = max((x["date"] for x in s["payments"] if x["date"]), default="")
        rows.append({"lead_id": s["lead_id"], "client": s["client"], "phone": s["phone"], "owner": s["owner"],
                     "owner_name": s["owner_name"], "brand": s["brand"], "buying_date": s["buying_date"],
                     "days": days, "total": s["total_amount"], "received": s["advance"], "balance": s["balance"],
                     "status": s["pay_status"], "last_payment": last})
    rows.sort(key=lambda r: (r["buying_date"], r["client"].lower()))
    return {"scope": "all" if owner is None else "own", "count": len(rows),
            "balance": sum(r["balance"] for r in rows), "rows": rows}


def _items_text(items, money=True):
    parts = []
    for it in items:
        t = f"{it['product']} x {it['qty']}"
        if it.get("price"):
            t += f" @ ${_money_fmt(it['price'])}" if money else f" @ {_money_fmt(it['price'])}"
        parts.append(t)
    return "; ".join(parts)


def leads_export(fmt, q, owner=None):
    """-> (bytes, content_type, filename). Exports exactly the leads the Leads page
    is showing (same status/source/brand/date/search filters, or the Today's Calls
    list), ALL pages not just the visible one. Non-admins are scoped to `owner`."""
    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    if q.get("today", [""])[0] == "1":
        leads = api_today(owner=owner)["leads"]
        scope = "Today's Calls"
    else:
        filters, or_filters = build_lead_filters(
            status=q.get("status", [""])[0], source=q.get("source", [""])[0],
            brand=q.get("brand", [""])[0], from_date=q.get("from_date", [""])[0],
            to_date=q.get("to_date", [""])[0], search=q.get("q", [""])[0].strip(), owner=owner)
        rows = flist("CRM Lead", LEAD_FIELDS, filters=filters or None, or_filters=or_filters,
                     order_by="modified desc", limit=0)
        leads = [shape(r) for r in rows]
        enrich_leads(leads)
        bits = [q.get(k, [""])[0] for k in ("status", "source", "brand")]
        bits = [b for b in bits if b]
        fd, td = q.get("from_date", [""])[0], q.get("to_date", [""])[0]
        if fd or td:
            bits.append(f"{fd or '...'} to {td or '...'}")
        if q.get("q", [""])[0].strip():
            bits.append(f"search '{q['q'][0].strip()[:30]}'")
        scope = ", ".join(bits) if bits else "All leads"
    def who(e):
        return e.split("@")[0].title() if e else "Unassigned"
    fname = re.sub(r"[^A-Za-z0-9]+", "-", f"Leads-{scope}-{datetime.datetime.now():%Y-%m-%d}").strip("-")[:80]
    title = f"Leads - {scope}"
    sub = f"Generated {stamp}. {len(leads)} lead(s)."
    if fmt == "xlsx":
        head = [(h, "head") for h in ("Lead ID", "Name", "Phone", "Email", "Brand", "Source", "Status", "City",
                                       "Requirement", "Owner", "Created", "Last contacted", "Next follow-up")]
        data_rows = [[l["id"], l["name"], l["phone"], l["email"], l["brand"], l["source"], l["status"], l["city"],
                      l["req"], who(l["owner"]), l["created"][:16].replace("T", " "), l["last_contacted"],
                      (l.get("next_followup") or "")[:10]] for l in leads]
        rows = [[(title, "title")], [(sub, "sub")], [], head] + data_rows
        data = exporters.make_xlsx([{"name": "Leads", "rows": rows,
                                     "widths": [18, 24, 15, 28, 12, 16, 14, 14, 50, 14, 17, 14, 14], "freeze": 4}])
        return data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fname + ".xlsx"
    rows = [[l["name"], l["phone"], l["brand"], l["source"], l["status"], who(l["owner"]),
             l["created"][:10], l["req"]] for l in leads]
    blocks = [("table", ["Name", "Phone", "Brand", "Source", "Status", "Owner", "Created", "Requirement"],
               rows, [13, 14, 11, 12, 10, 8, 10, 22], ["l"] * 8)] if rows else [("para", "No leads match this view.")]
    return exporters.make_pdf(title, sub, blocks), "application/pdf", fname + ".pdf"


def _cl(s):
    """Client name for exports; a repeat purchase is marked so the same client in two months is clearly two sales."""
    return s["client"] + (f" (repeat #{s['purchase_no']})" if s.get("purchase_no", 1) > 1 else "")


def sales_report_export(fmt, year, month=None, owner=None, admin=False):
    """-> (bytes, content_type, filename). month=None -> the year report."""
    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    if month:
        d = api_sales_report_month(year, month, owner=owner)
        who = d["rep_name"]
        title = f"Sales Report - {who} - {d['label']}"
        fname = re.sub(r"[^A-Za-z0-9]+", "-", f"Sales-Report-{who}-{d['label']}").strip("-")
        sub = (f"Generated {stamp}. Sales are counted by buying date; purchase tracking started on 1 Jan 2026. "
               "Received and balance come from each sale\'s payment ledger; sales saved before payment tracking with no payment recorded are listed separately.")
        if fmt == "xlsx":
            summ = [[(title, "title")], [(sub, "sub")], [],
                    [("Total sales ($)", "bold"), (_num(d["total"]), "moneyb")],
                    [("Number of sales", "bold"), d["count"]],
                    [("People converted", "bold"), d["converted"]],
                    [("Average sale ($)", "bold"), (round(d["avg"]), "money")],
                    [("Payment received ($)", "bold"), (_num(d["advance"]), "money")],
                    [("Balance due ($)", "bold"), (_num(d["balance"]), "money")]]
            if d["adv_missing"]:
                summ.append([("Sales with payment not recorded (not in received/balance)", "bold"), d["adv_missing"]])
            if d["missing_details"]:
                summ.append([("Converted leads without purchase details", "bold"), d["missing_details"]])
            summ += [[], [("By brand", "head"), ("Sales", "head"), ("Total ($)", "head"), ("Received ($)", "head"), ("Balance ($)", "head")]]
            summ += [[b["brand"], b["count"], (_num(b["total"]), "money"), (_num(b["advance"]), "money"), (_num(b["balance"]), "money")] for b in d["by_brand"]]
            if not owner:
                summ += [[], [("By sales person", "head"), ("Sales", "head"), ("Total ($)", "head"), ("Received ($)", "head"), ("Balance ($)", "head")]]
                summ += [[r["name"], r["count"], (_num(r["total"]), "money"), (_num(r["advance"]), "money"), (_num(r["balance"]), "money")] for r in d["by_rep"]]
            sale_rows = [[("Date", "head"), ("Client", "head"), ("Phone", "head"), ("Sales Person", "head"),
                          ("Brand", "head"), ("What they bought", "head"), ("Total ($)", "head"),
                          ("Received ($)", "head"), ("Balance ($)", "head"), ("Payment status", "head")]]
            item_rows = [[("Date", "head"), ("Client", "head"), ("Sales Person", "head"), ("Brand", "head"),
                          ("Product", "head"), ("Qty", "head"), ("Unit price ($)", "head"), ("Line total ($)", "head")]]
            for s in d["sales"]:
                sale_rows.append([s["buying_date"], _cl(s), s["phone"], s["owner_name"], s["brand"],
                                  _items_text(s["items"], money=False), (_num(s["total_amount"]), "money"),
                                  (_num(s["advance"]), "money") if s["advance"] is not None else "",
                                  (_num(s["balance"]), "money") if s["advance"] is not None else "",
                                  {"paid": "Paid in full", "partial": "Part paid", "unpaid": "Unpaid"}.get(s["pay_status"], "Not recorded")])
                for it in s["items"]:
                    item_rows.append([s["buying_date"], _cl(s), s["owner_name"], s["brand"], it["product"],
                                      it["qty"], (it["price"], "money") if it["price"] else "",
                                      (_num(it["line_total"]), "money") if it["price"] else ""])
            if not d["sales"]:
                sale_rows.append(["No data for this month"])
            data = exporters.make_xlsx([
                {"name": "Summary", "rows": summ, "widths": [38, 16, 16, 16, 16]},
                {"name": "Sales", "rows": sale_rows, "widths": [12, 26, 16, 18, 12, 60, 16, 16, 16, 16], "freeze": 1},
                {"name": "Items", "rows": item_rows, "widths": [12, 26, 18, 12, 40, 8, 16, 16], "freeze": 1}])
            return data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fname + ".xlsx"
        blocks = [("heading", "Month summary"),
                  ("para", f"Total sales: ${_money_fmt(d['total'])}  |  Sales: {d['count']}  |  People converted: "
                           f"{d['converted']}  |  Average sale: ${_money_fmt(d['avg'])}"),
                  ("para", f"Payment received: ${_money_fmt(d['advance'])}  |  Balance due: ${_money_fmt(d['balance'])}"
                           + (f"  ({d['adv_missing']} sale(s) have no payment recorded yet and are not counted in received/balance)" if d["adv_missing"] else ""))]
        if d["missing_details"]:
            blocks.append(("para", f"Note: {d['missing_details']} converted lead(s) this month have no purchase details recorded yet."))
        if d["by_brand"]:
            blocks.append(("table", ["Brand", "Sales", "Total ($)", "Received ($)", "Balance ($)"],
                           [[b["brand"], str(b["count"]), _money_fmt(b["total"]), _money_fmt(b["advance"]), _money_fmt(b["balance"])] for b in d["by_brand"]],
                           [28, 10, 22, 20, 20], ["l", "r", "r", "r", "r"]))
        if not owner and d["by_rep"]:
            blocks.append(("table", ["Sales person", "Sales", "Total ($)", "Received ($)", "Balance ($)"],
                           [[r["name"], str(r["count"]), _money_fmt(r["total"]), _money_fmt(r["advance"]), _money_fmt(r["balance"])] for r in d["by_rep"]],
                           [28, 10, 22, 20, 20], ["l", "r", "r", "r", "r"]))
        blocks.append(("heading", "All sales this month"))
        if d["sales"]:
            rows = [[s["buying_date"], _cl(s) + (f" ({s['phone']})" if s["phone"] else ""), s["owner_name"],
                     s["brand"], _items_text(s["items"]), _money_fmt(s["total_amount"]),
                     _money_fmt(s["advance"]) if s["advance"] is not None else "-",
                     _money_fmt(s["balance"]) if s["advance"] is not None else "-"] for s in d["sales"]]
            rows.append(["", "", "", "", "Month total", _money_fmt(d["total"]), _money_fmt(d["advance"]), _money_fmt(d["balance"])])
            blocks.append(("table", ["Date", "Client", "Sales person", "Brand", "What they bought", "Total ($)", "Received", "Balance"],
                           rows, [11, 15, 10, 11, 20, 11, 11, 11], ["l", "l", "l", "l", "l", "r", "r", "r"], True))
        else:
            blocks.append(("para", "No data for this month."))
        return exporters.make_pdf(title, sub, blocks), "application/pdf", fname + ".pdf"
    # ---- year report ----
    d = api_sales_report(year, owner=owner if not admin else None, admin=admin)
    blocks_src = ([d["team"]] if d["team"] else []) + d["reps"]
    title = f"Sales Report - {d['year']}"
    fname = f"Sales-Report-{d['year']}"
    sub = (f"Generated {stamp}. " + ("All sales persons. " if admin else "Your sales only. ") +
           "Sales are counted by buying date; purchase tracking started on 1 Jan 2026, earlier months have no data. "
           "Received and balance come from each sale\'s payment ledger; sales saved before payment tracking with no payment recorded are listed separately.")
    if fmt == "xlsx":
        rows = [[(title, "title")], [(sub, "sub")], []]
        if not d["has_any"]:
            rows.append([("No sales data for this year", "bold")])
            rows.append([])
        rows.append([("Sales Person", "head"), ("Month", "head"), ("Sales", "head"), ("People converted", "head"),
                     ("Total ($)", "head"), ("Received ($)", "head"), ("Balance ($)", "head"), ("Note", "head")])
        for blk in blocks_src:
            for m in blk["months"]:
                if m["future"]:
                    continue
                if m["has_data"] or m["current"]:
                    rows.append([blk["name"], m["label"], m["count"], m["converted"], (_num(m["total"]), "money"),
                                 (_num(m["advance"]), "money"), (_num(m["balance"]), "money"),
                                 ("" if m["has_data"] else ("No sales yet this month", "note"))])
                else:
                    rows.append([blk["name"], m["label"], "", "", "", "", "", ("No data for this month", "note")])
            yt = blk["year_total"]
            rows.append([(blk["name"], "bold"), (f"Total {d['year']}", "bold"), (yt["count"], "bold"), (yt["converted"], "bold"),
                         (_num(yt["total"]), "moneyb"), (_num(yt["advance"]), "moneyb"), (_num(yt["balance"]), "moneyb"), ""])
            rows.append([])
        data = exporters.make_xlsx([{"name": f"Sales {d['year']}", "rows": rows,
                                     "widths": [26, 14, 10, 18, 18, 18, 18, 26], "freeze": 0}])
        return data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", fname + ".xlsx"
    blocks = []
    if not d["has_any"]:
        blocks.append(("para", "No sales data for this year."))
    for blk in blocks_src:
        blocks.append(("heading", blk["name"]))
        rows = []
        for m in blk["months"]:
            if m["future"]:
                continue
            if m["has_data"] or m["current"]:
                rows.append([m["label"], str(m["count"]), str(m["converted"]), _money_fmt(m["total"]),
                             _money_fmt(m["advance"]), _money_fmt(m["balance"]),
                             "" if m["has_data"] else "No sales yet this month"])
            else:
                rows.append([m["label"], "-", "-", "-", "-", "-", "No data for this month"])
        yt = blk["year_total"]
        rows.append([f"Total {d['year']}", str(yt["count"]), str(yt["converted"]), _money_fmt(yt["total"]),
                     _money_fmt(yt["advance"]), _money_fmt(yt["balance"]), ""])
        blocks.append(("table", ["Month", "Sales", "Converted", "Total ($)", "Received ($)", "Balance ($)", "Note"],
                       rows, [12, 8, 10, 16, 16, 16, 22], ["l", "r", "r", "r", "r", "r", "l"], True))
    return exporters.make_pdf(title, sub, blocks), "application/pdf", fname + ".pdf"


def api_admin_reassign(lead_ids, owner, by="", from_email="", filter_desc="all open leads"):
    lead_ids = lead_ids or []
    owner = (owner or "").strip()
    if not lead_ids or not owner:
        return {"ok": False, "error": "lead_ids and owner required"}
    valid = {u["email"] for u in get_sales_users()}
    if owner not in valid:
        return {"ok": False, "error": "not a known sales user"}
    done = 0
    for lid in lead_ids:
        try:
            frappe("PUT", docpath("CRM Lead", lid), body={"lead_owner": owner})
            done += 1
        except Exception:
            pass
    if done:
        log_reassign(by, from_email or "?", owner, done, "manual", filter_desc)
    return {"ok": True, "reassigned": done}


def api_admin_create_user(body):
    """Quick-add a new Sales User straight from the Admin panel — always
    Sales User (never admin) so granting System Manager stays a deliberate,
    separate decision, never a side effect of hiring someone new. No SMTP is
    configured on this CRM (typical for an internal install), so the password is set
    directly here and handed over out-of-band."""
    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return {"ok": False, "error": "Enter a valid email address"}
    if not name:
        return {"ok": False, "error": "Name is required"}
    if len(password) < 8:
        return {"ok": False, "error": "Password must be at least 8 characters"}
    try:
        frappe("POST", "/api/resource/User", body={
            "email": email, "first_name": name,
            "send_welcome_email": 0, "new_password": password,
            "roles": [{"role": "Sales User"}],
        })
    except Exception as e:
        msg = str(e)
        if "already exist" in msg.lower() or "duplicate" in msg.lower():
            return {"ok": False, "error": "This email is already a user"}
        return {"ok": False, "error": "Frappe error: " + msg[:200]}
    return {"ok": True, "email": email}


def api_admin_schedule_get():
    return load_schedule()


def api_admin_schedule_add(body, by=""):
    """Queue a future reassignment — an external cron job applies it on
    the due date (see apply_scheduled_reassignments() there), not this
    process, so it fires even if nobody has the leadapp open that day."""
    from_email = (body.get("from_email") or "").strip()
    to_email = (body.get("to_email") or "").strip()
    date = (body.get("date") or "").strip()
    from_date = (body.get("from_date") or "").strip()
    to_date = (body.get("to_date") or "").strip()
    open_only = bool(body.get("open_only", True))
    valid = {u["email"] for u in get_sales_users()}
    if from_email not in valid or to_email not in valid or from_email == to_email:
        return {"ok": False, "error": "Select valid reps (from and to must be different)"}
    try:
        if datetime.date.fromisoformat(date) < datetime.date.today():
            return {"ok": False, "error": "Date must be today or in the future"}
    except ValueError:
        return {"ok": False, "error": "Enter a valid date (YYYY-MM-DD)"}
    s = load_schedule()
    item = {"id": hashlib.sha1(f"{date}{from_email}{to_email}{time.time()}".encode()).hexdigest()[:10],
            "date": date, "from_email": from_email, "to_email": to_email,
            "from_date": from_date, "to_date": to_date, "open_only": open_only,
            "status": "pending", "created_by": by,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
    s["items"].append(item)
    save_schedule(s)
    return {"ok": True, "id": item["id"]}


def api_admin_schedule_cancel(sid):
    s = load_schedule()
    before = len(s["items"])
    s["items"] = [i for i in s["items"] if not (i["id"] == sid and i["status"] == "pending")]
    save_schedule(s)
    return {"ok": before != len(s["items"])}


def api_admin_log_get():
    return load_reassign_log()


def api_admin_rules_get():
    rules = load_rules()
    allrows = flist("CRM Lead", ["source"], limit=0)
    sources = sorted({r.get("source") for r in allrows if r.get("source")})
    return {**rules, "sales_users": get_sales_users(), "sources": sources}


def api_admin_rules_set(body):
    rules = load_rules()
    if "source_rules" in body:
        rules["source_rules"] = {k: v for k, v in (body.get("source_rules") or {}).items() if v}
    if "active_users" in body:
        rules["active_users"] = [u for u in (body.get("active_users") or []) if u]
    if "daily_targets" in body:
        dt = {}
        for k, v in (body.get("daily_targets") or {}).items():
            try:
                dt[k] = int(v)
            except (TypeError, ValueError):
                pass
        rules["daily_targets"] = dt
    save_rules(rules)
    return {"ok": True, "rules": rules}


# ---------------- session / cookies ----------------
def sign(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(raw).decode()
    sig = hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{b}.{sig}"


def verify(cookie):
    try:
        b, sig = cookie.split(".", 1)
        if hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()[:24] != sig:
            return None
        p = json.loads(base64.urlsafe_b64decode(b.encode()))
        if p.get("exp", 0) < time.time():
            return None
        return p
    except Exception:
        return None


_FAILS = {}   # ip -> timestamps of recent failed logins (in-memory brute-force throttle)


def _login_blocked(ip, limit=8, window=300):
    now = time.time()
    _FAILS[ip] = [t for t in _FAILS.get(ip, []) if now - t < window]
    return len(_FAILS[ip]) >= limit


def do_login(usr, pwd):
    """Validate against the CRM's own login so we reuse CRM credentials."""
    if DEMO:
        return demo_store.check_login(usr, pwd)
    try:
        data = urllib.parse.urlencode({"usr": usr, "pwd": pwd}).encode()
        req = urllib.request.Request(CRM + "/api/method/login", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


# ---------------- HTTP handler ----------------
class H(BaseHTTPRequestHandler):
    server_version = "AcmeLeads/1.0"

    def _cookies(self):
        c = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1); c[k] = v
        return c

    def _user(self):
        return verify(self._cookies().get("sess", ""))

    def _sec_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")

    def _cookie(self, value, max_age):
        return f"sess={value}; Path=/; HttpOnly; Max-Age={max_age}; SameSite=Lax" + ("; Secure" if COOKIE_SECURE else "")

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._sec_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, data, ctype, filename):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._sec_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not n or n > MAX_BODY:
            return {}
        try:
            b = json.loads(self.rfile.read(n).decode() or "{}")
            return b if isinstance(b, dict) else {}
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path == "/kiosk":
            return self._kiosk(q)
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/favicon.ico":
            return self._serve_favicon()
        if path == "/api/me":
            user = self._user()
            if not user:
                return self._json({"user": None})
            # Session cookie never carried full_name (only u/exp/admin) — this
            # used to fall back to titlecasing the email's local-part, so a
            # renamed Frappe full_name (e.g. "Admin") never showed up in the
            # topbar. Fetch it live instead, same source Team Overview already
            # trusts (get_sales_users' u.get("full_name")).
            display_name = None
            try:
                display_name = frappe("GET", docpath("User", user.get("u", ""))).get("data", {}).get("full_name")
            except Exception:
                pass
            return self._json({"user": user.get("u"), "admin": bool(user.get("admin")),
                                "name": display_name or user.get("u", "").split("@")[0].title()})
        # everything else under /api needs auth
        if path.startswith("/api/"):
            user = self._user()
            if not user:
                return self._json({"error": "auth"}, 401)
            admin = bool(user.get("admin"))
            owner = None if admin else user.get("u")
            try:
                if path == "/api/leads":
                    # admins may explicitly scope by a specific rep's email
                    # (the Admin > Reassign-all flow needs "give me X's open
                    # leads") — non-admins stay locked to their own session
                    # owner no matter what they pass.
                    eff_owner = owner if not admin else (q.get("owner", [""])[0] or None)
                    return self._json(api_leads(q, owner=eff_owner))
                if path == "/api/today":
                    return self._json(api_today(owner=owner))
                if path == "/api/dashboard":
                    return self._json(api_dashboard(owner=owner))
                if path == "/api/leads/export":
                    fmt = q.get("format", ["xlsx"])[0]
                    if fmt not in ("xlsx", "pdf"):
                        return self._json({"error": "format must be xlsx or pdf"}, 400)
                    data, ctype, fname = leads_export(fmt, q, owner=owner)
                    return self._file(data, ctype, fname)
                if path == "/api/leads/timeseries":
                    return self._json(api_lead_timeseries(q, owner=owner))
                if path.startswith("/api/lead/"):
                    _lid = urllib.parse.unquote(path[len("/api/lead/"):])
                    if not lead_access(_lid, user.get("u"), admin):
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_lead(_lid))
                if path == "/api/admin/team":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_admin_team())
                if path == "/api/marketing":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    f_from = q.get("from", [""])[0]
                    f_to = q.get("to", [""])[0]
                    for dv in (f_from, f_to):
                        if dv and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dv):
                            return self._json({"error": "bad date"}, 400)
                    return self._json(api_marketing(f_from, f_to, q.get("brand", [""])[0]))
                if path == "/api/admin/sales-summary":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    today = datetime.date.today()
                    from_date = q.get("from", [""])[0] or today.replace(day=1).isoformat()
                    to_date = q.get("to", [""])[0] or today.isoformat()
                    return self._json(api_admin_sales_summary(from_date, to_date))
                if path == "/api/pending-balances":
                    # admin: everyone (or ?owner=); a sales rep: only their own, whatever they pass
                    return self._json(api_pending_balances(owner=(owner if not admin else (q.get("owner", [""])[0] or None))))
                if path in ("/api/sales-report", "/api/sales-report/month", "/api/sales-report/export"):
                    # any logged-in user: admins see every sales person, reps are
                    # locked to their own session owner whatever they pass.
                    try:
                        year = int(q.get("year", [datetime.date.today().year])[0])
                        mraw = q.get("month", [""])[0]
                        month = int(mraw) if mraw else None
                    except ValueError:
                        return self._json({"error": "bad year/month"}, 400)
                    if not 2000 <= year <= 2100 or (month is not None and not 1 <= month <= 12):
                        return self._json({"error": "bad year/month"}, 400)
                    eff_owner = owner if not admin else (q.get("owner", [""])[0] or None)
                    if path == "/api/sales-report":
                        return self._json(api_sales_report(year, owner=owner, admin=admin))
                    if path == "/api/sales-report/month":
                        if month is None:
                            return self._json({"error": "month required"}, 400)
                        return self._json(api_sales_report_month(year, month, owner=eff_owner))
                    fmt = q.get("format", ["xlsx"])[0]
                    if fmt not in ("xlsx", "pdf"):
                        return self._json({"error": "format must be xlsx or pdf"}, 400)
                    data, ctype, fname = sales_report_export(fmt, year, month=month, owner=eff_owner, admin=admin)
                    return self._file(data, ctype, fname)
                if path == "/api/admin/rules":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_admin_rules_get())
                if path == "/api/admin/schedule":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_admin_schedule_get())
                if path == "/api/admin/log":
                    if not admin:
                        return self._json({"error": "forbidden"}, 403)
                    return self._json(api_admin_log_get())
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if path == "/api/login":
            usr = str(body.get("usr", "")).strip().lower()
            ip = self.client_address[0]
            if _login_blocked(ip):
                return self._json({"ok": False, "error": "Too many failed attempts, try again in a few minutes"}, 429)
            if do_login(usr, str(body.get("pwd", ""))):
                _FAILS.pop(ip, None)
                admin = is_admin(usr)
                cookie = self._cookie(sign({"u": usr, "exp": time.time() + COOKIE_TTL, "admin": admin}), COOKIE_TTL)
                return self._json({"ok": True, "user": usr, "admin": admin}, cookie=cookie)
            _FAILS.setdefault(ip, []).append(time.time())
            return self._json({"ok": False, "error": "Incorrect username or password"}, 401)
        if path == "/api/logout":
            return self._json({"ok": True}, cookie=self._cookie("", 0))
        user = self._user()
        if not user:
            return self._json({"error": "auth"}, 401)
        admin = bool(user.get("admin"))
        owner = None if admin else user.get("u")
        try:
            if path == "/api/leads/delete_all":
                return self._json(api_bulk_delete(body, owner=owner))
            if path == "/api/admin/reassign":
                if not admin:
                    return self._json({"error": "forbidden"}, 403)
                return self._json(api_admin_reassign(
                    body.get("lead_ids"), body.get("owner", ""), by=user.get("u", ""),
                    from_email=body.get("from_email", ""), filter_desc=body.get("filter_desc", "")))
            if path == "/api/admin/rules":
                if not admin:
                    return self._json({"error": "forbidden"}, 403)
                return self._json(api_admin_rules_set(body))
            if path == "/api/admin/users":
                if not admin:
                    return self._json({"error": "forbidden"}, 403)
                return self._json(api_admin_create_user(body))
            if path == "/api/admin/schedule":
                if not admin:
                    return self._json({"error": "forbidden"}, 403)
                return self._json(api_admin_schedule_add(body, by=user.get("u", "")))
            if path == "/api/admin/schedule/cancel":
                if not admin:
                    return self._json({"error": "forbidden"}, 403)
                return self._json(api_admin_schedule_cancel(body.get("id", "")))
            if path == "/api/leads/create":
                return self._json(api_create_lead(body, owner=user.get("u", "")))
            if path.startswith("/api/lead/"):
                rest = path[len("/api/lead/"):]
                lead_id, _, action = rest.partition("/")
                lead_id = urllib.parse.unquote(lead_id)
                if not lead_access(lead_id, user.get("u"), admin):
                    return self._json({"error": "forbidden"}, 403)
                if action == "status":
                    return self._json(api_set_status(lead_id, body.get("status", ""),
                                                     body.get("date") or None))
                if action == "called":
                    return self._json(api_called(lead_id, body.get("status", ""),
                                                 body.get("note", ""), body.get("date") or None,
                                                 user.get("u", "")))
                if action == "purchase":
                    return self._json(api_set_purchase(lead_id, body, user.get("u", ""), is_admin=bool(admin)))
                if action == "purchase-delete":
                    return self._json(api_delete_purchase(lead_id, body, user.get("u", ""), is_admin=bool(admin)))
                if action == "payment":
                    return self._json(api_payment(lead_id, body, user.get("u", "")))
                if action == "note":
                    return self._json(api_add_note(lead_id, body.get("content", ""), user.get("u", "")))
                if action == "name":
                    return self._json(api_update_name(lead_id, body.get("name", ""), user.get("u", "")))
                if action == "field":
                    return self._json(api_update_field(lead_id, body.get("field", ""),
                                                        body.get("value", ""), user.get("u", "")))
                if action == "followup":
                    return self._json(api_followup(lead_id, body.get("date", ""), user.get("u", "")))
                if action == "delete":
                    return self._json(api_delete_lead(lead_id))
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def _kiosk(self, q):
        """Desktop-shortcut auto-login: /kiosk?k=<key> sets the session and redirects home."""
        k = q.get("k", [""])[0]
        ok = bool(KIOSK_KEY) and hmac.compare_digest(k, KIOSK_KEY)
        self.send_response(302)
        self.send_header("Location", "/")
        if ok:
            self.send_header("Set-Cookie", self._cookie(
                sign({"u": KIOSK_USER, "exp": time.time() + COOKIE_TTL, "admin": is_admin(KIOSK_USER)}), COOKIE_TTL))
        self.end_headers()

    def _serve_favicon(self):
        try:
            with open(os.path.join(HERE, "favicon.ico"), "rb") as f:
                data = f.read()
        except FileNotFoundError:          # no icon shipped with the showcase
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/x-icon")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def _serve_index(self):
        try:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            try:
                import build           # index.html is a build artefact: assemble it on first run
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    data = f.read()
            except Exception:
                data = b"index.html missing - run: python3 build.py"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._sec_headers()
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"Acme Leads on :{PORT}  ->  CRM {CRM}")
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), PORT), H).serve_forever()
