"""Payment ledger for CRM purchases.

Pure stdlib, no I/O, no Frappe: every money calculation of the Acme Leads lives here so it can
be unit-tested (tests_payments.py) and so the form, the lead card, the Sales Report and the
exports can never disagree. The server is the only place that adds money up; the browser only
displays what comes back.

A purchase (stored as JSON in CRM Lead.custom_purchase_details) is:
    items[]            {product, qty, price}            -> total_amount (derived, 2dp)
    payments[]         {id, amount, date, method, note, type, by, at, edited_by, edited_at}
    audit[]            append-only {at, by, action, detail}
Derived on EVERY read (never trusted from storage): received, balance, status.
    status = unpaid  (nothing received)
             partial (some received, balance > 0)
             paid    (balance == 0)           <- balance is "nil", no balance is shown
             unknown (legacy sale saved before payment tracking, payment never recorded;
                      never assumed to be 0 or full)

Money is held as Decimal dollars quantised to cents, never float.
"""
import datetime, uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

METHODS = ("Cash", "Card", "Bank transfer", "Other")
MAX_AMOUNT = Decimal("100000000")        # $100 million: anything above is a typo
_CENT = Decimal("0.01")


class PayError(ValueError):
    """User-facing validation message (shown as-is in the form)."""


def D(v, what="Amount"):
    """Any number/str -> Decimal dollars, 2dp. Rejects NaN/inf/garbage/negative-zero tricks."""
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise PayError(f"{what} is required")
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PayError(f"{what} must be a number")
    if not d.is_finite():
        raise PayError(f"{what} must be a number")
    if d.copy_abs() > MAX_AMOUNT:
        raise PayError(f"{what} is too large")
    d = d.quantize(_CENT, rounding=ROUND_HALF_UP)
    return d + Decimal("0")                # normalises -0.00 to 0.00


def to_float(d):
    """Decimal -> float dollars for JSON (exact to the cent for any realistic amount)."""
    return float(Decimal(d).quantize(_CENT))


def usd(d):
    d = Decimal(d).quantize(_CENT)
    s = f"{d:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def compute_items(raw_items):
    """-> (clean_items, total Decimal). Each line needs product + qty>0 (integer) + price>0."""
    items, total = [], Decimal("0")
    for it in (raw_items or []):
        name = str((it or {}).get("product") or "").strip()
        try:
            qty = int(it.get("qty"))
        except (TypeError, ValueError, AttributeError):
            qty = 0
        try:
            price = D(it.get("price"), "Price")
        except PayError:
            price = Decimal("0")
        if name and qty > 0 and price > 0:
            items.append({"product": name, "qty": qty, "price": to_float(price)})
            total += price * qty
    if not items:
        raise PayError("Buying date, and at least one product with quantity and price, are all required")
    return items, total.quantize(_CENT)


def _date(v, what="Payment date", default=None, allow_future=False):
    s = str(v or "").strip()[:10]
    if not s:
        if default:
            return default
        raise PayError(f"{what} is required")
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise PayError(f"{what} is not a valid date")
    if d > datetime.date.today() and not allow_future:
        raise PayError(f"{what} cannot be in the future")
    if d.year < 2020:
        raise PayError(f"{what} is not a valid date")
    return d.isoformat()


def _method(v):
    v = str(v or "").strip()
    if not v:
        return ""
    for m in METHODS:
        if v.lower() == m.lower():
            return m
    raise PayError("Payment method must be one of: " + ", ".join(METHODS))


def _now():
    return f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"


def legacy_payments(p):
    """Purchases saved before the ledger carry advance_paid. -> (payments|None, known).
    None/known=False = payment was never recorded (do NOT assume 0 or full)."""
    if isinstance(p.get("payments"), list):
        return list(p["payments"]), True
    adv = p.get("advance_paid")
    if adv is None or (isinstance(adv, str) and not adv.strip()):
        return [], False
    try:
        a = D(adv, "Advance")
    except PayError:
        return [], False
    if a <= 0:
        return [], True
    return [{"id": "legacy-advance", "amount": to_float(a), "date": str(p.get("buying_date") or "")[:10], "method": "",
             "note": "Advance (recorded before payment tracking)", "type": "advance",
             "by": p.get("recorded_by", ""), "at": p.get("recorded_at", ""), "legacy": True}], True


