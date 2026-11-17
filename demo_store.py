"""Embedded demo backend: a tiny SQLite emulation of the subset of the Frappe-style REST API
that app.py uses, so the app runs with zero external services.

Supported (everything app.py calls):
  GET    /api/resource/<doctype>            list: fields, filters, or_filters, order_by, limit_page_length, limit_start
  GET    /api/resource/<doctype>/<name>     one document
  POST   /api/resource/<doctype>            create
  PUT    /api/resource/<doctype>/<name>     partial update
  DELETE /api/resource/<doctype>/<name>
  GET    /api/method/frappe.client.get_count
Documents are stored as JSON in one table; filtering/sorting happens in Python (demo-sized data).
"""
import datetime, hashlib, hmac, json, os, re, sqlite3, sys, threading, urllib.parse, uuid

_LOCK = threading.RLock()
_DB = None


def init(path):
    """Open (or create) the demo database; seed it with invented data when empty."""
    global _DB
    _DB = sqlite3.connect(path, check_same_thread=False)
    _DB.execute("CREATE TABLE IF NOT EXISTS doc (doctype TEXT, name TEXT, data TEXT, PRIMARY KEY (doctype, name))")
    _DB.execute("CREATE TABLE IF NOT EXISTS counter (k TEXT PRIMARY KEY, v INTEGER)")
    _DB.commit()
    if not _DB.execute("SELECT 1 FROM doc LIMIT 1").fetchone():
        import seed_demo
        seed_demo.seed(sys.modules[__name__])



def reset():
    """Drop every document (used by `seed_demo.py --reset`)."""
    with _LOCK:
        _DB.execute("DELETE FROM doc")
        _DB.execute("DELETE FROM counter")
        _DB.commit()


# ---------------- storage helpers ----------------
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _next(key):
    with _LOCK:
        row = _DB.execute("SELECT v FROM counter WHERE k=?", (key,)).fetchone()
        n = (row[0] if row else 0) + 1
        _DB.execute("INSERT OR REPLACE INTO counter VALUES (?,?)", (key, n))
        return n


