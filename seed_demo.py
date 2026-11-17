#!/usr/bin/env python3
"""Seed the demo database with entirely invented data (fixed RNG seed -> same data every run).

Everything here is fictional: people, phone numbers (555 range), @example.com addresses,
cities, products, notes and amounts. Dates are generated relative to today so the dashboards
always look current.

    python3 seed_demo.py --reset     wipe and re-seed data/demo.db
(The app also calls seed() automatically the first time it starts in demo mode.)
"""
import datetime, json, os, random, sys

import payments

PASSWORD = "demo1234"          # demo logins only - every demo user shares it
USERS = [  # (email, full name, roles)
    ("admin@example.com", "Demo Admin", ["System Manager", "Sales Manager"]),
    ("alice@example.com", "Alice Demo", ["Sales User"]),
    ("bob@example.com", "Bob Demo", ["Sales User"]),
    ("carol@example.com", "Carol Demo", ["Sales User"]),
    ("service-bot@example.com", "Service Bot", ["Sales User"]),      # service account (hidden from team views)
]
REPS = ["alice@example.com", "bob@example.com", "carol@example.com"]

FIRST = ["Avery", "Blair", "Casey", "Dana", "Eli", "Frankie", "Gray", "Harper", "Indigo", "Jules", "Kit", "Lane",
         "Morgan", "Nico", "Oakley", "Parker", "Quinn", "Riley", "Sage", "Tatum", "Uma", "Vale", "Wren", "Xan",
         "Yael", "Zion", "Arden", "Briar", "Cleo", "Drew"]
LAST = ["Alder", "Birch", "Cedar", "Dunmore", "Elmsworth", "Fairchild", "Greystone", "Hollis", "Ironwood", "Juniper",
        "Kingsley", "Larkspur", "Marlowe", "Northcott", "Oakhurst", "Pennywhistle", "Quillfeather", "Rookwood",
        "Stillwater", "Thornbury", "Underhill", "Vanterpool", "Willowby", "Yarrow", "Ashgrove"]
CITIES = ["Springfield", "Riverton", "Lakeside", "Maple Grove", "Fairview", "Oakdale", "Brookfield", "Ashford",
          "Westbury", "Northgate"]
PRODUCTS = [("Starter Plan", 49), ("Pro Plan", 199), ("Widget A100", 120), ("Widget A200", 240),
            ("Widget A300", 390), ("Sensor Pack S7", 89), ("Connector Kit C12", 26),
            ("Consulting Hour", 150), ("Extended Care Plan", 45)]
SOURCES = [("Website", 40), ("Meta Lead Ads", 14), ("AI Chat", 10), ("Google Ads", 8),
           ("Existing Customer", 8), ("Reference", 8), ("Cold Calling", 6), ("Walk In", 6)]
STATUSES = [("New", 18), ("Contacted", 15), ("Interested", 12), ("Follow Up", 12), ("Not Available", 8),
            ("Not Interested", 8), ("Junk", 6), ("Converted", 12), ("Qualified", 3), ("Nurture", 4),
            ("Unqualified", 2)]
CHANNELS = ["ChatGPT", "Gemini", "Google", "Instagram", "Facebook", "Twitter / X", "Friends & Family"]
NOTES = ["Asked for a quote, will revert after checking with the team.", "Phone was busy, try again tomorrow.",
         "Wants a demo next week.", "Comparing us with another vendor, price is the concern.",
         "Needs 3 units, delivery within two weeks.", "Sent the product sheet on chat.",
         "Said to call after the weekend.", "Budget approved, waiting on purchase order.",
         "Not the decision maker, asked for the manager's contact.", "Interested in the extended care plan too."]
REQS = ["Looking for {p}, quantity {q}.", "Need a quote for {p}.", "Is {p} available? Need {q} units.",
        "Interested in {p} for our office.", "Want to compare {p} with the bundle."]
BUY_PRICE = {n: p for n, p in PRODUCTS}