def view(p):
    """Derived money view of one purchase dict. Always recomputed, never read from storage."""
    pays, known = legacy_payments(p)
    try:
        total = D(p.get("total_amount") or 0, "Total")
    except PayError:
        total = Decimal("0")
    received = sum((D(x.get("amount") or 0) for x in pays), Decimal("0"))
    balance = max(total - received, Decimal("0"))
    if not known:
        status = "unknown"
    elif received <= 0:
        status = "unpaid"
    elif balance == 0:
        status = "paid"
    else:
        status = "partial"
    return {"payments": pays, "payments_known": known, "total": total, "received": received,
            "balance": balance, "status": status, "overpaid": received > total}


def out(p):
    """Purchase for the browser: stored fields + derived money (JSON-safe)."""
    v = view(p)
    o = {k: val for k, val in p.items() if k not in ("advance_paid", "balance_due", "payments", "audit")}
    o.update({"payments": v["payments"], "payments_known": v["payments_known"], "total_amount": to_float(v["total"]),
              "received": to_float(v["received"]) if v["payments_known"] else None,
              "balance": to_float(v["balance"]) if v["payments_known"] else None,
              "payment_status": v["status"], "overpaid": v["overpaid"]})
    return o


def _entry(amount, date, method, note, ptype, user):
    return {"id": uuid.uuid4().hex[:10], "amount": to_float(amount), "date": date, "method": method,
            "note": str(note or "").strip()[:200], "type": ptype, "by": user, "at": _now()}


def _stamp(p, user, action, detail=""):
    p.setdefault("audit", []).append({"at": _now(), "by": user, "action": action, "detail": str(detail)[:300]})
    del p["audit"][:-200]                  # keep the last 200 events


def _refresh(p):
    """Persist derived numbers next to the ledger (handy in Frappe list views / old readers)."""
    v = view(p)
    p["total_amount"] = to_float(v["total"])
    if v["payments_known"]:
        p["received"], p["balance_due"], p["payment_status"] = to_float(v["received"]), to_float(v["balance"]), v["status"]
    else:
        for k in ("received", "balance_due"):
            p.pop(k, None)
        p["payment_status"] = "unknown"
    p.pop("advance_paid", None)
    return p


def _sync_rows(p, pays, rows, user):
    """Apply the edit form's payment rows to the ledger (edit / add / delete), each change audited."""
    cur = {x.get("id"): x for x in pays}
    out_rows, seen = [], set()
    for i, r in enumerate(rows or [], 1):
        r = r or {}
        amt = D(r.get("amount"), f"Payment {i} amount")
        if amt <= 0:
            raise PayError(f"Payment {i}: amount must be more than 0 (remove the row to delete it)")
        date = _date(r.get("date"), f"Payment {i} date", default=datetime.date.today().isoformat())
        method = _method(r.get("method"))
        note = str(r.get("note") or "").strip()[:200]
        rid = r.get("id")
        if rid:
            if rid not in cur or rid in seen:
                raise PayError("A payment was changed by someone else - close this form and open it again")
            seen.add(rid)
            old = cur[rid]
            if D(old.get("amount")) != amt or str(old.get("date") or "")[:10] != date or (old.get("method") or "") != method or (old.get("note") or "") != note:
                new = dict(old)
                new.update({"amount": to_float(amt), "date": date, "method": method, "note": note, "edited_by": user, "edited_at": _now()})
                new.pop("legacy", None)
                _stamp(p, user, "payment_edited", f"{usd(D(old.get('amount')))} on {old.get('date')} -> {usd(amt)} on {date}")
                out_rows.append(new)
            else:
                out_rows.append(old)
        else:
            e = _entry(amt, date, method, note, "advance" if not out_rows and not seen else "balance", user)
            _stamp(p, user, "payment_added", f"{usd(amt)} on {date}")
            out_rows.append(e)
    for rid, old in cur.items():
        if rid not in seen:
            _stamp(p, user, "payment_deleted", f"{usd(D(old.get('amount')))} on {old.get('date')} (by {old.get('by', '?')})")
    return out_rows


