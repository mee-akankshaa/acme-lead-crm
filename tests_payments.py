"""Unit tests for payments.py - run:  python3 -m unittest tests_payments -v"""
import datetime, random, unittest
from decimal import Decimal as Dx
import payments as P

TODAY = datetime.date.today().isoformat()
ITEMS = [{"product": "Widget A200", "qty": 2, "price": 10000}, {"product": "Accessory Kit", "qty": 1, "price": 2500.5}]  # 22500.50
U = "rep@example.com"


def new(choice, **kw):
    return P.save_purchase({}, TODAY, ITEMS, dict(choice=choice, **kw), U)


class Cases(unittest.TestCase):
    # ---- Case 1: paid in one go, never an advance -> balance must be NIL
    def test_full_payment_balance_nil(self):
        v = P.out(new("full"))
        self.assertEqual((v["total_amount"], v["received"], v["balance"], v["payment_status"]), (22500.5, 22500.5, 0, "paid"))
        self.assertEqual(len(v["payments"]), 1)
        self.assertEqual(v["payments"][0]["type"], "full")

    def test_nothing_received_is_unpaid_not_unknown(self):
        v = P.out(new("none"))
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (0, 22500.5, "unpaid"))
        self.assertTrue(v["payments_known"])

    # ---- Case 2: advance, then balance comes later
    def test_advance_then_balance(self):
        p = new("advance", amount=5000)
        v = P.out(p)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (5000, 17500.5, "partial"))
        p = P.add_payment(p, {"full": True}, U)                      # clear the rest with one click
        v = P.out(p)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (22500.5, 0, "paid"))
        self.assertEqual([x["type"] for x in v["payments"]], ["advance", "balance"])

    def test_three_instalments(self):
        p = new("advance", amount=1000)
        p = P.add_payment(p, {"amount": 2000.25}, U)
        p = P.add_payment(p, {"amount": 19500.25}, U)
        v = P.out(p)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (22500.5, 0, "paid"))

    def test_advance_equal_to_total_is_full(self):
        v = P.out(new("advance", amount=22500.5))
        self.assertEqual((v["balance"], v["payment_status"], v["payments"][0]["type"]), (0, "paid", "full"))

    # ---- guards
    def test_choice_is_mandatory_for_new_sale(self):
        for pay in (None, {}, {"choice": ""}, {"choice": "maybe"}):
            with self.assertRaises(P.PayError):
                P.save_purchase({}, TODAY, ITEMS, pay, U)

    def test_advance_validation(self):
        for amt in (0, -5, 22500.51, "abc", "", None, float("nan"), float("inf"), "1e999"):
            with self.assertRaises(P.PayError, msg=repr(amt)):
                new("advance", amount=amt)

    def test_overpay_blocked(self):
        p = new("advance", amount=20000)
        with self.assertRaises(P.PayError):
            P.add_payment(p, {"amount": 2500.51}, U)
        p = P.add_payment(p, {"amount": 2500.5}, U)
        with self.assertRaises(P.PayError):                          # already paid
            P.add_payment(p, {"amount": 1}, U)
        with self.assertRaises(P.PayError):
            P.add_payment(p, {"full": True}, U)

    def test_bad_inputs(self):
        p = new("advance", amount=100)
        for raw in ({"amount": 0}, {"amount": -1}, {"amount": ""}, {"amount": "x"}, {}, {"amount": 5, "date": "2999-01-01"},
                    {"amount": 5, "date": "garbage"}, {"amount": 5, "method": "Bitcoin"}):
            with self.assertRaises(P.PayError, msg=repr(raw)):
                P.add_payment(p, raw, U)

    def test_no_future_or_ancient_payment_date(self):
        with self.assertRaises(P.PayError):
            new("full", date="2999-01-01")
        with self.assertRaises(P.PayError):
            new("full", date="1999-01-01")

    def test_items_validation(self):
        for items in ([], None, [{"product": "", "qty": 1, "price": 5}], [{"product": "a", "qty": 0, "price": 5}],
                      [{"product": "a", "qty": 1, "price": 0}], [{"product": "a", "qty": 1.5, "price": "x"}]):
            with self.assertRaises(P.PayError):
                P.save_purchase({}, TODAY, items, {"choice": "none"}, U)

    # ---- editing money after the fact
    def test_total_cannot_drop_below_received(self):
        p = new("advance", amount=20000)
        with self.assertRaises(P.PayError):
            P.save_purchase(p, TODAY, [{"product": "x", "qty": 1, "price": 19999}], None, U)
        q = P.save_purchase(p, TODAY, [{"product": "x", "qty": 1, "price": 20000}], None, U)   # exactly equal is fine
        self.assertEqual(P.out(q)["payment_status"], "paid")

    def test_total_increase_reopens_a_paid_sale(self):
        p = new("full")
        q = P.save_purchase(p, TODAY, ITEMS + [{"product": "Rails", "qty": 1, "price": 500}], None, U)
        v = P.out(q)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (22500.5, 500, "partial"))

    def test_edit_keeps_payments_ignores_pay_block(self):
        p = new("advance", amount=1000)
        q = P.save_purchase(p, TODAY, ITEMS, {"choice": "full"}, "other@example.com")     # pay block ignored once a ledger exists
        self.assertEqual(P.out(q)["received"], 1000)
        self.assertEqual(q["recorded_by"], U)
        self.assertEqual(q["updated_by"], "other@example.com")

    def test_edit_payment(self):
        p = new("advance", amount=1000)
        pid = p["payments"][0]["id"]
        p = P.edit_payment(p, pid, {"amount": 3000, "method": "card", "note": "fixed typo"}, "adm@example.com")
        x = P.out(p)["payments"][0]
        self.assertEqual((x["amount"], x["method"], x["edited_by"]), (3000, "Card", "adm@example.com"))
        with self.assertRaises(P.PayError):
            P.edit_payment(p, pid, {"amount": 22500.51}, U)
        with self.assertRaises(P.PayError):
            P.edit_payment(p, pid, {"amount": 0}, U)
        with self.assertRaises(P.PayError):
            P.edit_payment(p, "nope", {"amount": 5}, U)

    def test_edit_cannot_overflow_with_other_payments(self):
        p = new("advance", amount=10000)
        p = P.add_payment(p, {"amount": 5000}, U)
        first = p["payments"][0]["id"]
        with self.assertRaises(P.PayError):
            P.edit_payment(p, first, {"amount": 17500.51}, U)
        p = P.edit_payment(p, first, {"amount": 17500.5}, U)
        self.assertEqual(P.out(p)["payment_status"], "paid")

    def test_delete_payment_reopens_balance_and_is_audited(self):
        p = new("full")
        p = P.delete_payment(p, p["payments"][0]["id"], "adm@example.com")
        v = P.out(p)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (0, 22500.5, "unpaid"))
        self.assertTrue(v["payments_known"])                         # deleted == unpaid, never "unknown"
        self.assertEqual([a["action"] for a in p["audit"]][-1], "payment_deleted")
        with self.assertRaises(P.PayError):
            P.delete_payment(p, "gone", U)

    # ---- legacy rows (saved by the old Advance Paid form)
    def test_legacy_advance_becomes_ledger(self):
        old = {"buying_date": "2026-10-10", "items": ITEMS, "total_amount": 22500.5, "advance_paid": 5000.0, "balance_due": 17500.5,
               "recorded_by": "a@example.com", "recorded_at": "2026-10-10 10:00:00"}
        v = P.out(old)
        self.assertEqual((v["received"], v["balance"], v["payment_status"], len(v["payments"])), (5000, 17500.5, "partial", 1))
        q = P.add_payment(old, {"full": True}, U)
        v = P.out(q)
        self.assertEqual((v["received"], v["balance"], v["payment_status"]), (22500.5, 0, "paid"))
        self.assertNotIn("advance_paid", q)
        self.assertEqual(len(v["payments"]), 2)

    def test_legacy_equal_to_total_is_paid(self):
        old = {"buying_date": "2026-10-10", "items": ITEMS, "total_amount": 22500.5, "advance_paid": 22500.5}
        self.assertEqual(P.out(old)["payment_status"], "paid")

    def test_legacy_without_advance_is_unknown_not_zero_not_full(self):
        old = {"buying_date": "2026-10-10", "items": ITEMS, "total_amount": 22500.5}
        v = P.out(old)
        self.assertEqual((v["payment_status"], v["received"], v["balance"], v["payments_known"]), ("unknown", None, None, False))
        with self.assertRaises(P.PayError):                          # editing it must ask for the payment choice
            P.save_purchase(old, "2026-10-10", ITEMS, None, U)
        q = P.save_purchase(old, "2026-10-10", ITEMS, {"choice": "full", "date": "2026-10-11"}, U)
        self.assertEqual(P.out(q)["payment_status"], "paid")
        # first payment on an unknown sale settles it as known
        r = P.add_payment(old, {"amount": 100}, U)
        self.assertEqual(P.out(r)["payment_status"], "partial")

    def test_legacy_advance_zero_is_unpaid(self):
        old = {"buying_date": "2026-10-10", "items": ITEMS, "total_amount": 22500.5, "advance_paid": 0}
        self.assertEqual(P.out(old)["payment_status"], "unpaid")

    # ---- editing payments straight from the purchase edit form
    def test_form_edit_amount_date_method(self):
        p = new("advance", amount=1000)
        pid = p["payments"][0]["id"]
        q = P.save_purchase(p, TODAY, ITEMS, None, "adm@example.com", pay_rows=[{"id": pid, "amount": 4000, "date": TODAY, "method": "cash"}])
        v = P.out(q)
        self.assertEqual((v["received"], v["balance"], v["payments"][0]["method"], v["payments"][0]["edited_by"]), (4000, 18500.5, "Cash", "adm@example.com"))

    def test_form_add_and_delete_rows(self):
        p = new("advance", amount=1000)
        pid = p["payments"][0]["id"]
        q = P.save_purchase(p, TODAY, ITEMS, None, U, pay_rows=[{"id": pid, "amount": 1000, "date": TODAY}, {"amount": 21500.5, "date": TODAY, "method": "Card"}])
        v = P.out(q)
        self.assertEqual((v["received"], v["balance"], v["payment_status"], len(v["payments"])), (22500.5, 0, "paid", 2))
        q = P.save_purchase(q, TODAY, ITEMS, None, U, pay_rows=[])      # remove every row -> unpaid (known), not unknown
        v = P.out(q)
        self.assertEqual((v["received"], v["payment_status"], v["payments_known"]), (0, "unpaid", True))
        self.assertIn("payment_deleted", [a["action"] for a in q["audit"]])

    def test_form_rows_validation(self):
        p = new("advance", amount=1000)
        pid = p["payments"][0]["id"]
        for rows in ([{"id": pid, "amount": 22500.51}], [{"id": pid, "amount": 0}], [{"id": pid, "amount": ""}], [{"id": "ghost", "amount": 5}],
                     [{"id": pid, "amount": 5}, {"id": pid, "amount": 5}], [{"amount": 22000}, {"id": pid, "amount": 1000}], [{"id": pid, "amount": 5, "date": "2999-01-01"}]):
            with self.assertRaises(P.PayError, msg=repr(rows)):
                P.save_purchase(p, TODAY, ITEMS, None, U, pay_rows=rows)

    def test_form_total_and_rows_together(self):
        p = new("full")                                                  # 22500.5 paid
        pid = p["payments"][0]["id"]
        q = P.save_purchase(p, TODAY, [{"product": "x", "qty": 1, "price": 10000}], None, U, pay_rows=[{"id": pid, "amount": 10000, "date": TODAY}])
        self.assertEqual((P.out(q)["total_amount"], P.out(q)["payment_status"]), (10000, "paid"))   # lower the total AND the payment in one save

    def test_form_rows_on_legacy_advance(self):
        old = {"buying_date": "2026-10-10", "items": ITEMS, "total_amount": 22500.5, "advance_paid": 5000}
        q = P.save_purchase(old, "2026-10-10", ITEMS, None, U, pay_rows=[{"id": "legacy-advance", "amount": 7000, "date": "2026-10-10"}])
        self.assertEqual((P.out(q)["received"], P.out(q)["balance"]), (7000, 15500.5))
        self.assertNotIn("advance_paid", q)

    # ---- money precision
    def test_cents_exact(self):
        items = [{"product": "a", "qty": 3, "price": 0.1}, {"product": "b", "qty": 1, "price": 0.2}]
        p = P.save_purchase({}, TODAY, items, {"choice": "advance", "amount": 0.3}, U)
        self.assertEqual(P.out(p)["total_amount"], 0.5)
        p = P.add_payment(p, {"amount": 0.2}, U)
        self.assertEqual((P.out(p)["balance"], P.out(p)["payment_status"]), (0, "paid"))   # 0.1*3+0.2 vs 0.3+0.2 in float would drift

    def test_random_ledgers_never_break_invariants(self):
        rnd = random.Random(7)
        for _ in range(300):
            items = [{"product": "p%d" % i, "qty": rnd.randint(1, 5), "price": round(rnd.uniform(1, 50000), 2)} for i in range(rnd.randint(1, 4))]
            total = sum(Dx(str(i["price"])) * i["qty"] for i in items)
            p = P.save_purchase({}, TODAY, items, {"choice": rnd.choice(["none", "advance", "full"]), "amount": float(total) * rnd.random() or 1}, U)
            for _ in range(rnd.randint(0, 8)):
                v = P.view(p)
                op = rnd.choice(["add", "edit", "del", "total"])
                try:
                    if op == "add":
                        p = P.add_payment(p, {"amount": float(v["balance"]) * rnd.random() + 0.01}, U)
                    elif op == "edit" and v["payments"]:
                        p = P.edit_payment(p, rnd.choice(v["payments"])["id"], {"amount": rnd.uniform(0.01, float(total))}, U)
                    elif op == "del" and v["payments"]:
                        p = P.delete_payment(p, rnd.choice(v["payments"])["id"], U)
                    elif op == "total":
                        p = P.save_purchase(p, TODAY, items, None, U)
                except P.PayError:
                    pass
                w = P.view(p)
                self.assertEqual(w["received"] + w["balance"], max(w["total"], w["received"]))
                self.assertGreaterEqual(w["balance"], 0)
                self.assertFalse(w["overpaid"])
                self.assertEqual(w["status"] == "paid", w["balance"] == 0 and w["received"] > 0)
                self.assertEqual(w["status"] == "unpaid", w["received"] == 0)