def _wpick(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def seed(store):
    """Populate `store` (the demo_store module) with invented users, leads, tasks, notes and sales."""
    rng = random.Random(20260101)
    now = datetime.datetime.now().replace(microsecond=0)
    today = now.date()

    for email, name, roles in USERS:
        store.insert("User", {"email": email, "first_name": name, "new_password": PASSWORD,
                              "roles": [{"role": r} for r in roles]})

    converted = []
    for n in range(1, 141):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        status = _wpick(rng, STATUSES)
        source = _wpick(rng, SOURCES)
        age = int(rng.triangular(0, 150, 20))
        created = now - datetime.timedelta(days=age, hours=rng.randint(0, 9), minutes=rng.randint(0, 59))
        owner = rng.choice(REPS)
        prod, qty = rng.choice(PRODUCTS)[0], rng.randint(1, 4)
        doc = {
            "first_name": f"{first} {last}", "last_name": "", "status": status, "source": source,
            "custom_brand": "Acme Pro" if rng.random() < 0.2 else "Acme",
            "mobile_no": f"+1 555 010 {n:04d}",
            "email": f"{first}.{last}{n}@example.com".lower() if rng.random() < 0.8 else "",
            "custom_billing_city": rng.choice(CITIES), "lead_owner": owner,
            "custom_product_model": prod if rng.random() < 0.6 else "",
            "custom_requirement": rng.choice(REQS).format(p=prod, q=qty),
            "custom_budget": str(rng.choice([500, 1000, 2500, 5000, 10000])) if rng.random() < 0.3 else "",
            "custom_source_history": f"demo.web#{n}",
        }
        if source in ("Website", "Meta Lead Ads") and rng.random() < 0.65:
            r = rng.random()
            doc["custom_referral"] = (rng.choice(CHANNELS) if r < 0.85 else "Others: " + rng.choice(["Trade show", "Newsletter", "Podcast"]))
        if status != "New":
            lc_age = rng.randint(0, max(1, min(age, 30)))
            if rng.random() < 0.12:
                lc_age = 0
            doc["custom_last_contacted"] = (today - datetime.timedelta(days=lc_age)).isoformat()
        if status in ("Not Interested", "Junk", "Unqualified"):
            doc["lost_reason"] = {"Junk": "Other", "Unqualified": "Poor Fit"}.get(status, "Poor Fit")
        stamp = created.strftime("%Y-%m-%d %H:%M:%S.000000")
        doc["creation"] = stamp
        doc["modified"] = (created + datetime.timedelta(days=rng.randint(0, max(0, age)))).strftime("%Y-%m-%d %H:%M:%S.000000")
        lead = store.insert("CRM Lead", doc, keep_times=True)
        if status == "Converted":
            converted.append((lead, created, age))

        # follow-up tasks (same cadence idea as app.py: closed history + at most one open task)
        if status in ("New", "Contacted", "Interested", "Follow Up", "Not Available"):
            for k in range(rng.randint(0, 2)):
                due = created.date() + datetime.timedelta(days=2 * (k + 1))
                store.insert("CRM Task", {"title": f"Follow up with {doc['first_name']}", "status": "Done",
                                          "priority": "Medium", "reference_doctype": "CRM Lead",
                                          "reference_docname": lead["name"],
                                          "due_date": f"{due} 11:00:00"}, keep_times=True)
            if rng.random() < 0.8:
                due = today + datetime.timedelta(days=rng.choice([-3, -2, -1, 0, 0, 0, 1, 2, 4]))
                store.insert("CRM Task", {"title": f"Follow up with {doc['first_name']}", "status": "Todo",
                                          "priority": rng.choice(["High", "Medium", "Low"]),
                                          "reference_doctype": "CRM Lead", "reference_docname": lead["name"],
                                          "due_date": f"{due} 11:00:00"}, keep_times=True)

        for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
            when = created + datetime.timedelta(days=rng.randint(0, max(0, age)), hours=1)
            store.insert("Comment", {"comment_type": "Comment", "reference_doctype": "CRM Lead",
                                     "reference_name": lead["name"], "content": rng.choice(NOTES),
                                     "comment_email": owner, "comment_by": owner,
                                     "creation": when.strftime("%Y-%m-%d %H:%M:%S.000000")}, keep_times=True)

    # sales: purchases on Converted leads (ledger built with the real payments module)
    for i, (lead, created, age) in enumerate(converted):
        owner = lead["lead_owner"]
        buys = 2 if i % 5 == 0 else 1
        purchases = []
        for b in range(buys):
            bd = created.date() + datetime.timedelta(days=rng.randint(1, 10) + b * rng.randint(20, 40))
            bd = min(bd, today)
            items = []
            for _ in range(rng.randint(1, 3)):
                name = rng.choice(PRODUCTS)[0]
                items.append({"product": name, "qty": rng.randint(1, 3), "price": BUY_PRICE[name]})
            choice = rng.choice(["full", "full", "advance", "none"])
            total = sum(x["qty"] * x["price"] for x in items)
            pay = {"choice": choice, "date": bd.isoformat(), "method": rng.choice(["Cash", "Card", "Bank transfer"])}
            if choice == "advance":
                pay["amount"] = round(total * rng.choice([0.3, 0.5, 0.7]))
            p = payments.save_purchase({}, bd.isoformat(), items, pay, owner)
            p["id"] = payments.new_id(purchases)
            p["credit_to"] = owner
            if choice == "advance" and rng.random() < 0.4:      # a later balance payment
                later = min(today, bd + datetime.timedelta(days=rng.randint(3, 15)))
                p = payments.add_payment(p, {"full": True, "date": later.isoformat(), "method": "Card"}, owner)
            purchases.append(p)
        store.handle("PUT", f"/api/resource/CRM Lead/{lead['name']}",
                     body={"custom_purchase_details": json.dumps(payments.pack(purchases))})


if __name__ == "__main__":
    import demo_store
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.environ.get("DATA_DIR", os.path.join(here, "data"))
    os.makedirs(data_dir, exist_ok=True)
    demo_store.init(os.path.join(data_dir, "demo.db"))
    if "--reset" in sys.argv:
        demo_store.reset()
        seed(demo_store)
        print("demo database re-seeded in", data_dir)
    else:
        print("demo database ready in", data_dir, "(use --reset to wipe and re-seed)")