def save_purchase(existing, buying_date, raw_items, pay, user, pay_rows=None):
    """Create or edit a purchase (items + date + first payment choice). -> new purchase dict.
    existing: stored dict or {}. pay: {"choice": full|advance|none, amount, date, method, note}
    -- required whenever payment is not yet known (new sale, or a legacy sale with no payment
    recorded); ignored once a ledger exists.
    pay_rows (ledger exists): the FULL list of payments as shown in the edit form
    [{id?, amount, date, method, note}] - rows with an id are edited, rows without are added,
    ids missing from the list are deleted (all audited). None = leave payments untouched."""
    existing = existing or {}
    bd = _date(buying_date, "Buying date", allow_future=True)
    items, total = compute_items(raw_items)
    pays, known = legacy_payments(existing)
    p = {k: v for k, v in existing.items()}
    if known:
        if pay_rows is not None:
            pays = _sync_rows(p, pays, pay_rows, user)
        received = sum((D(x.get("amount") or 0) for x in pays), Decimal("0"))
        if total < received:
            raise PayError(f"Net total (${usd(total)}) cannot be less than the payments already received "
                           f"(${usd(received)}). Edit or delete a payment first.")
        p["payments"] = pays
    else:
        pay = pay or {}
        choice = str(pay.get("choice") or "").strip().lower()
        if choice not in ("full", "advance", "none"):
            raise PayError("Choose the payment received: full payment, part payment (advance), or nothing yet")
        if choice == "none":
            p["payments"] = []
        else:
            date = _date(pay.get("date"), default=datetime.date.today().isoformat())
            if choice == "full":
                amt, ptype = total, "full"
            else:
                amt, ptype = D(pay.get("amount"), "Advance amount"), "advance"
                if amt <= 0:
                    raise PayError("Advance amount must be more than 0 (choose 'nothing yet' if none was received)")
                if amt > total:
                    raise PayError(f"Advance (${usd(amt)}) cannot be more than the net total (${usd(total)})")
                if amt == total:
                    ptype = "full"
            p["payments"] = [_entry(amt, date, _method(pay.get("method")), pay.get("note"), ptype, user)]
    p.update({"buying_date": bd, "items": items, "total_amount": to_float(total)})
    p.setdefault("recorded_by", user)
    p.setdefault("recorded_at", _now())
    if existing:
        p["updated_by"], p["updated_at"] = user, _now()
    _stamp(p, user, "purchase_edited" if existing else "purchase_created",
           f"total {usd(total)}, payments {len(p['payments'])}")
    return _refresh(p)


def _find(p, pid):
    pays, known = legacy_payments(p)
    if not known:
        raise PayError("No payments are recorded for this sale yet")
    for x in pays:
        if x.get("id") == pid:
            return pays, x
    raise PayError("Payment not found (it may already have been changed)")


def _ensure_ledger(p):
    """Turn a legacy advance_paid into a real ledger entry the first time a payment is touched."""
    pays, known = legacy_payments(p)
    if known and not isinstance(p.get("payments"), list):
        p["payments"] = pays
    return p


def add_payment(existing, raw, user):
    p = _ensure_ledger(dict(existing or {}))
    if not p.get("items"):
        raise PayError("This lead has no purchase recorded yet")
    pays, known = legacy_payments(p)
    if not known:
        p["payments"] = pays = []         # first payment on a legacy "unknown" sale also settles the unknown
    v = view(p)
    if raw.get("full") in (True, "true", 1, "1"):
        amt = v["balance"]
    else:
        amt = D(raw.get("amount"), "Amount")
    if amt <= 0:
        if v["balance"] == 0:
            raise PayError("This sale is already paid in full - nothing is due")
        raise PayError("Amount must be more than 0")
    if amt > v["balance"]:
        raise PayError(f"Amount (${usd(amt)}) is more than the balance due (${usd(v['balance'])})")
    date = _date(raw.get("date"), default=datetime.date.today().isoformat())
    if v["received"] > 0:
        ptype = "balance"
    else:
        ptype = "full" if amt == v["total"] else "advance"
    p["payments"] = pays + [_entry(amt, date, _method(raw.get("method")), raw.get("note"), ptype, user)]
    _stamp(p, user, "payment_added", f"{usd(amt)} on {date}")
    return _refresh(p)


