#!/usr/bin/env python3
"""Fix CRM leads whose lead_name is actually the enquiry text.

Import artefact: the website DB put the customer's message in the name column, so
the CRM shows leads called "Do you have this in stock?" or "Widget A200 available".

For each such lead: derive a usable name (from the email, else the phone), and make
sure the original text survives inside custom_requirement. The original name is
written to a rollback file so the whole run can be undone.

  fix_names.py --dry-run     show what would change, write nothing   (default)
  fix_names.py --apply       do it, writing rollback_names.json
  fix_names.py --rollback rollback_names.json
"""
import json, os, re, sys, urllib.parse, urllib.request, datetime

CRM = os.environ.get("CRM_URL", "http://localhost:8080").rstrip("/")
AUTH = os.environ.get("CRM_TOKEN", "")      # "token <key>:<secret>" - never hard-code it
ROLLBACK = "rollback_names.json"

GENERIC_LOCALPARTS = {"info", "sales", "admin", "contact", "support", "enquiry",
                      "enquiries", "mail", "office", "accounts", "purchase", "hr", "team"}
# words that mean the "name" is really an enquiry
INTENT = re.compile(r"\b(need|needs|want|wants|looking|require|required|price|prices|pricing|quote|sell|selling|buy|buying|available|availability|inquiry|enquiry|widget|widgets|gadget|bundle|kit|sensor|specification|specifications|warranty)\b", re.I)
# 2+ plain alphabetic words, short: that is a person's name, never touch it
PERSONISH = re.compile(r"^[A-Za-z][A-Za-z.'\-]{0,14}(\s+[A-Za-z][A-Za-z.'\-]{0,14}){0,2}$")


def api(method, path, params=None, body=None):
    url = CRM + urllib.parse.quote(path, safe="/%")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", AUTH)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def looks_like_enquiry(name):
    n = (name or "").strip()
    if not n:
        return False
    if "?" in n:
        return True
    # a short, plain, alphabetic 1-3 word string with no intent word is a person
    if PERSONISH.match(n) and not INTENT.search(n):
        return False
    if len(n) > 28 or len(n.split()) >= 4:
        return True
    if re.search(r"\d", n) and len(n.split()) >= 2:
        return True
    if INTENT.search(n):
        return True
    return False


def derive_name(lead):
    """A usable display name: prefer the email's local part, else the phone tail."""
    email = (lead.get("email") or "").strip().lower()
    local = email.split("@")[0] if "@" in email else ""
    cleaned = re.sub(r"[._\-]+", " ", re.sub(r"\d+", "", local)).strip()
    words = cleaned.split()
    ok = (cleaned and cleaned.lower() not in GENERIC_LOCALPARTS
          and all(w.lower() not in GENERIC_LOCALPARTS for w in words)
          # one long run-together handle ("jdoeacmewidgets") is not a name
          and (len(words) > 1 or len(cleaned) <= 12)
          and len(cleaned) <= 26 and cleaned.replace(" ", "").isalpha())
    if ok:
        return " ".join(w.capitalize() for w in words)
    phone = re.sub(r"\D", "", lead.get("mobile_no") or "")
    if len(phone) >= 4:
        return f"Lead {phone[-4:]}"
    return "Web Lead"


def main():
    apply = "--apply" in sys.argv
    if "--rollback" in sys.argv:
        path = sys.argv[sys.argv.index("--rollback") + 1]
        undo = json.load(open(path))
        for r in undo:
            api("PUT", f"/api/resource/CRM Lead/{r['id']}",
                body={"lead_name": r["old_name"], "first_name": r["old_first"],
                      "custom_requirement": r["old_req"]})
        print(f"rolled back {len(undo)} leads")
        return

    fields = ["name", "lead_name", "first_name", "email", "mobile_no",
              "custom_requirement", "custom_source_history"]
    leads = api("GET", "/api/resource/CRM Lead",
                {"fields": json.dumps(fields), "limit_page_length": 0})["data"]
    targets = [l for l in leads if looks_like_enquiry(l.get("lead_name"))]
    print(f"{len(leads)} leads scanned, {len(targets)} look like enquiry text\n")

    plan, rollback = [], []
    for l in targets:
        old = (l.get("lead_name") or "").strip()
        new = derive_name(l)
        req = (l.get("custom_requirement") or "")
        # keep the enquiry text: only add it if it isn't already in the requirement
        if old.lower() not in req.lower():
            req = (f"Enquiry: {old}\n" + req).strip()
        plan.append((l["name"], old, new, len(req)))
        rollback.append({"id": l["name"], "old_name": old,
                         "old_first": l.get("first_name") or "",
                         "old_req": l.get("custom_requirement") or "",
                         "new_name": new, "new_req": req})

    for lid, old, new, _ in plan[:15]:
        print(f"  {lid}  {old[:44]:<46} ->  {new}")
    if len(plan) > 15:
        print(f"  ... and {len(plan)-15} more")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to make the changes.")
        return

    json.dump(rollback, open(ROLLBACK, "w"), indent=1)
    print(f"\nrollback file written: {ROLLBACK}")
    ok = fail = 0
    for r in rollback:
        try:
            hist = f"\nName cleaned {datetime.date.today()} (was: {r['old_name'][:80]})"
            api("PUT", f"/api/resource/CRM Lead/{r['id']}",
                body={"lead_name": r["new_name"], "first_name": r["new_name"].split()[0],
                      "custom_requirement": r["new_req"][:2000]})
            ok += 1
        except Exception as e:
            fail += 1
            print("  FAILED", r["id"], str(e)[:120])
    print(f"updated {ok}, failed {fail}")


if __name__ == "__main__":
    main()
