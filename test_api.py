"""Run: pip install fastapi httpx && python test_api.py"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

tmp = tempfile.mkdtemp()
DB = f"{tmp}/t.db"

# --- build a v1 database first, to prove the migration works on real data ---
v1 = sqlite3.connect(DB)
v1.execute("""CREATE TABLE tx (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, merchant TEXT NOT NULL, card TEXT NOT NULL DEFAULT '',
  category TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
v1.execute("CREATE UNIQUE INDEX tx_dedup ON tx(ts, amount_cents, merchant)")
v1.execute("INSERT INTO tx(ts, amount_cents, merchant, card) VALUES ('2026-10-01T10:00:00-07:00', 645, 'JJ Bean #12', 'Visa')")
v1.commit()
v1.close()

os.environ.update(DB_PATH=DB, RULES_PATH=str(Path(__file__).parent / "data" / "categories.json"),
                  API_TOKEN="secret", MONTHLY_BUDGET="1500", DISABLE_WORKER="1",
                  ANTHROPIC_API_KEY="test-key")
sys.path.insert(0, str(Path(__file__).parent / "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

c = TestClient(main.app)
H = {"Authorization": "Bearer secret"}
TZ = main.TZ

# parsing
assert main.parse_amount("$12.34") == 1234
assert main.parse_amount("CA$1,234.56") == 123456
assert main.parse_amount("12,34") == 1234
assert main.merchant_key("SAVE-ON-FOODS #1234") == main.merchant_key("Save-On-Foods #77") == "save on foods"

# auth + dashboard
assert c.get("/summary").status_code == 401
assert c.get("/health").status_code == 200
assert c.get("/").status_code == 200 and "<html" in c.get("/").text.lower()

# migration kept v1 row and added key
rows = c.get("/tx", headers=H).json()
assert rows["count"] == 1 and rows["items"][0]["merchant"] == "JJ Bean #12"
assert rows["items"][0]["category_source"] == "pending"

# ---- categorization: rule first, then AI (mocked), AI failure leaves pending ----
class FakeResp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass
    def json(self): return {"content": [{"type": "text", "text": self.text}]}

now = datetime.now(TZ).replace(microsecond=0)
iso = lambda d: d.isoformat()
c.post("/tx", headers=H, json={"amount": "84.10", "merchant": "SAVE-ON-FOODS #1234", "card": "Amex", "ts": iso(now)})
c.post("/tx", headers=H, json={"amount": "12.00", "merchant": "Mystery Noodle Bar", "card": "Visa", "ts": iso(now - timedelta(hours=1))})
c.post("/tx", headers=H, json={"amount": "40.00", "merchant": "Weird Place", "card": "Visa", "ts": iso(now - timedelta(hours=2))})

calls = []
def fake_post(url, **kw):
    m = kw["json"]["messages"][0]["content"]
    calls.append(m)
    if "Weird" in m:
        raise RuntimeError("api down")
    return FakeResp("Dining")

with mock.patch.object(main.httpx, "post", fake_post):
    main.categorize_pending()

items = {t["merchant"]: t for t in c.get("/tx", headers=H).json()["items"]}
assert items["JJ Bean #12"]["category"] == "Coffee" and items["JJ Bean #12"]["category_source"] == "rule"
assert items["SAVE-ON-FOODS #1234"]["category"] == "Groceries"
assert items["Mystery Noodle Bar"]["category"] == "Dining" and items["Mystery Noodle Bar"]["category_source"] == "ai"
assert items["Weird Place"]["category"] == "Other" and items["Weird Place"]["category_source"] == "pending"
assert len(calls) == 2, calls  # rules matched the others, no AI call for them

# retry later succeeds; a second store of a known merchant needs no AI call
with mock.patch.object(main.httpx, "post", lambda url, **kw: FakeResp("Shopping.")):
    main.categorize_pending()
c.post("/tx", headers=H, json={"amount": "9.00", "merchant": "Mystery Noodle Bar", "ts": iso(now - timedelta(days=1))})
with mock.patch.object(main.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call expected"))):
    main.categorize_pending()
items = {(t["merchant"], t["amount"]): t for t in c.get("/tx", headers=H).json()["items"]}
assert items[("Weird Place", 40.0)]["category"] == "Shopping"
assert items[("Mystery Noodle Bar", 9.0)]["category"] == "Dining"

# ---- user correction: one-off vs apply to merchant ----
nid = items[("Mystery Noodle Bar", 12.0)]["id"]
c.patch(f"/tx/{nid}", headers=H, json={"category": "Groceries"})
items = {(t["merchant"], t["amount"]): t for t in c.get("/tx", headers=H).json()["items"]}
assert items[("Mystery Noodle Bar", 12.0)]["category"] == "Groceries"
assert items[("Mystery Noodle Bar", 9.0)]["category"] == "Dining"
c.patch(f"/tx/{nid}", headers=H, json={"category": "Coffee", "apply_to_merchant": True})
items = {(t["merchant"], t["amount"]): t for t in c.get("/tx", headers=H).json()["items"]}
assert items[("Mystery Noodle Bar", 12.0)]["category"] == "Coffee"
assert items[("Mystery Noodle Bar", 9.0)]["category"] == "Coffee"
assert items[("Mystery Noodle Bar", 9.0)]["category_source"] == "user"
assert c.patch(f"/tx/{nid}", headers=H, json={"category": "Nope"}).status_code == 422

# ---- filters ----
r = c.get("/tx", headers=H, params={"category": "Coffee"}).json()
assert r["count"] == 3, r
r = c.get("/tx", headers=H, params={"q": "noodle"}).json()
assert r["count"] == 2
t = now.date()
r = c.get("/tx", headers=H, params={"start": t.isoformat(), "end": t.isoformat()}).json()
assert all(i["date"] == t.isoformat() for i in r["items"])

# ---- recurring ----
gym_day = max(1, t.day - 1) if t.day > 1 else 1
r = c.post("/recurring", headers=H, json={"name": "Club16 Gym", "amount": "29.99", "day": gym_day,
                                          "category": "Health & Fitness"}).json()
gid = r["id"]
gym = [i for i in c.get("/tx", headers=H, params={"q": "Club16"}).json()["items"]]
assert len(gym) == 1 and gym[0]["source"] == "recurring" and gym[0]["category"] == "Health & Fitness"
assert main.run_recurring() == 0  # idempotent
# simulate time passing 2 months: two more charges, clamped day
future = date(t.year + (t.month + 1) // 12, (t.month + 1) % 12 + 1, 28)
assert main.run_recurring(future) == 2
# future-dated day not logged
c.post("/recurring", headers=H, json={"name": "iCloud+", "amount": 3.99, "day": 31, "category": "Subscriptions"})
icloud = c.get("/tx", headers=H, params={"q": "iCloud"}).json()["items"]
from calendar import monthrange
assert len(icloud) == (1 if t.day == monthrange(t.year, t.month)[1] else 0)
c.patch(f"/recurring/{gid}", headers=H, json={"active": False})
assert c.get("/recurring", headers=H).json()[-1]["active"] in (True, False)
assert c.delete(f"/recurring/{gid}", headers=H).status_code == 200

# ---- categories ----
assert c.post("/categories", headers=H, json={"name": "Gifts", "budget": 100}).status_code == 200
assert c.post("/categories", headers=H, json={"name": "Gifts"}).status_code == 409
c.patch("/categories/Gifts", headers=H, json={"new_name": "Presents"})
c.patch(f"/tx/{nid}", headers=H, json={"category": "Presents"})
assert c.delete("/categories/Presents", headers=H).status_code == 200
assert next(i for i in c.get("/tx", headers=H).json()["items"] if i["id"] == nid)["category"] == "Other"
assert c.delete("/categories/Other", headers=H).status_code == 422
c.patch("/categories/Dining", headers=H, json={"budget": 250})
assert next(x for x in c.get("/categories", headers=H).json() if x["name"] == "Dining")["budget"] == 250

# ---- settings ----
c.put("/settings", headers=H, json={"monthly_budget": 2000})
assert c.get("/settings", headers=H).json()["monthly_budget"] == 2000

# ---- budgets ----
c.post("/categories", headers=H, json={"name": "Hobbies"})
hist = {"2026-09": 10000, "2026-10": 10000, "2026-11": 12000, "2026-12": 200000, "2027-02": 11000}  # Jan: nothing
with sqlite3.connect(DB) as x:
    for ym, cents in hist.items():
        x.execute("""INSERT INTO tx(ts, amount_cents, merchant, card, category, merchant_key, source)
                     VALUES (?,?,?,'',?,?, 'manual')""", (f"{ym}-10T12:00:00-08:00", cents, "Hobby Shop", "Hobbies", "hobby shop"))
with mock.patch.object(main, "today", return_value=date(2027, 3, 15)):
    b = c.get("/budgets", headers=H).json()
hob = next(i for i in b["items"] if i["name"] == "Hobbies")
assert b["months"] == ["2026-09", "2026-10", "2026-11", "2026-12", "2027-02"], b["months"]  # January: no data at all
assert hob["typical"] == 110 and hob["average"] == 486 and hob["last"] == 110, hob  # median ignores December
assert b["total"] == 2000
assert c.put("/budgets", headers=H, json={"categories": {"Nope": 5}}).status_code == 422
assert c.put("/budgets", headers=H, json={"categories": {"Hobbies": -1}}).status_code == 422
c.put("/budgets", headers=H, json={"categories": {"Hobbies": 105}})   # total left out: unchanged
b = c.get("/budgets", headers=H).json()
assert b["total"] == 2000 and next(i for i in b["items"] if i["name"] == "Hobbies")["budget"] == 105
assert next(i for i in b["items"] if i["name"] == "Dining")["budget"] == 250   # untouched
c.put("/settings", headers=H, json={"income_account": None})   # Settings no longer touches the budget
assert c.get("/settings", headers=H).json()["monthly_budget"] == 2000
c.put("/budgets", headers=H, json={"total": 1800, "categories": {"Hobbies": None}})
b = c.get("/budgets", headers=H).json()
assert b["total"] == 1800 and next(i for i in b["items"] if i["name"] == "Hobbies")["budget"] is None
c.put("/budgets", headers=H, json={"total": 2000})
with sqlite3.connect(DB) as x:
    x.execute("DELETE FROM tx WHERE merchant='Hobby Shop'")
c.delete("/categories/Hobbies", headers=H)

# ---- stats ----
m = c.get("/stats/month", headers=H).json()
assert m["is_current"] and m["budget"] == 2000 and len(m["days"]) >= 28
assert all("recurring" in x for x in m["categories"])
assert next(x for x in m["categories"] if x["name"] == "Health & Fitness")["recurring"] >= 29.99  # the Club16 charge
assert abs(sum(d["total"] for d in m["days"]) - m["total"]) < 0.01
assert abs(sum(x["total"] for x in m["categories"]) - m["total"]) < 0.01
assert any(x["name"] == "Dining" and x["budget"] == 250 for x in m["categories"])
pm = c.get("/stats/month", headers=H, params={"month": main.shift_month(t.strftime('%Y-%m'), -1)}).json()
assert not pm["is_current"] and pm["projected"] is None
tr = c.get("/stats/trend", headers=H).json()
assert len(tr["months"]) == 6 and tr["months"][-1]["total"] == m["total"]

s = c.get("/summary", headers=H).json()
assert "usual_today" in s and s["recent"][0]["color"].startswith("#")
assert s["budget"] == 2000

# batch path still works
r = c.post("/tx/batch", headers=H, json={"lines": f"{iso(now)}|$5.25|Amex|Matchstick Coffee\n \nbad"}).json()
assert r["added"] == 1 and len(r["errors"]) == 1

# declined tap: the Shortcut sends empty fields; skipped (200), not saved, not an error
before = c.get("/tx", headers=H).json()["count"]
r = c.post("/tx", headers=H, json={"amount": "", "merchant": "", "card": "", "ts": iso(now)})
assert r.status_code == 200 and r.json()["skipped"] == "empty amount" and r.json()["id"] is None
r = c.post("/tx/batch", headers=H, json={"lines": f"{iso(now)}| ||\n{iso(now)}|$3.00|Visa|Kiosk"}).json()
assert r["added"] == 1 and r["skipped"] == 1 and r["errors"] == []
assert c.get("/tx", headers=H).json()["count"] == before + 1
assert c.post("/tx", headers=H, json={"amount": "abc", "merchant": "X"}).status_code == 422
# Shortcut keys with a stray space or capital, and null fields, still work
r = c.post("/tx", headers=H, json={"merchant ": "Momo Sushi", "Amount": "$7.45", "card​": "Visa", "ts": iso(now)}).json()
assert r["id"] and c.get("/tx", headers=H, params={"q": "Momo"}).json()["items"][0]["amount"] == 7.45
c.delete(f"/tx/{r['id']}", headers=H)
assert c.post("/tx", headers=H, json={"merchant": None, "amount": None, "card": None}).json()["skipped"] == "empty amount"

# ---- foreign currency ----
assert main.parse_money("$12.34") == (1234, "CAD")
assert main.parse_money("CA$5.00") == (500, "CAD")
assert main.parse_money("US$1,234.56") == (123456, "USD")
assert main.parse_money("AU$20.00") == (2000, "AUD")
assert main.parse_money("8,50 €") == (850, "EUR")
assert main.parse_money("£3.20") == (320, "GBP")
assert main.parse_money("¥1,200") == (120000, "JPY")
assert main.parse_money("12.00 usd") == (1200, "USD")
assert main.parse_money("฿350.00") == (35000, "THB")
assert main.parse_money(7.5) == (750, "CAD")

class FakeRate:
    def __init__(self, rate): self.rate = rate
    def raise_for_status(self): pass
    def json(self): return {"rates": {"CAD": self.rate}}

fx_calls = []
def fake_get(url, params=None, **kw):
    fx_calls.append((url, params))
    return FakeRate({"USD": 1.40, "EUR": 1.50}[params["from"]])

day = (now - timedelta(days=2)).replace(hour=13)
with mock.patch.object(main.httpx, "get", fake_get):
    r = c.post("/tx", headers=H, json={"amount": "US$10.00", "merchant": "Diner NYC", "card": "Visa", "ts": iso(day)}).json()
    c.post("/tx", headers=H, json={"amount": "US$20.00", "merchant": "Diner NYC", "card": "Visa", "ts": iso(day + timedelta(hours=1))})
assert len(fx_calls) == 1, fx_calls  # same currency + day: cached
fx = next(i for i in c.get("/tx", headers=H, params={"q": "Diner NYC"}).json()["items"] if i["orig_amount"] == 10.0)
assert fx["amount"] == round(10 * 1.40 * (1 + main.FX_FEE_PCT / 100), 2), fx
assert fx["orig_currency"] == "USD" and fx["fx_rate"] == 1.40 and not fx["fx_pending"]

# rate source down: stored as pending (amount 0), worker converts it later
main._fx_down_until = 0
def down(*a, **k): raise RuntimeError("offline")
line = f"{iso(day)}|€8,50|Amex|Café Paris"
with mock.patch.object(main.httpx, "get", down):
    r = c.post("/tx/batch", headers=H, json={"lines": line}).json()
assert r["added"] == 1 and not r["errors"]
paris = c.get("/tx", headers=H, params={"q": "Paris"}).json()["items"][0]
assert paris["fx_pending"] and paris["amount"] == 0 and paris["orig_amount"] == 8.5
with mock.patch.object(main.httpx, "get", down):  # breaker: no network wait for a minute
    assert main.convert_pending() == 0
main._fx_down_until = 0
with mock.patch.object(main.httpx, "get", fake_get):
    assert main.convert_pending() == 1
    # the phone re-sends the same queue line: still one purchase
    assert c.post("/tx/batch", headers=H, json={"lines": line}).json()["duplicates"] == 1
paris = c.get("/tx", headers=H, params={"q": "Paris"}).json()
assert paris["count"] == 1 and paris["items"][0]["amount"] == round(8.5 * 1.5 * (1 + main.FX_FEE_PCT / 100), 2)
assert not paris["items"][0]["fx_pending"]

# typing the statement's CAD amount keeps the original, drops the estimated rate
c.patch(f"/tx/{fx['id']}", headers=H, json={"amount": "14.52"})
fx = next(i for i in c.get("/tx", headers=H, params={"q": "Diner NYC"}).json()["items"] if i["id"] == fx["id"])
assert fx["amount"] == 14.52 and fx["orig_amount"] == 10.0 and fx["fx_rate"] is None
# saving the edit form unchanged doesn't touch the conversion
c.patch(f"/tx/{paris['items'][0]['id']}", headers=H, json={"amount": f"{paris['items'][0]['amount']:.2f}", "note": "lunch"})
p2 = c.get("/tx", headers=H, params={"q": "Paris"}).json()["items"][0]
assert p2["fx_rate"] == 1.5 and p2["note"] == "lunch"

# ---- accounts + balances + net worth ----
r = c.post("/accounts", headers=H, json={"name": "TD Chequing", "type": "chequing", "balance": "2,500.00"}).json()
chq = r["id"]
visa = c.post("/accounts", headers=H, json={"name": "TD Visa", "type": "credit"}).json()["id"]
assert c.post("/accounts", headers=H, json={"name": "TD Visa", "type": "credit"}).status_code == 409
assert c.post("/accounts", headers=H, json={"name": "X", "type": "crypto"}).status_code == 422
a = c.get("/accounts", headers=H).json()
assert a["net_worth"] == 2500 and next(x for x in a["items"] if x["id"] == visa)["balance"] is None
assert c.post(f"/accounts/{visa}/balances", headers=H, json={"balance": 400}).status_code == 200
a = c.get("/accounts", headers=H).json()
assert (a["assets"], a["liabilities"], a["net_worth"]) == (2500, 400, 2100), a
# history: chequing also had a balance 40 days ago; visa didn't -> that month is partial
old = (t - timedelta(days=40)).isoformat()
c.post(f"/accounts/{chq}/balances", headers=H, json={"balance": 1000, "day": old})
assert c.post(f"/accounts/{chq}/balances", headers=H, json={"balance": 1, "day": "2999-01-01"}).status_code == 422
nw = {m["month"]: m for m in c.get("/stats/networth", headers=H, params={"months": 3}).json()["months"]}
this_m, old_m = t.strftime("%Y-%m"), old[:7]
assert nw[this_m]["net"] == 2100 and not nw[this_m]["partial"]
assert nw[old_m]["net"] == 1000 and nw[old_m]["partial"]
# same day again replaces; closing = final 0 balance + inactive
c.post(f"/accounts/{visa}/balances", headers=H, json={"balance": 350})
assert c.get(f"/accounts/{visa}/balances", headers=H).json() == [{"day": t.isoformat(), "balance": 350.0}]
c.patch(f"/accounts/{visa}", headers=H, json={"active": False, "name": "Old Visa"})
assert c.get("/accounts", headers=H).json()["net_worth"] == 2150

# ---- income ----
r = c.post("/income", headers=H, json={"amount": "865.40", "payer": "ACME", "ts": t.isoformat(), "account_id": chq}).json()
iid = r["id"]
assert not r["duplicate"] and c.post("/income", headers=H, json={"amount": "865.40", "payer": "ACME", "ts": t.isoformat()}).json()["duplicate"]
assert c.post("/income", headers=H, json={"amount": 5, "payer": "X", "account_id": 999}).status_code == 422
inc = c.get("/income", headers=H, params={"start": t.isoformat(), "end": t.isoformat()}).json()
assert inc["count"] == 1 and inc["items"][0]["account"] == "TD Chequing" and inc["items"][0]["ts"][11:16] == "12:00"
m = c.get("/stats/month", headers=H).json()
assert m["income"] == 865.40 and m["income_count"] == 1
assert abs(m["net"] - (865.40 - m["total"])) < 0.01
assert m["savings_rate"] == round(m["net"] / 865.40 * 100, 1)
tr = c.get("/stats/trend", headers=H).json()["months"][-1]
assert tr["income"] == 865.40 and abs(tr["net"] - (865.40 - tr["total"])) < 0.01
assert c.get("/summary", headers=H).json()["income_month"] == 865.40
c.patch(f"/income/{iid}", headers=H, json={"amount": "900", "account_id": 0, "note": "raise"})
it = c.get("/income", headers=H).json()["items"][0]
assert it["amount"] == 900 and it["account_id"] is None and it["note"] == "raise"
# spending endpoints are untouched by income
assert c.get("/tx", headers=H, params={"q": "ACME"}).json()["count"] == 0

# ---- recurring income, weekly ----
anchor = t - timedelta(days=15)  # occurrences: anchor, +7, +14 (all <= today)
r = c.post("/recurring", headers=H, json={"name": "Paycheque", "amount": "850.00", "kind": "income",
                                          "freq": "weekly", "anchor": anchor.isoformat(), "account_id": chq}).json()
pid = r["id"]
pay = [x for x in c.get("/income", headers=H).json()["items"] if x["payer"] == "Paycheque"]
assert len(pay) == 3 and all(x["source"] == "recurring" and x["account"] == "TD Chequing" for x in pay)
assert main.run_recurring() == 0  # idempotent
assert main.run_recurring(t + timedelta(days=6)) == 1  # next week
assert c.post("/recurring", headers=H, json={"name": "Bad", "amount": 1, "freq": "weekly"}).status_code == 422
rec = next(x for x in c.get("/recurring", headers=H).json() if x["id"] == pid)
assert rec["kind"] == "income" and rec["freq"] == "weekly" and rec["category"] is None
# pause + resume skips the paused weeks
c.patch(f"/recurring/{pid}", headers=H, json={"active": False})
c.patch(f"/recurring/{pid}", headers=H, json={"active": True})
assert main.last_occurrence_before("2026-01-02", 7, date(2026, 1, 16)) == "2026-01-09"
assert main.last_occurrence_before("2026-01-02", 7, date(2026, 1, 2)) is None
# biweekly spend still lands in tx
c.post("/recurring", headers=H, json={"name": "Cleaner", "amount": 60, "freq": "biweekly",
                                      "anchor": (t - timedelta(days=14)).isoformat(), "category": "Other"})
assert c.get("/tx", headers=H, params={"q": "Cleaner"}).json()["count"] == 2

# deleting an account keeps the income, just unlinked
c.delete(f"/accounts/{chq}", headers=H)
assert all(x["account_id"] is None for x in c.get("/income", headers=H).json()["items"])

# ---- balances include income paid in since the recorded balance ----
ws = c.post("/accounts", headers=H, json={"name": "Wealthsimple", "type": "chequing"}).json()["id"]
d5 = (t - timedelta(days=5)).isoformat()
c.post(f"/accounts/{ws}/balances", headers=H, json={"balance": 1000, "day": d5})
c.post("/income", headers=H, json={"amount": 300, "payer": "Same Day", "ts": d5, "account_id": ws})      # in the balance already
c.post("/income", headers=H, json={"amount": 850.00, "payer": "ACME", "ts": (t - timedelta(days=2)).isoformat(), "account_id": ws})
c.post("/income", headers=H, json={"amount": 50, "payer": "Elsewhere", "ts": (t - timedelta(days=1)).isoformat()})  # no account
acc = next(a for a in c.get("/accounts", headers=H).json()["items"] if a["id"] == ws)
assert acc["balance"] == 1850.00 and acc["recorded_balance"] == 1000 and acc["income_since"] == 850.00, acc
card = c.post("/accounts", headers=H, json={"name": "Visa Test", "type": "credit", "balance": 500}).json()["id"]
c.post("/income", headers=H, json={"amount": 100, "payer": "Refund-ish", "ts": t.isoformat(), "account_id": card})
assert next(a for a in c.get("/accounts", headers=H).json()["items"] if a["id"] == card)["balance"] == 500  # same day
# ---- default income account (Settings) ----
assert c.get("/settings", headers=H).json()["income_account"] is None
assert c.put("/settings", headers=H, json={"monthly_budget": 2000, "income_account": 9999}).status_code == 422
c.put("/settings", headers=H, json={"monthly_budget": 2000, "income_account": ws})
c.put("/settings", headers=H, json={"monthly_budget": 2000})  # leaving it out keeps it
assert c.get("/settings", headers=H).json()["income_account"] == ws
iid = c.post("/income", headers=H, json={"amount": 25, "payer": "Gift", "ts": d5}).json()["id"]  # before ws's balance: no change
inc = {x["id"]: x for x in c.get("/income", headers=H).json()["items"]}
assert inc[iid]["account_id"] == ws
iid2 = c.post("/income", headers=H, json={"amount": 5, "payer": "Chosen", "ts": d5, "account_id": card}).json()["id"]
assert {x["id"]: x for x in c.get("/income", headers=H).json()["items"]}[iid2]["account_id"] == card  # an explicit pick wins
side = c.post("/recurring", headers=H, json={"name": "Side gig", "amount": 40, "kind": "income", "freq": "weekly",
                                             "anchor": d5}).json()["id"]
gig = [x for x in c.get("/income", headers=H).json()["items"] if x["payer"] == "Side gig"]
assert gig and all(x["account_id"] == ws for x in gig)
c.delete(f"/recurring/{side}", headers=H)
for x in gig + [{"id": iid}, {"id": iid2}]:
    c.delete(f"/income/{x['id']}", headers=H)
c.patch(f"/accounts/{ws}", headers=H, json={"active": False})
assert c.get("/settings", headers=H).json()["income_account"] is None  # closed accounts don't take income
c.patch(f"/accounts/{ws}", headers=H, json={"active": True})
c.put("/settings", headers=H, json={"monthly_budget": 2000, "income_account": 0})
assert c.get("/settings", headers=H).json()["income_account"] is None
nw_now = c.get("/stats/networth", headers=H, params={"months": 1}).json()["months"][0]
assert abs(nw_now["net"] - c.get("/accounts", headers=H).json()["net_worth"]) < 0.01  # chart uses the same balances

# ---- bank statement CSV import ----
import bankcsv  # noqa: E402
assert bankcsv.num("1,234.56") == 123456 and bankcsv.num("(12.00)") == -1200 and bankcsv.num("12/03/2026") is None
assert bankcsv.num("$5") == 500 and bankcsv.num("") is None and bankcsv.num("abc") is None
assert bankcsv.parse_day("2026-10-03") == date(2026, 10, 3) and bankcsv.parse_day("Oct 3, 2026") == date(2026, 10, 3)
assert bankcsv.parse_day("03-Oct-2026") == date(2026, 10, 3) and bankcsv.parse_day("10/03/2026") == date(2026, 10, 3)
assert bankcsv.parse_day("10/03/2026", "dmy") == date(2026, 3, 10)
assert bankcsv.detect_order(["14/10/2026", "03/10/2026"]) == ("dmy", False)
assert bankcsv.detect_order(["10/14/2026"]) == ("mdy", False) and bankcsv.detect_order(["10/03/2026"]) == ("mdy", True)
assert bankcsv.is_own_transfer("Transfer to TFSA") and not bankcsv.is_own_transfer("Interac e-Transfer to Landlord")

def mdy(d): return d.strftime("%m/%d/%Y")
# a tap and a converted US$ purchase the statement should recognise
tap_day = t - timedelta(days=3)
c.post("/tx", headers=H, json={"amount": "6.45", "merchant": "Starbucks", "card": "Visa", "ts": iso(datetime.combine(tap_day, datetime.min.time(), TZ).replace(hour=8))})
main._fx_down_until = 0
with mock.patch.object(main.httpx, "get", fake_get):
    c.post("/tx", headers=H, json={"amount": "US$20.00", "merchant": "Hotel NYC", "card": "Visa", "ts": iso(datetime.combine(tap_day, datetime.min.time(), TZ).replace(hour=20))})
hotel = c.get("/tx", headers=H, params={"q": "Hotel NYC"}).json()["items"][0]
assert hotel["orig_currency"] == "USD"

# TD-style credit card export: no header, MM/DD/YYYY, debit, credit, balance
td = "\n".join([
    f"{mdy(tap_day + timedelta(days=1))},STARBUCKS #4421 VANCOUVER BC,6.45,,612.24",
    f"{mdy(tap_day + timedelta(days=2))},HOTEL NYC NEW YORK NY,29.10,,641.34",
    f"{mdy(tap_day + timedelta(days=2))},AMZN MKTP CA*AB12CD,42.99,,684.33",
    f"{mdy(tap_day + timedelta(days=2))},AMZN MKTP CA*AB12CD,42.99,,727.32",
    f"{mdy(tap_day + timedelta(days=2))},PAYMENT - THANK YOU,,500.00,227.32",
    f"{mdy(tap_day + timedelta(days=2))},BEST BUY #12 REFUND,,19.99,207.33",
])
pv = c.post("/import/preview", headers=H, json={"account_id": card, "csv": td}).json()
assert pv["sign"] == "debit_credit" and pv["date_order"] == "mdy" and pv["balance"] is None  # card: no balance snapshot
acts = [r["action"] for r in pv["rows"]]
assert acts == ["matched", "matched", "purchase", "purchase", "skip", "purchase"], pv["rows"]
assert pv["rows"][1]["fix"] == 29.10 and pv["rows"][0]["fix"] is None   # FX tap corrected to the statement
assert pv["rows"][5]["amount"] == -19.99 and pv["rows"][4]["reason"] == "card payment"
r = c.post("/import/commit", headers=H, json={"account_id": card, "csv": td}).json()
assert (r["purchase"], r["matched"], r["skip"], r["fixed"]) == (3, 2, 1, 1), r
accs = {a["id"]: a for a in c.get("/accounts", headers=H).json()["items"]}
assert accs[card]["imported_through"] == (tap_day + timedelta(days=2)).isoformat()  # last statement row
assert accs[ws]["imported_through"] is None
hotel = c.get("/tx", headers=H, params={"q": "Hotel NYC"}).json()["items"][0]
assert hotel["amount"] == 29.10 and hotel["orig_amount"] == 20.0 and hotel["fx_rate"] is None
amzn = c.get("/tx", headers=H, params={"q": "Amzn"}).json()
assert amzn["count"] == 2 and all(i["source"] == "bank" and i["card"] == "Visa Test" for i in amzn["items"])
assert amzn["items"][0]["merchant"] == "Amzn Mktp Ca*Ab12Cd"  # all-caps descriptions get title-cased
assert c.get("/tx", headers=H, params={"q": "Starbucks"}).json()["count"] == 1  # not duplicated
assert c.get("/tx", headers=H, params={"q": "Best Buy"}).json()["items"][0]["amount"] == -19.99
# importing the same file again (or an overlapping one) adds nothing
pv = c.post("/import/preview", headers=H, json={"account_id": card, "csv": td}).json()
assert all(r["action"] == "seen" for r in pv["rows"])
assert c.post("/import/commit", headers=H, json={"account_id": card, "csv": td}).json()["purchase"] == 0

# Wealthsimple-style chequing: header, ISO dates, one signed amount, balance; newest first
pay_day = t - timedelta(days=2)  # the ACME income logged above
ws_csv = "\n".join([
    "Account: Chequing ****1234",
    "date,transaction,description,amount,balance",
    f"{t.isoformat()},WD,Interac e-Transfer to Landlord,-1200.00,7432.58",
    f"{(t - timedelta(days=1)).isoformat()},TRFOUT,Transfer to TFSA,-500.00,8632.58",
    f"{pay_day.isoformat()},DEP,ACME PAYROLL,865.40,9132.58",
    f"{pay_day.isoformat()},DEP,CANADA GST CREDIT,111.25,8241.53",
    f"{(t - timedelta(days=4)).isoformat()},WD,TD VISA PREAUTH PYMT,-250.00,8130.28",
    f"{(t - timedelta(days=4)).isoformat()},SPEND,Costco Wholesale,-86.12,8380.28",
])
pv = c.post("/import/preview", headers=H, json={"account_id": ws, "csv": ws_csv}).json()
assert pv["sign"] == "negative_out" and pv["date_order"] == "ymd"
by = {r["description"]: r for r in pv["rows"]}
assert by["Interac e-Transfer to Landlord"]["action"] == "purchase"            # e-transfers are real spending
assert by["Transfer to TFSA"]["action"] == "skip"
assert by["Acme Payroll"]["action"] == "matched" and by["Acme Payroll"]["fix"] == 865.40  # paycheque, new amount
assert by["Canada Gst Credit"]["action"] == "income"
assert by["Td Visa Preauth Pymt"]["action"] == "skip"
assert pv["balance"] == {"day": t.isoformat(), "amount": 7432.58, "newer_balance_day": None}  # newest row's balance
# user overrides: count the TFSA transfer as spending, skip the GST credit
i_tfsa, i_gst = by["Transfer to TFSA"]["i"], by["Canada Gst Credit"]["i"]
assert c.post("/import/commit", headers=H, json={"account_id": ws, "csv": ws_csv,
              "actions": {str(by["Td Visa Preauth Pymt"]["i"]): "income"}}).status_code == 422  # money out isn't income
r = c.post("/import/commit", headers=H, json={"account_id": ws, "csv": ws_csv,
                                              "actions": {str(i_tfsa): "purchase", str(i_gst): "skip"}}).json()
assert (r["purchase"], r["income"], r["matched"], r["skip"], r["fixed"]) == (3, 0, 1, 2, 1), r
assert c.get("/tx", headers=H, params={"q": "TFSA"}).json()["count"] == 1
scan = [i for i in c.get("/income", headers=H).json()["items"] if i["payer"] == "ACME" and i["account_id"] == ws]
assert len(scan) == 1 and scan[0]["amount"] == 865.40
acc = next(a for a in c.get("/accounts", headers=H).json()["items"] if a["id"] == ws)
assert acc["balance"] == 7432.58 and acc["as_of"] == t.isoformat()  # statement balance replaces the estimate
pv = c.post("/import/preview", headers=H, json={"account_id": ws, "csv": ws_csv}).json()
assert {r["action"] for r in pv["rows"]} == {"seen"}

# DD/MM dates detected; flip fixes a file whose signs came out backwards; junk is a clean 422
dm = "Date,Details,Amount\n14/10/2026,COFFEE ONE,4.50\n13/10/2026,COFFEE TWO,3.75\n"
pv = c.post("/import/preview", headers=H, json={"account_id": card, "csv": dm}).json()
assert pv["date_order"] == "dmy" and pv["rows"][0]["date"] == "2026-10-14" and pv["rows"][0]["amount"] == 4.50
pv = c.post("/import/preview", headers=H, json={"account_id": card, "csv": dm, "flip": True}).json()
assert pv["rows"][0]["amount"] == -4.50
assert c.post("/import/preview", headers=H, json={"account_id": card, "csv": "hello,world\n"}).status_code == 422
assert c.post("/import/preview", headers=H, json={"account_id": 9999, "csv": dm}).status_code == 404
# deleting the account forgets its imported rows
c.delete(f"/accounts/{card}", headers=H)

# ---- OpenAI Decisions categorization ----
cats_now = c.get("/categories", headers=H).json()
assert next(x for x in cats_now if x["name"] == "Coffee")["description"].startswith("Cafes")  # seeded hints
class FakeDecision:
    def __init__(self, choice, conf): self.choice, self.conf = choice, conf
    def raise_for_status(self): pass
    def json(self): return {"answers": [{"type": "choice", "name": "category", "choice": self.choice,
                                         "confidence": self.conf, "probabilities": []}], "usage": {"input_tokens": 150}}
sent = []
def fake_decide(url, **kw):
    assert url == "https://api.openai.com/v1/decisions", url
    body = kw["json"]; sent.append(body)
    q = body["questions"][0]
    assert body["model"] == main.DECISIONS_MODEL and q["type"] == "choice" and q["name"] == "category"
    assert {"value": "Coffee", "description": next(x for x in cats_now if x["name"] == "Coffee")["description"]} in q["choices"]
    m = body["input"]
    if "Lantern" in m: return FakeDecision("Dining", 0.93)      # sure
    if "Moose" in m: return FakeDecision("Other", 1.0)          # confidently Other -> still review
    if "Hazy" in m: return FakeDecision("Dining", 0.41)         # unsure -> review
    if "Broken" in m: raise RuntimeError("503")                 # stays pending
    if "Bogus" in m: return FakeDecision("Not A Category", 0.9)  # off-list answer is rejected
    raise AssertionError(m)

for name, amt in [("Lantern Noodle House", 18), ("Duck Duck Moose 2", 25), ("Hazy Tap Room", 31),
                  ("Broken Merchant", 9), ("Bogus Shop", 7), ("JJ Bean Commercial", 5.5)]:
    c.post("/tx", headers=H, json={"amount": amt, "merchant": name, "ts": iso(now - timedelta(minutes=amt))})
with mock.patch.object(main, "OPENAI_API_KEY", "sk-test"), mock.patch.object(main.httpx, "post", fake_decide):
    main.categorize_pending()
    assert not any("JJ Bean" in b["input"] for b in sent)  # keyword rule handled it, no API call
    items = {i["merchant"]: i for i in c.get("/tx", headers=H, params={"start": t.isoformat()}).json()["items"]}
    L, M, Hz = items["Lantern Noodle House"], items["Duck Duck Moose 2"], items["Hazy Tap Room"]
    assert (L["category"], L["category_source"], L["category_confidence"], L["category_review"]) == ("Dining", "ai", 0.93, False)
    assert M["category"] == "Other" and M["category_review"]
    assert Hz["category"] == "Dining" and Hz["category_review"] and Hz["category_confidence"] == 0.41
    assert items["Broken Merchant"]["category_source"] == "pending" and items["Bogus Shop"]["category_source"] == "pending"
    assert items["JJ Bean Commercial"]["category_source"] == "rule"
    rev = c.get("/tx", headers=H, params={"review": "true"}).json()
    assert {i["merchant"] for i in rev["items"]} >= {"Duck Duck Moose 2", "Hazy Tap Room"} and rev["count"] >= 2
    assert all(i["category_review"] for i in rev["items"])
    month_rev = c.get("/stats/month", headers=H).json()["review_count"]
    # confirming (same category) clears the flag for the merchant
    c.patch(f"/tx/{Hz['id']}", headers=H, json={"category": "Dining", "apply_to_merchant": True})
    hz = next(i for i in c.get("/tx", headers=H, params={"q": "Hazy"}).json()["items"])
    assert hz["category_source"] == "user" and not hz["category_review"] and hz["category_confidence"] is None
    assert c.get("/stats/month", headers=H).json()["review_count"] == month_rev - 1
    # a later run retries only what's still pending
    sent.clear(); main.categorize_pending()
    retried = {b["input"] for b in sent}
    assert {"Merchant: Bogus Shop", "Merchant: Broken Merchant"} <= retried
    assert not retried & {"Merchant: Lantern Noodle House", "Merchant: Duck Duck Moose 2", "Merchant: Hazy Tap Room"}
    s = c.get("/settings", headers=H).json()
    assert s["ai_enabled"] and s["ai_model"].startswith("OpenAI Decisions") and s["ai_review_below"] == 0.75
# ---- a fake Decisions API that answers any question type, for the features below ----
class FakeAnswers:
    def __init__(self, answers): self.answers = answers
    def raise_for_status(self): pass
    def json(self): return {"answers": self.answers, "usage": {"input_tokens": 100}}
ai_calls = []
def fake_ai(url, **kw):
    body = kw["json"]; inp = body["input"]; ai_calls.append(body)
    out = []
    for q in body["questions"]:
        n = q["name"]
        if n == "category":
            out.append({"name": n, "type": "choice", "choice": "Other", "confidence": 0.9})
        elif n == "need":
            want = any(w in inp for w in ("Pizza", "Arcade", "Netflix", "Grabby"))
            out.append({"name": n, "type": "choice", "choice": "want" if want else "need",
                        "confidence": 0.2 if "Grabby" in inp else 0.88})
        elif n == "subscription":
            out.append({"name": n, "type": "predicate", "probability": 0.97 if "Streamflix" in inp else 0.04})
        elif n == "same":
            out.append({"name": n, "type": "predicate", "probability": 0.92 if "SQ *ST4421" in inp.upper() else 0.08})
        elif n == "payback":
            vals = [ch["value"] for ch in q["choices"]]
            assert vals[-2:] == ["refund", "income"], vals
            if "JAMIE" in inp.upper():
                out.append({"name": n, "type": "choice", "choice": "refund", "confidence": 0.64})
            else:   # pick the purchase named in the e-Transfer's memo
                hit = next((ch["value"] for ch in q["choices"] if ch["value"].startswith("tx:")
                            and ch["description"].split(",")[0].upper() in inp.upper()), "income")
                out.append({"name": n, "type": "choice", "choice": hit, "confidence": 0.86})
        elif n == "kind":
            line = inp.split("Statement line: ")[1].split("\n")[0].upper()
            pick = {"WS TRANSFER 4471": ("own_transfer", 0.91), "E-TRANSFER FROM JAMIE": ("refund", 0.64),
                    "ACME PAYROLL": ("income", 0.97), "BC HYDRO": ("spending", 0.95)}.get(line, ("spending", 0.5))
            assert {"value": pick[0]} in [{"value": ch["value"]} for ch in q["choices"]], (line, q["choices"])
            out.append({"name": n, "type": "choice", "choice": pick[0], "confidence": pick[1]})
        else:
            raise AssertionError(n)
    return FakeAnswers(out)

# ---- needs vs wants ----
nd = t - timedelta(days=1)
def add(merchant, amount, day, category=None, **kw):
    return c.post("/tx", headers=H, json={"amount": amount, "merchant": merchant, "category": category,
                  "ts": iso(datetime.combine(day, datetime.min.time(), TZ).replace(hour=12)), **kw}).json()["id"]
g_id = add("Nesters Need Test", 40, nd, "Groceries")
p_id = add("Pizza Want Test", 25, nd, "Dining")
s_id = add("Gizmo Shop Test", 15, nd, "Shopping")   # Shopping: depends -> unrated until rated
items = {i["merchant"]: i for i in c.get("/tx", headers=H, params={"q": "Test", "start": nd.isoformat(), "end": nd.isoformat()}).json()["items"]}
assert items["Nesters Need Test"]["need"] is True and items["Nesters Need Test"]["need_source"] == "category"
assert items["Pizza Want Test"]["need"] is False and items["Gizmo Shop Test"]["need"] is None
m0 = c.get("/stats/month", headers=H, params={"month": nd.strftime("%Y-%m")}).json()
assert abs(m0["essential"] + m0["optional"] + m0["unrated"] - m0["total"]) < 0.01
# you decide: this merchant is a need
c.patch(f"/tx/{s_id}", headers=H, json={"need": True})
gz = c.get("/tx", headers=H, params={"q": "Gizmo"}).json()["items"][0]
assert gz["need"] is True and gz["need_source"] == "user"
m1 = c.get("/stats/month", headers=H, params={"month": nd.strftime("%Y-%m")}).json()
assert abs(m1["essential"] - m0["essential"] - 15) < 0.01 and abs(m1["unrated"] - m0["unrated"] + 15) < 0.01
wants = c.get("/tx", headers=H, params={"need": "want", "q": "Test"}).json()["items"]
assert "Pizza Want Test" in {i["merchant"] for i in wants} and "Nesters Need Test" not in {i["merchant"] for i in wants}
# AI rates merchants that have no rating; it never overrides yours
add("Arcade Fun Test", 30, nd, "Shopping")
add("Grabby Ride Test", 14, nd, "Transport")  # AI unsure -> Transport's default wins
with mock.patch.object(main, "OPENAI_API_KEY", "sk-test"), mock.patch.object(main.httpx, "post", fake_ai):
    assert main.rate_needs_pending() > 0
    assert main.rate_needs_pending() == 0  # nothing left to rate
arc = c.get("/tx", headers=H, params={"q": "Arcade"}).json()["items"][0]
assert arc["need"] is False and arc["need_source"] == "ai"
assert c.get("/tx", headers=H, params={"q": "Gizmo"}).json()["items"][0]["need_source"] == "user"
gr = c.get("/tx", headers=H, params={"q": "Grabby"}).json()["items"][0]
assert gr["need"] is True and gr["need_source"] == "category"  # 20%-sure "want" ignored

# ---- subscription finder ----
for k in (3, 2, 1):  # monthly, steady price
    add("Streamflix Premium", 20.99, t - timedelta(days=30 * k + 2), "Subscriptions")
for k in range(5):   # weekly visits, steady price: a pattern, but not a subscription
    add("Corner Bakery Visit", 4.75, t - timedelta(days=7 * k + 1), "Dining")
for d in (40, 33, 9):  # irregular
    add("Random Hardware", 19.00, t - timedelta(days=d), "Shopping")
sugg = c.get("/subscriptions/suggestions", headers=H).json()  # no AI: pattern only
names = {s["name"] for s in sugg}
assert "Streamflix Premium" in names and "Corner Bakery Visit" in names and "Random Hardware" not in names
with mock.patch.object(main, "OPENAI_API_KEY", "sk-test"), mock.patch.object(main.httpx, "post", fake_ai):
    ai_calls.clear()
    sugg = c.get("/subscriptions/suggestions", headers=H).json()
    n_calls = len(ai_calls)
    assert c.get("/subscriptions/suggestions", headers=H).json() == sugg and len(ai_calls) == n_calls  # cached
sf = next(s for s in sugg if s["name"] == "Streamflix Premium")
assert sf["freq"] == "monthly" and sf["amount"] == 20.99 and sf["charges"] == 3 and sf["probability"] == 0.97
assert "Corner Bakery Visit" not in {s["name"] for s in sugg}  # AI: just regular visits
before = c.get("/tx", headers=H, params={"q": "Streamflix"}).json()["count"]
r = c.post(f"/subscriptions/{sf['merchant_key']}/accept", headers=H).json()
assert r["freq"] == "monthly" and r["category"] == "Subscriptions"
rec = next(x for x in c.get("/recurring", headers=H).json() if x["id"] == r["recurring_id"])
assert rec["name"] == "Streamflix Premium" and rec["amount"] == 20.99
after = c.get("/tx", headers=H, params={"q": "Streamflix"}).json()["count"]
assert after - before <= 1  # at most this month's charge, if it's due; never the past ones again
assert "Streamflix Premium" not in {s["name"] for s in c.get("/subscriptions/suggestions", headers=H).json()}
# already set up under another name (same price, same schedule): not suggested
c.post("/recurring", headers=H, json={"name": "Music Plan", "amount": 9.99, "day": 1, "category": "Subscriptions", "log_this_month": False})
for k in (3, 2, 1):
    add("Spotify P1234Ab", 9.99, t - timedelta(days=30 * k + 1), "Subscriptions")
assert "Spotify P1234Ab" not in {s["name"] for s in c.get("/subscriptions/suggestions", headers=H).json()}
for k in (2, 1):
    add("Cloudbox Storage", 2.99, t - timedelta(days=30 * k + 3), "Subscriptions")
cb = next(s for s in c.get("/subscriptions/suggestions", headers=H).json() if s["name"] == "Cloudbox Storage")
c.post(f"/subscriptions/{cb['merchant_key']}/dismiss", headers=H)
assert "Cloudbox Storage" not in {s["name"] for s in c.get("/subscriptions/suggestions", headers=H).json()}

# ---- smarter statement import (AI second opinion in the preview) ----
aic = c.post("/accounts", headers=H, json={"name": "AI Chequing", "type": "chequing"}).json()["id"]
d2 = t - timedelta(days=2)
add("Starbucks", 6.47, d2 - timedelta(days=1))           # same purchase, no shared words with the bank's name
add("Joe's Pizza Place", 12.37, d2 - timedelta(days=1))  # same amount+date as an unrelated statement row
st_csv = "\n".join(["date,description,amount", *[f"{d2.isoformat()},{desc},{amt}" for desc, amt in [
    ("WS TRANSFER 4471", "-500.00"), ("E-TRANSFER FROM JAMIE", "45.00"), ("ACME PAYROLL", "2000.00"),
    ("BC HYDRO", "-80.00"), ("SQ *ST4421 VANCOUVER", "-6.47"), ("UNRELATED HARDWARE", "-12.37")]]])
with mock.patch.object(main, "OPENAI_API_KEY", "sk-test"), mock.patch.object(main.httpx, "post", fake_ai):
    pv = c.post("/import/preview", headers=H, json={"account_id": aic, "csv": st_csv}).json()
by = {r["description"]: r for r in pv["rows"]}
assert by["Ws Transfer 4471"]["action"] == "skip" and by["Ws Transfer 4471"]["ai"]["choice"] == "own_transfer"  # AI agrees
assert by["E-Transfer From Jamie"]["action"] == "purchase" and by["E-Transfer From Jamie"]["money_in"]  # paid back
assert by["E-Transfer From Jamie"]["ai_unsure"]                           # 64% -> highlighted
assert by["Acme Payroll"]["action"] == "income" and not by["Acme Payroll"]["ai_unsure"]
assert by["Bc Hydro"]["action"] == "purchase"
assert by["Sq *St4421 Vancouver"]["action"] == "matched"                  # AI: same purchase
assert by["Unrelated Hardware"]["action"] == "purchase" and by["Unrelated Hardware"]["match"] is None
# commit applies exactly what was reviewed (no AI at commit time)
acts = {str(r["i"]): r["action"] for r in pv["rows"]}
with mock.patch.object(main.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI on commit"))):
    r = c.post("/import/commit", headers=H, json={"account_id": aic, "csv": st_csv, "actions": acts}).json()
assert (r["purchase"], r["income"], r["matched"], r["skip"]) == (3, 1, 1, 1), r
assert c.get("/tx", headers=H, params={"q": "Jamie"}).json()["items"][0]["amount"] == -45.0  # refund, not income
assert c.get("/tx", headers=H, params={"q": "Unrelated Hardware"}).json()["count"] == 1

# ---- split bills: shares, IOUs and e-Transfer paybacks ----
sd = t - timedelta(days=3)
earls = add("Earls Split Test", 120, sd, "Dining")
before = c.get("/stats/month", headers=H, params={"month": sd.strftime("%Y-%m")}).json()
dining0 = next(x for x in before["categories"] if x["name"] == "Dining")["total"]
assert c.post(f"/tx/{earls}/shares", headers=H, json={"person": "Priya", "amount": 121}).status_code == 422  # more than the bill
assert c.post(f"/tx/{earls}/shares", headers=H, json={"person": " ", "amount": 5}).status_code == 422
priya = c.post(f"/tx/{earls}/shares", headers=H, json={"person": "Priya", "amount": "60"}).json()["id"]
e = next(i for i in c.get("/tx", headers=H, params={"q": "Earls Split"}).json()["items"])
assert (e["amount"], e["full_amount"], e["shared"], e["owed"]) == (60, 120, 60, 60), e
after = c.get("/stats/month", headers=H, params={"month": sd.strftime("%Y-%m")}).json()
assert abs(next(x for x in after["categories"] if x["name"] == "Dining")["total"] - (dining0 - 60)) < 0.01  # only your share
assert c.patch(f"/tx/{earls}", headers=H, json={"amount": 50}).status_code == 422   # below what's split off
assert c.patch(f"/shares/{priya}", headers=H, json={"amount": 70}).status_code == 200
assert c.patch(f"/shares/{priya}", headers=H, json={"amount": 130}).status_code == 422
c.patch(f"/shares/{priya}", headers=H, json={"amount": 60})
owed = c.get("/shares", headers=H).json()
assert owed["owed"] >= 60 and any(x["id"] == priya and not x["paid"] and x["merchant"] == "Earls Split Test" for x in owed["items"])
# a statement: Priya's e-Transfer settles the IOU (no AI needed); Sam's is matched to the bill by the AI
pb_csv = "\n".join(["date,description,debit,credit",
    f"{(sd + timedelta(days=1)).isoformat()},INTERAC E-TRANSFER FROM: PRIYA SHAH 8812,,60.00",
    f"{(sd + timedelta(days=2)).isoformat()},E-TRANSFER FROM SAM - EARLS SPLIT TEST,,30.00",
    f"{(sd + timedelta(days=2)).isoformat()},E-TRANSFER FROM MOM,,200.00",
    f"{(sd + timedelta(days=2)).isoformat()},SPLIT TEST HARDWARE,14.10,"])
with mock.patch.object(main, "OPENAI_API_KEY", "sk-test"), mock.patch.object(main.httpx, "post", fake_ai):
    pv = c.post("/import/preview", headers=H, json={"account_id": aic, "csv": pb_csv}).json()
by = {r["description"]: r for r in pv["rows"]}
pr, sam, mom = by["Interac E-Transfer From: Priya Shah 8812"], by["E-Transfer From Sam - Earls Split Test"], by["E-Transfer From Mom"]
assert pr["action"] == "payback" and pr["payback"]["target"] == f"share:{priya}" and pr["payback"]["person"] == "Priya", pr
assert sam["action"] == "payback" and sam["payback"]["target"] == f"tx:{earls}" and sam["person"].startswith("Sam"), sam
assert not sam["ai_unsure"]
assert mom["action"] == "income", mom   # nothing $200+ to pay back -> stays income
pbs = {str(r["i"]): r["payback"]["target"] for r in pv["rows"] if r["action"] == "payback"}
acts = {str(r["i"]): r["action"] for r in pv["rows"]}
with mock.patch.object(main.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI on commit"))):
    r = c.post("/import/commit", headers=H, json={"account_id": aic, "csv": pb_csv, "actions": acts, "paybacks": pbs}).json()
assert (r["payback"], r["income"], r["purchase"]) == (2, 1, 1), r
sh = {x["person"]: x for x in c.get(f"/tx/{earls}/shares", headers=H).json()}
assert sh["Priya"]["paid"] and sh["Priya"]["from_statement"] and sh["Priya"]["paid_day"] == (sd + timedelta(days=1)).isoformat()
sam_share = next(x for p, x in sh.items() if p.startswith("Sam"))
assert sam_share["amount"] == 30 and sam_share["paid"]
e = next(i for i in c.get("/tx", headers=H, params={"q": "Earls Split"}).json()["items"])
assert (e["amount"], e["owed"]) == (30, 0), e
assert c.get("/income", headers=H).json()["items"][0]["payer"] != "E-Transfer From Sam"   # paybacks aren't income
# re-importing: seen; a payback with no target or for too much is refused
assert all(r["action"] == "seen" for r in c.post("/import/preview", headers=H, json={"account_id": aic, "csv": pb_csv}).json()["rows"])
big = "date,description,debit,credit\n" + f"{sd.isoformat()},E-TRANSFER FROM ZED,,500.00\n{sd.isoformat()},ZED HARDWARE,3.00,"
assert c.post("/import/commit", headers=H, json={"account_id": aic, "csv": big, "actions": {"0": "payback"}}).status_code == 422
assert c.post("/import/commit", headers=H, json={"account_id": aic, "csv": big, "actions": {"0": "payback"},
                                                 "paybacks": {"0": f"tx:{earls}"}}).status_code == 422
# manual "paid" toggle, and deleting the purchase removes its shares
c.patch(f"/shares/{priya}", headers=H, json={"paid": False})
assert not next(x for x in c.get(f"/tx/{earls}/shares", headers=H).json() if x["id"] == priya)["paid"]
c.delete(f"/tx/{earls}", headers=H)
assert c.get(f"/tx/{earls}/shares", headers=H).json() == []
assert main.bankcsv.transfer_person("INTERAC E-TRANSFER FROM: JAMIE LEE 1234") == "Jamie Lee"
assert main.bankcsv.transfer_person("e-Transfer received - Jamie Lee") == "Jamie Lee"

# without AI the keyword guesses still work (and nothing breaks)
with mock.patch.object(main, "OPENAI_API_KEY", ""):
    pv2 = c.post("/import/preview", headers=H, json={"account_id": aic, "csv": st_csv.replace("4471", "4472")}).json()
assert any(r["description"] == "Ws Transfer 4472" for r in pv2["rows"])

# ---- trusted devices: your Mac over Tailscale needs no token ----
with mock.patch.object(main, "TRUSTED_DEVICES", {"100.64.0.10"}):
    mac = {"X-Forwarded-For": "100.64.0.10"}
    assert c.get("/summary", headers=mac).status_code == 200
    assert c.get("/settings", headers=mac).json()["trusted_device"] is True
    assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.20"}).status_code == 401   # not on the list
    # a forged header from elsewhere: Caddy appends the real client last, and that's what counts
    assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.10, 100.64.0.30"}).status_code == 401
    assert c.get("/summary").status_code == 401                                                  # no header at all
    assert c.get("/summary", headers=H).status_code == 200                                       # token still works
    assert c.get("/settings", headers=H).json()["trusted_device"] is False
assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.10"}).status_code == 401  # empty list trusts nobody
# with PROXY_SECRET set, a forwarded IP only counts when Caddy's secret header comes with it
with mock.patch.object(main, "TRUSTED_DEVICES", {"100.64.0.10"}), mock.patch.object(main, "PROXY_SECRET", "s3cret"):
    assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.10"}).status_code == 401
    assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.10", "X-Spend-Proxy": "wrong"}).status_code == 401
    assert c.get("/summary", headers={"X-Forwarded-For": "100.64.0.10", "X-Spend-Proxy": "s3cret"}).status_code == 200

# ---- merchant grouping ignores order / reference codes ----
mk = main.merchant_key
assert mk("AMZN Mktp CA*5L8HC5UG1") == mk("Amzn Mktp Ca*By42u1s82") == "amzn mktp ca"
assert mk("Shell C01217") == mk("Shell C10093") == "shell" and mk("Chatime Cha114") == "chatime"
assert mk("Club16 Gym") == "club16 gym" and mk("49th Parallel") == "49th parallel"  # not codes
assert mk("5L8HC5UG1") == "5l8hc5ug1"  # a name that's only a code keeps it (no "unknown" pile)
# migration: old per-order keys collapse into one, keeping your choice and the majority
with main.db() as cx:
    for i, (name, oldkey) in enumerate([("AMZN Mktp CA*AA11BB22C", "amzn mktp ca aa11bb22c"),
                                        ("AMZN Mktp CA*DD33EE44F", "amzn mktp ca dd33ee44f"),
                                        ("AMZN Mktp CA*GG55HH66I", "amzn mktp ca gg55hh66i")]):
        cx.execute("INSERT INTO tx(ts, amount_cents, merchant, merchant_key, source) VALUES (?,?,?,?, 'bank')",
                   (f"2026-05-0{i + 1}T12:00:00-07:00", 1000 + i, name, oldkey))
    cx.executemany("INSERT INTO merchant_category(merchant_key, category, source, confidence) VALUES (?,?,?,?)",
                   [("amzn mktp ca aa11bb22c", "Shopping", "ai", 0.9), ("amzn mktp ca dd33ee44f", "Shopping", "ai", 0.8),
                    ("amzn mktp ca gg55hh66i", "Groceries", "user", None)])   # your choice beats two AI votes
    cx.executemany("INSERT INTO merchant_need(merchant_key, need, source, confidence) VALUES (?,?,?,?)",
                   [("amzn mktp ca aa11bb22c", 0, "ai", 0.7), ("amzn mktp ca dd33ee44f", 0, "ai", 0.9),
                    ("amzn mktp ca gg55hh66i", 1, "ai", 0.6)])                # majority: want
    cx.execute("INSERT INTO subscription_checks(merchant_key, dismissed) VALUES ('amzn mktp ca dd33ee44f', 1)")
    assert main.regroup_merchants(cx) >= 1
    assert {r[0] for r in cx.execute("SELECT DISTINCT merchant_key FROM tx WHERE merchant IN "
                                     "('AMZN Mktp CA*AA11BB22C', 'AMZN Mktp CA*DD33EE44F', 'AMZN Mktp CA*GG55HH66I')")} == {"amzn mktp ca"}
    cat = cx.execute("SELECT * FROM merchant_category WHERE merchant_key IN ('amzn mktp ca', 'amzn mktp ca aa11bb22c', 'amzn mktp ca dd33ee44f', 'amzn mktp ca gg55hh66i')").fetchall()
    assert len(cat) == 1 and (cat[0]["merchant_key"], cat[0]["category"], cat[0]["source"]) == ("amzn mktp ca", "Groceries", "user")
    nd_ = cx.execute("SELECT * FROM merchant_need WHERE merchant_key IN ('amzn mktp ca', 'amzn mktp ca aa11bb22c', 'amzn mktp ca dd33ee44f', 'amzn mktp ca gg55hh66i')").fetchall()
    assert len(nd_) == 1 and nd_[0]["need"] == 0 and nd_[0]["confidence"] == 0.9
    assert cx.execute("SELECT dismissed FROM subscription_checks WHERE merchant_key='amzn mktp ca'").fetchone()[0] == 1
    assert main.regroup_merchants(cx) == 0  # idempotent
    assert cx.execute("SELECT value FROM settings WHERE key='merchant_key_version'").fetchone()[0] == str(main.MERCHANT_KEY_VERSION)
amz = c.get("/tx", headers=H, params={"q": "AMZN Mktp CA*", "start": "2026-05-01", "end": "2026-05-31"}).json()["items"]
assert {i["category"] for i in amz} == {"Groceries"} and {i["need"] for i in amz} == {False}

# category descriptions are editable and sent with the next request
c.patch("/categories/Coffee", headers=H, json={"description": "Cafes only"})
assert next(x for x in c.get("/categories", headers=H).json() if x["name"] == "Coffee")["description"] == "Cafes only"

# ---- weekly summary via ntfy ----
wk = c.get("/summary/weekly", headers=H).json()
end = date.fromisoformat(wk["end"])
assert end.weekday() == 6 and end <= t and (date.fromisoformat(wk["end"]) - date.fromisoformat(wk["start"])).days == 6
assert wk["title"].startswith("Week of ") and wk["title"].endswith(" spent") and isinstance(wk["lines"], list)
mid = c.get("/summary/weekly", headers=H, params={"end": "2026-10-05"}).json()   # spans two months
assert mid["start"] == "2026-09-29" and "Sep 29–Oct 5" in mid["title"], mid["title"]
assert any(l.startswith("October: ") for l in mid["lines"]), mid["lines"]
# shows up: an open IOU and next week's recurring charges
iou_tx = add("Weekly IOU Test", 80, end - timedelta(days=1), "Dining")
c.post(f"/tx/{iou_tx}/shares", headers=H, json={"person": "Robin", "amount": 40})
c.post("/recurring", headers=H, json={"name": "Weekly Test Gym", "amount": 20, "freq": "weekly",
                                      "anchor": (end + timedelta(days=3)).isoformat(), "category": "Health & Fitness"})
wk = c.get("/summary/weekly", headers=H, params={"end": end.isoformat()}).json()
assert any("Owed to you" in l and "Robin" in l for l in wk["lines"]), wk["lines"]
assert any(l.startswith("Next week:") and "Weekly Test Gym $20" in l for l in wk["lines"]), wk["lines"]
assert main.upcoming_recurring  # monthly entries land on their (clamped) day
sent_n = []
def fake_ntfy(url, **kw):
    sent_n.append((url, kw)); return FakeAnswers([])
assert c.post("/summary/weekly/send", headers=H).status_code == 502          # not configured
with mock.patch.object(main, "NTFY_URL", "https://ntfy.test"), mock.patch.object(main, "NTFY_TOKEN", "tk_x"), \
        mock.patch.object(main.httpx, "post", fake_ntfy):
    sun = datetime.combine(end, datetime.min.time(), TZ)
    assert not main.maybe_send_weekly(sun.replace(hour=18, minute=59))         # too early
    assert main.maybe_send_weekly(sun.replace(hour=19, minute=3))              # due
    assert not main.maybe_send_weekly(sun.replace(hour=19, minute=8))          # once per week
    url, kw = sent_n[0]
    assert url == "https://ntfy.test" and kw["json"]["topic"] == "spend" and kw["json"]["click"].startswith("https://")
    assert kw["headers"]["Authorization"] == "Bearer tk_x" and kw["json"]["title"].startswith("Week of")
    with sqlite3.connect(DB) as x:
        x.execute("DELETE FROM settings WHERE key='weekly_sent'")
    assert main.maybe_send_weekly(sun + timedelta(hours=28))                   # missed Sunday: Monday 4am catches up
    with sqlite3.connect(DB) as x:
        x.execute("DELETE FROM settings WHERE key='weekly_sent'")
    assert not main.maybe_send_weekly(sun + timedelta(hours=37))               # but not after Monday noon
    assert c.post("/summary/weekly/send", headers=H).status_code == 200
assert c.get("/settings", headers=H).json()["weekly_summary"] is False

# ---- review fixes (2026-10-20) ----
bc = main.bankcsv
assert not bc.is_own_transfer("CRESPO RESTAURANT") and not bc.is_own_transfer("SEND E-TFR ***abc")
assert bc.E_TRANSFER.search("E-TFR AUTODEP") and bc.is_own_transfer("WS TRANSFER 4471") and bc.is_own_transfer("TFSA CONTRIBUTION")
assert not bc.is_card_payment("VISA DEBIT PURCHASE SAFEWAY") and bc.is_card_payment("TD VISA PREAUTH PYMT")
assert not bc.has("GAMEXCHANGE", ["amex"]) and bc.has("PAYMENTS RECEIVED", ["payment"])
# moving a weekly schedule's anchor doesn't re-log every past week
rid = c.post("/recurring", headers=H, json={"name": "Anchor Move Test", "amount": 10, "freq": "weekly",
                                            "anchor": (t - timedelta(days=21)).isoformat(), "category": "Other"}).json()["id"]
n0 = c.get("/tx", headers=H, params={"q": "Anchor Move Test", "start": "2026-01-01"}).json()["count"]
assert n0 == 4
c.patch(f"/recurring/{rid}", headers=H, json={"anchor": (t - timedelta(days=22)).isoformat()})
assert c.get("/tx", headers=H, params={"q": "Anchor Move Test", "start": "2026-01-01"}).json()["count"] == n0
assert c.patch(f"/recurring/{rid}", headers=H, json={"amount": "abc"}).status_code == 422
assert c.patch(f"/recurring/{rid}", headers=H, json={"amount": 0}).status_code == 422
assert c.post("/recurring", headers=H, json={"name": "Zero", "amount": 0, "day": 1}).status_code == 422
assert c.post("/recurring", headers=H, json={"name": "Bad cat", "amount": 5, "day": 1, "category": "Nope"}).status_code == 422
c.delete(f"/recurring/{rid}", headers=H)
# unknown categories and bad input are 422s, not 500s
assert c.post("/tx", headers=H, json={"amount": 5, "merchant": "X", "category": "Nope"}).status_code == 422
assert c.post("/income", headers=H, json={"amount": 0, "payer": "X"}).status_code == 422
for path, params in [("/tx", {"start": "2026-13-01"}), ("/income", {"start": "nope"}),
                     ("/stats/month", {"month": "2026-13"}), ("/summary/weekly", {"end": "x"})]:
    assert c.get(path, headers=H, params=params).status_code == 422, path
# a past week's summary only counts the month up to that week
past_end = date(2026, 10, 5)
add("Later Purchase Test", 777, date(2026, 10, 20), "Other")
assert not any("777" in l for l in c.get("/summary/weekly", headers=H, params={"end": past_end.isoformat()}).json()["lines"])
# the same row on two cards' statements stays two purchases
ca = c.post("/accounts", headers=H, json={"name": "Card A", "type": "credit"}).json()["id"]
cb = c.post("/accounts", headers=H, json={"name": "Card B", "type": "credit"}).json()["id"]
same = "date,description,amount\n2026-09-02,TWIN CAFE,7.25\n2026-09-03,OTHER THING,1.00\n"
for acc in (ca, cb):
    assert c.post("/import/commit", headers=H, json={"account_id": acc, "csv": same}).json()["purchase"] == 2
assert c.get("/tx", headers=H, params={"q": "Twin Cafe", "start": "2026-09-01", "end": "2026-09-30"}).json()["count"] == 2

print("all tests passed")