def edit_payment(existing, pid, raw, user):
    p = _ensure_ledger(dict(existing or {}))
    pays, x = _find(p, pid)
    total = D(p.get("total_amount") or 0)
    others = sum((D(y.get("amount") or 0) for y in pays if y is not x), Decimal("0"))
    amt = D(raw.get("amount"), "Amount") if "amount" in raw else D(x.get("amount"))
    if amt <= 0:
        raise PayError("Amount must be more than 0 (use delete to remove a payment)")
    if others + amt > total:
        raise PayError(f"Amount (${usd(amt)}) would make the total received ${usd(others + amt)}, more than "
                       f"the net total ${usd(total)}. Maximum for this payment: ${usd(total - others)}")
    before = f"{usd(D(x.get('amount')))} on {x.get('date')}"
    new = dict(x)
    new.update({"amount": to_float(amt),
                "date": _date(raw.get("date"), default=x.get("date") or datetime.date.today().isoformat()) if "date" in raw else x.get("date"),
                "method": _method(raw.get("method")) if "method" in raw else x.get("method", ""),
                "note": str(raw.get("note") if "note" in raw else x.get("note") or "").strip()[:200],
                "edited_by": user, "edited_at": _now()})
    new.pop("legacy", None)
    p["payments"] = [new if y is x else y for y in pays]
    _stamp(p, user, "payment_edited", f"{before} -> {usd(amt)} on {new['date']}")
    return _refresh(p)


def delete_payment(existing, pid, user):
    p = _ensure_ledger(dict(existing or {}))
    pays, x = _find(p, pid)
    p["payments"] = [y for y in pays if y is not x]
    _stamp(p, user, "payment_deleted", f"{usd(D(x.get('amount')))} on {x.get('date')} (by {x.get('by', '?')})")
    return _refresh(p)


# ---------------------------------------------------------------------------
# Multiple purchases per lead: a client who buys again months later gets
# a NEW purchase on the SAME lead instead of overwriting the first one. custom_purchase_details
# now holds {"purchases": [purchase, ...], "removed": [...]}. A pre-existing single-purchase
# record (a bare purchase dict) is read as a one-item list with id "p1" - nothing is migrated
# in storage until that lead is saved again, and every old record keeps working.
# Every function above still works on ONE purchase dict; this layer only picks which one.
# ---------------------------------------------------------------------------
import json as _json


def _parse(raw):
    if isinstance(raw, (str, bytes)):
        try:
            raw = _json.loads(raw or "null")
        except ValueError:
            return None
    return raw


def purchases_of(raw):
    """Stored custom_purchase_details (str/dict/None) -> list of purchase dicts, each with an `id`."""
    d = _parse(raw)
    if not isinstance(d, dict) or not d:
        return []
    if isinstance(d.get("purchases"), list):
        lst = [dict(x) for x in d["purchases"] if isinstance(x, dict)]
    else:
        lst = [dict(d)]                      # legacy single purchase
    for i, p in enumerate(lst, 1):
        p.setdefault("id", f"p{i}")
    return lst


def removed_of(raw):
    d = _parse(raw)
    return list(d.get("removed") or []) if isinstance(d, dict) else []


def pack(purchases, removed=None):
    o = {"purchases": purchases}
    if removed:
        o["removed"] = removed
    return o


def pick(purchases, pid):
    """Which purchase an edit/payment call means. pid given -> exactly that one. No pid: allowed only
    when the lead has exactly one purchase (older browser tabs); with several the caller must say which."""
    if pid:
        for p in purchases:
            if p.get("id") == pid:
                return p
        raise PayError("That purchase was not found - close this form and open the lead again")
    if len(purchases) == 1:
        return purchases[0]
    if not purchases:
        return None
    raise PayError("This client has more than one purchase - open the one you want to change")


def new_id(purchases):
    used = {p.get("id") for p in purchases}
    while True:
        i = "p" + uuid.uuid4().hex[:7]
        if i not in used:
            return i


def credit_of(p, lead_owner):
    """Sales person a purchase counts for: whoever entered it (credit_to), or - for purchases saved
    before this rule existed - the lead's owner exactly as before."""
    return (p.get("credit_to") or lead_owner or "").strip()


def with_total(purchases):
    """Sum of every purchase's net total (for the marketing dashboard)."""
    t = Decimal("0")
    for p in purchases:
        t += view(p)["total"]
    return to_float(t)