def _all(doctype):
    with _LOCK:
        rows = _DB.execute("SELECT data FROM doc WHERE doctype=?", (doctype,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _get(doctype, name):
    with _LOCK:
        row = _DB.execute("SELECT data FROM doc WHERE doctype=? AND name=?", (doctype, str(name))).fetchone()
    return json.loads(row[0]) if row else None


def _save(doctype, doc):
    with _LOCK:
        _DB.execute("INSERT OR REPLACE INTO doc VALUES (?,?,?)", (doctype, str(doc["name"]), json.dumps(doc)))
        _DB.commit()


def _new_name(doctype):
    if doctype == "CRM Lead":
        return f"LEAD-{_next(doctype):05d}"
    if doctype == "CRM Task":
        return _next(doctype)                 # integer names, like the real thing
    return uuid.uuid4().hex[:10]


def _derive(doctype, doc):
    if doctype == "CRM Lead":
        doc["lead_name"] = f"{doc.get('first_name') or ''} {doc.get('last_name') or ''}".strip()
    return doc


def insert(doctype, body, keep_times=False):
    """Create a document. keep_times=True lets the seed script back-date creation/modified."""
    doc = {k: v for k, v in (body or {}).items() if k != "doctype"}
    if doctype == "User":
        email = (doc.get("email") or doc.get("name") or "").strip().lower()
        if _get("User", email):
            raise RuntimeError(f"demo POST User -> 409: {email} already exists (duplicate)")
        doc["name"] = doc["email"] = email
        doc["full_name"] = doc.get("full_name") or doc.get("first_name") or email.split("@")[0].title()
        doc.setdefault("enabled", 1)
        doc.setdefault("user_type", "System User")
        doc.setdefault("roles", [])
        if "new_password" in doc:
            doc["_pwd"] = _hash(doc.pop("new_password"))
        doc.pop("send_welcome_email", None)
    else:
        doc["name"] = doc.get("name") or _new_name(doctype)
    now = _now()
    if not keep_times:
        doc["creation"] = now
        doc["modified"] = now
    else:
        doc.setdefault("creation", now)
        doc.setdefault("modified", doc["creation"])
    _derive(doctype, doc)
    _save(doctype, doc)
    return doc


# ---------------- passwords / login ----------------
def _hash(pwd, salt=None):
    salt = salt or os.urandom(8).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 20000).hex()
    return f"{salt}${dk}"


def check_login(usr, pwd):
    u = _get("User", (usr or "").strip().lower())
    if not u or not u.get("enabled") or not u.get("_pwd"):
        return False
    salt, _ = u["_pwd"].split("$", 1)
    return hmac.compare_digest(_hash(pwd or "", salt), u["_pwd"])


# ---------------- filtering ----------------
def _like(val, pat):
    rx = "^" + ".*".join(re.escape(p) for p in str(pat).split("%")) + "$"
    return re.match(rx, str(val or ""), re.I | re.S) is not None


def _match(doc, flt):
    if len(flt) == 4:                       # [doctype, field, op, value]
        flt = flt[1:]
    field, op, val = flt
    cur = doc.get(field)
    op = op.lower()
    if op in ("in", "not in"):
        hit = str(cur) in {str(v) for v in (val or [])}
        return hit if op == "in" else not hit
    if op == "like":
        return _like(cur, val)
    if op == "not like":
        return not _like(cur, val)
    a, b = ("" if cur is None else cur), ("" if val is None else val)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            a, b = float(a or 0), float(b or 0)
        except (TypeError, ValueError):
            a, b = str(a), str(b)
    else:
        a, b = str(a), str(b)
    return {"=": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}.get(op, False)


def _jload(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def _query(doctype, params):
    rows = _all(doctype)
    filters = _jload(params.get("filters"), [])
    or_filters = _jload(params.get("or_filters"), [])
    rows = [r for r in rows if all(_match(r, f) for f in filters)]
    if or_filters:
        rows = [r for r in rows if any(_match(r, f) for f in or_filters)]
    order = params.get("order_by") or "modified desc"
    for part in reversed([p.strip() for p in order.split(",") if p.strip()]):
        bits = part.replace("`", "").split()
        field, desc = bits[0].split(".")[-1], (len(bits) > 1 and bits[1].lower() == "desc")
        rows.sort(key=lambda r: (r.get(field) is None, str(r.get(field) if r.get(field) is not None else "")
                                 if not isinstance(r.get(field), (int, float)) else r.get(field)), reverse=desc)
    start = int(params.get("limit_start") or 0)
    limit = int(params.get("limit_page_length", 20) or 0)
    rows = rows[start:start + limit] if limit else rows[start:]
    fields = _jload(params.get("fields"), None)
    if fields:
        rows = [{f: r.get(f) for f in fields} for r in rows]
    return rows


def _public(doc):
    return {k: v for k, v in doc.items() if not k.startswith("_")}


# ---------------- the REST facade ----------------
def handle(method, path, params=None, body=None):
    params = params or {}
    path = urllib.parse.unquote(path)
    if path == "/api/method/frappe.client.get_count":
        dt = params.get("doctype", "")
        return {"message": len(_query(dt, {"filters": params.get("filters"), "limit_page_length": 0}))}
    m = re.match(r"^/api/resource/([^/]+)(?:/(.+))?$", path)
    if not m:
        raise RuntimeError(f"demo {method} {path} -> 404: unsupported endpoint")
    doctype, name = m.group(1), m.group(2)
    if name is None:
        if method == "GET":
            return {"data": [_public(r) for r in _query(doctype, params)]}
        if method == "POST":
            return {"data": _public(insert(doctype, body))}
    else:
        doc = _get(doctype, name)
        if doc is None:
            raise RuntimeError(f"demo {method} {path} -> 404: {doctype} {name} not found")
        if method == "GET":
            out = _public(doc)
            return {"data": out}
        if method == "PUT":
            for k, v in (body or {}).items():
                if k in ("name", "doctype", "creation"):
                    continue
                if k == "new_password":
                    doc["_pwd"] = _hash(v)
                else:
                    doc[k] = v
            doc["modified"] = _now()
            _derive(doctype, doc)
            _save(doctype, doc)
            return {"data": _public(doc)}
        if method == "DELETE":
            with _LOCK:
                _DB.execute("DELETE FROM doc WHERE doctype=? AND name=?", (doctype, str(name)))
                _DB.commit()
            return {"message": "ok"}
    raise RuntimeError(f"demo {method} {path} -> 405: unsupported")