if __name__ == "__main__":
    unittest.main()


class MultiPurchase(unittest.TestCase):
    """Repeat buyer: several purchases on one lead."""

    def test_legacy_single_dict_reads_as_one_purchase(self):
        old = new("full")                                  # bare purchase dict, no id (old storage)
        lst = P.purchases_of(__import__("json").dumps(old))
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], "p1")
        self.assertEqual(P.view(lst[0])["total"], Dx("22500.50"))

    def test_empty_and_garbage(self):
        for raw in (None, "", "null", "{}", "not json", "[]", 5):
            self.assertEqual(P.purchases_of(raw), [], raw)

    def test_pack_roundtrip_and_ids_stable(self):
        a = new("full"); a["id"] = "p1"
        b = P.save_purchase({}, TODAY, ITEMS, {"choice": "none"}, U); b["id"] = P.new_id([a])
        raw = __import__("json").dumps(P.pack([a, b]))
        lst = P.purchases_of(raw)
        self.assertEqual([x["id"] for x in lst], ["p1", b["id"]])
        self.assertNotEqual(a["id"], b["id"])

    def test_pick_rules(self):
        a = {"id": "p1"}; b = {"id": "p2"}
        self.assertIs(P.pick([a], None), a)                # single purchase, old tab: fine
        self.assertIs(P.pick([a, b], "p2"), b)
        self.assertIsNone(P.pick([], None))
        with self.assertRaises(P.PayError): P.pick([a, b], None)      # ambiguous
        with self.assertRaises(P.PayError): P.pick([a, b], "zzz")     # unknown id

    def test_each_purchase_has_independent_payments(self):
        a = new("full"); a["id"] = "p1"
        b = P.save_purchase({}, TODAY, ITEMS, {"choice": "advance", "amount": 1000}, U); b["id"] = "p2"
        b2 = P.add_payment(b, {"amount": 500}, U)
        self.assertEqual(P.view(a)["received"], Dx("22500.50"))
        self.assertEqual(P.view(b2)["received"], Dx("1500"))
        self.assertEqual(P.view(a)["status"], "paid")
        self.assertEqual(P.view(b2)["status"], "partial")
        self.assertEqual(P.with_total([a, b2]), 45001.0)

    def test_credit(self):
        self.assertEqual(P.credit_of({}, "owner@example.com"), "owner@example.com")                      # old purchase -> lead owner
        self.assertEqual(P.credit_of({"credit_to": "rep2@example.com"}, "owner@example.com"), "rep2@example.com")
