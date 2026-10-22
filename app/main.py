"""Spend tracker API v2.

Receives Apple Pay transactions from an iOS Shortcut, stores them in SQLite,
categorizes merchants (your corrections > keyword rules > Claude Haiku),
logs recurring payments, and serves a dashboard + a compact widget summary.
"""
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import bankcsv

DB_PATH = os.environ.get("DB_PATH", "/data/spend.db")
RULES_PATH = os.environ.get("RULES_PATH", "/data/categories.json")
API_TOKEN = os.environ.get("API_TOKEN", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Vancouver"))
ENV_BUDGET = float(os.environ.get("MONTHLY_BUDGET", "0") or 0)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-5-5")
# OpenAI Decisions API (preferred when set): picks one of your categories with a confidence
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DECISIONS_MODEL = os.environ.get("DECISIONS_MODEL", "gpt-6-luna")
AI_REVIEW_BELOW = float(os.environ.get("AI_REVIEW_BELOW", "0.75"))  # flag AI picks under this for review
WORKER_INTERVAL = int(os.environ.get("WORKER_INTERVAL", "300"))
HOME_CURRENCY = os.environ.get("HOME_CURRENCY", "CAD").upper()
FX_FEE_PCT = float(os.environ.get("FX_FEE_PCT", "2.5") or 0)  # card's foreign transaction fee
STATIC = Path(__file__).parent / "static"
# Weekly summary, pushed with ntfy (self-hosted) on Sunday evening
NTFY_URL = os.environ.get("NTFY_URL", "").rstrip("/")     # server, e.g. https://ntfy.anilbronson.dev
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "spend")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://spend.anilbronson.dev")   # tapping the notification opens this
WEEKLY_DAY, WEEKLY_HOUR = 6, 19   # Sunday 7pm local

log = logging.getLogger("spend")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="spend")


@app.exception_handler(ValueError)
def bad_value(request: Request, e: ValueError):
    """Bad input that slipped past an endpoint's own checks (a date like 2026-13-01 in a query
    string) is the caller's mistake: 422, not a 500."""
    log.warning("bad value on %s: %s", request.url.path, e)
    return JSONResponse({"detail": str(e)}, status_code=422)

# Categorical palette, fixed order (light, dark). Color follows the category.
PALETTE = [
    ("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
    ("#6250d6", "#9085e9"), ("#e34948", "#e66767"),
]
OTHER = "Other"
OTHER_COLOR = "#898781"
DEFAULT_CATEGORIES = [
    "Groceries", "Dining", "Coffee", "Transport",
    "Shopping", "Health & Fitness", "Subscriptions", "Car",
]


# ---------- storage ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            """CREATE TABLE IF NOT EXISTS tx (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                merchant TEXT NOT NULL,
                card TEXT NOT NULL DEFAULT '',
                category TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # v2 columns (safe to run on a v1 database)
        have = cols(c, "tx")
        if "merchant_key" not in have:
            c.execute("ALTER TABLE tx ADD COLUMN merchant_key TEXT")
        if "source" not in have:
            c.execute("ALTER TABLE tx ADD COLUMN source TEXT NOT NULL DEFAULT 'tap'")
        if "note" not in have:
            c.execute("ALTER TABLE tx ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        # v3: foreign-currency purchases. amount_cents is always home currency; the
        # amount as charged abroad is kept in orig_*.
        for col, decl in [("orig_amount_cents", "INTEGER"), ("orig_currency", "TEXT"),
                          ("fx_rate", "REAL"), ("fx_pending", "INTEGER NOT NULL DEFAULT 0")]:
            if col not in have:
                c.execute(f"ALTER TABLE tx ADD COLUMN {col} {decl}")
        # Dedup on the amount as charged, so re-sending a purchase whose conversion was
        # still pending (or used a different rate) can't store it twice.
        c.execute("DROP INDEX IF EXISTS tx_dedup")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS tx_dedup_v3
                     ON tx(ts, COALESCE(orig_amount_cents, amount_cents), merchant)""")
        c.execute("CREATE INDEX IF NOT EXISTS tx_ts ON tx(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS tx_mkey ON tx(merchant_key)")

        c.execute(
            """CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                color TEXT NOT NULL,
                budget_cents INTEGER,
                position INTEGER NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS merchant_category (
                merchant_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                source TEXT NOT NULL,          -- user | rule | ai
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS recurring (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                day INTEGER NOT NULL,
                category TEXT,
                card TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                last_run TEXT                  -- YYYY-MM of the last logged month
            )"""
        )
        # v4: recurring can be income, and repeat weekly / every 2 weeks from `anchor`
        have_r = cols(c, "recurring")
        for col, decl in [("kind", "TEXT NOT NULL DEFAULT 'spend'"),       # spend | income
                          ("freq", "TEXT NOT NULL DEFAULT 'monthly'"),     # monthly | weekly | biweekly
                          ("anchor", "TEXT"),                              # first date, weekly/biweekly
                          ("last_date", "TEXT"),                           # last logged date, weekly/biweekly
                          ("account_id", "INTEGER")]:
            if col not in have_r:
                c.execute(f"ALTER TABLE recurring ADD COLUMN {col} {decl}")
        c.execute(
            """CREATE TABLE IF NOT EXISTS income (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                payer TEXT NOT NULL,
                account_id INTEGER,            -- where it landed (optional)
                source TEXT NOT NULL DEFAULT 'manual',   -- manual | recurring | import
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS income_dedup ON income(ts, amount_cents, payer)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,            -- see ACCOUNT_TYPES
                position INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS balances (
                account_id INTEGER NOT NULL,
                day TEXT NOT NULL,             -- YYYY-MM-DD
                balance_cents INTEGER NOT NULL, -- liabilities: amount owed, positive
                PRIMARY KEY (account_id, day)
            )"""
        )
        # v5: every statement row ever imported, so re-importing an overlapping CSV skips it
        c.execute(
            """CREATE TABLE IF NOT EXISTS bank_rows (
                fingerprint TEXT PRIMARY KEY,  -- account, day, amount, description, occurrence
                account_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                amount_cents INTEGER NOT NULL, -- > 0 money out
                description TEXT NOT NULL,
                action TEXT NOT NULL,          -- purchase | income | matched | skip
                tx_id INTEGER,
                income_id INTEGER,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # v8: other people's shares of a purchase (split bills). Spending counts only your share;
        # paid_day NULL means they still owe it. A Wealthsimple e-Transfer can settle it on import.
        c.execute(
            """CREATE TABLE IF NOT EXISTS shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_id INTEGER NOT NULL,
                person TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,  -- their share, > 0
                paid_day TEXT,                  -- YYYY-MM-DD, NULL = still owed
                bank_fp TEXT,                   -- bank_rows fingerprint of the e-Transfer that paid it
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS shares_tx ON shares(tx_id)")
        # v6: category descriptions (sent to the AI) and the AI's confidence per merchant
        if "description" not in cols(c, "categories"):
            c.execute("ALTER TABLE categories ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "confidence" not in cols(c, "merchant_category"):
            c.execute("ALTER TABLE merchant_category ADD COLUMN confidence REAL")
        # v7: need vs want per merchant (AI or the user); unrated merchants use CATEGORY_NEED
        c.execute(
            """CREATE TABLE IF NOT EXISTS merchant_need (
                merchant_key TEXT PRIMARY KEY,
                need INTEGER NOT NULL,         -- 1 essential, 0 optional
                confidence REAL,
                source TEXT NOT NULL,          -- ai | user
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # v7: subscription finder: the AI's verdict per merchant, and the ones you dismissed
        c.execute(
            """CREATE TABLE IF NOT EXISTS subscription_checks (
                merchant_key TEXT PRIMARY KEY,
                probability REAL,              -- AI: is this a subscription / recurring bill
                dismissed INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS fx_rates (
                day TEXT NOT NULL,             -- date the rate was requested for
                currency TEXT NOT NULL,
                rate REAL NOT NULL,            -- HOME_CURRENCY per 1 unit of currency
                source TEXT NOT NULL,
                PRIMARY KEY (day, currency)
            )"""
        )

        if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            for i, name in enumerate(DEFAULT_CATEGORIES):
                c.execute("INSERT INTO categories(name, color, position) VALUES (?,?,?)",
                          (name, PALETTE[i][0], i))
        c.execute("INSERT OR IGNORE INTO categories(name, color, position) VALUES (?,?,?)",
                  (OTHER, OTHER_COLOR, 999))

        for r in c.execute("SELECT id, merchant FROM tx WHERE merchant_key IS NULL").fetchall():
            c.execute("UPDATE tx SET merchant_key=? WHERE id=?", (merchant_key(r["merchant"]), r["id"]))
        # after the default categories exist; only fills empty descriptions
        for name, hint in CATEGORY_HINTS.items():
            c.execute("UPDATE categories SET description=? WHERE name=? AND description=''", (hint, name))

        v = c.execute("SELECT value FROM settings WHERE key='merchant_key_version'").fetchone()
        if (int(v[0]) if v else 1) < MERCHANT_KEY_VERSION:
            regroup_merchants(c)
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('merchant_key_version', ?)",
                      (str(MERCHANT_KEY_VERSION),))


def regroup_merchants(c) -> int:
    """After merchant_key() changes: recompute every tx.merchant_key, then merge the per-merchant
    rows of keys that now collapse into one (ten Amazon orders -> one Amazon). Your own choices
    win; otherwise the category / need held by the most purchases. Returns merged groups."""
    groups: dict[str, dict[str, int]] = {}  # new key -> {old key: purchases}
    for r in c.execute("SELECT id, merchant, merchant_key FROM tx").fetchall():
        new = merchant_key(r["merchant"])
        if new != r["merchant_key"]:
            c.execute("UPDATE tx SET merchant_key=? WHERE id=?", (new, r["id"]))
        g = groups.setdefault(new, {})
        g[r["merchant_key"]] = g.get(r["merchant_key"], 0) + 1
    merged = 0
    for new, olds in groups.items():
        if set(olds) == {new}:
            continue
        keys = list(set(olds) | {new})
        marks = ",".join("?" * len(keys))
        for table, field in (("merchant_category", "category"), ("merchant_need", "need")):
            rows = c.execute(f"SELECT * FROM {table} WHERE merchant_key IN ({marks})", keys).fetchall()
            if not rows:
                continue
            pool = [r for r in rows if r["source"] == "user"] or rows
            votes = {}
            for r in pool:
                votes[r[field]] = votes.get(r[field], 0) + olds.get(r["merchant_key"], 1)
            win = max(votes, key=votes.get)
            best = max((r for r in pool if r[field] == win), key=lambda r: r["confidence"] or 0)
            c.execute(f"DELETE FROM {table} WHERE merchant_key IN ({marks})", keys)
            c.execute(f"INSERT INTO {table}(merchant_key, {field}, source, confidence) VALUES (?,?,?,?)",
                      (new, best[field], best["source"], best["confidence"]))
        dismissed = c.execute(f"SELECT MAX(dismissed) FROM subscription_checks WHERE merchant_key IN ({marks})",
                              keys).fetchone()[0]
        c.execute(f"DELETE FROM subscription_checks WHERE merchant_key IN ({marks})", keys)
        if dismissed:
            c.execute("INSERT INTO subscription_checks(merchant_key, dismissed) VALUES (?, 1)", (new,))
        merged += 1
        log.info("regrouped %d merchant names into %r", len(olds), new)
    return merged


# ---------- parsing ----------

MERCHANT_KEY_VERSION = 2  # bump when merchant_key() changes; init_db() regroups existing data


def _is_code(tok: str) -> bool:
    """Order / reference / station codes: 'by42u1s82', 'c01217', 'chv43043', '1409qps'."""
    return len(tok) >= 6 and sum(ch.isdigit() for ch in tok) >= 3 and any(ch.isalpha() for ch in tok)


def merchant_key(m: str) -> str:
    """Normalized name that groups one merchant's purchases: 'AMZN Mktp CA*5L8HC5UG1' and
    'Amzn Mktp Ca*By42u1s82' are both 'amzn mktp ca'; 'SAVE-ON-FOODS #1234' is 'save on foods'."""
    s = m.lower()
    s = re.sub(r"#\s*\d+", " ", s)          # store numbers
    s = re.sub(r"\b\d{3,}\b", " ", s)        # long numeric ids
    s = re.sub(r"[^a-z0-9&' ]+", " ", s)
    words = s.split()
    kept = [w for w in words if not _is_code(w)]
    return " ".join(kept or words) or "unknown"  # a name that's all code keeps its code


# Longest first, so "US$" wins over "$" and "CA$" over "A$". A bare "$" is home currency.
CURRENCY_SYMBOLS = sorted([
    ("CA$", "CAD"), ("C$", "CAD"), ("US$", "USD"), ("U$", "USD"), ("AU$", "AUD"), ("A$", "AUD"),
    ("NZ$", "NZD"), ("HK$", "HKD"), ("MX$", "MXN"), ("SG$", "SGD"), ("S$", "SGD"), ("NT$", "TWD"),
    ("R$", "BRL"), ("CN¥", "CNY"), ("JP¥", "JPY"), ("¥", "JPY"), ("￥", "JPY"), ("€", "EUR"),
    ("£", "GBP"), ("₩", "KRW"), ("₹", "INR"), ("฿", "THB"), ("₱", "PHP"), ("₪", "ILS"), ("₺", "TRY"),
    ("zł", "PLN"), ("Rp", "IDR"), ("RM", "MYR"),
], key=lambda s: -len(s[0]))


def parse_money(raw) -> tuple[int, str]:
    """'$12.34' -> (1234, home), 'US$1,234.56' -> (123456, 'USD'), '8,50 €' -> (850, 'EUR'),
    '12.00 GBP' -> (1200, 'GBP'), '¥1,200' -> (120000, 'JPY'). Hundredths of the currency's unit."""
    if isinstance(raw, (int, float)):
        return round(float(raw) * 100), HOME_CURRENCY
    s = str(raw).strip()
    code = re.search(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])", s)
    if code:
        currency = code.group(1).upper()
    else:
        currency = next((iso for sym, iso in CURRENCY_SYMBOLS if sym in s), HOME_CURRENCY)
    return parse_amount_number(raw, s), currency


def parse_amount(raw) -> int:
    """Amount in hundredths, ignoring any currency (recurring payments, settings)."""
    return parse_money(raw)[0]


def parse_amount_number(raw, s: str) -> int:
    negative = s.startswith("-") or "(" in s
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        raise ValueError(f"no number in amount {raw!r}")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    cents = round(float(s) * 100)
    return -cents if negative else cents


def parse_ts(raw: str | None) -> str:
    if not raw or not raw.strip():
        return datetime.now(TZ).isoformat(timespec="seconds")
    s = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"bad timestamp {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def load_rules() -> dict[str, list[str]]:
    try:
        with open(RULES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def local_bounds(d0: date, d1: date) -> tuple[str, str]:
    """ISO strings for [d0 00:00, d1+1 00:00) local time."""
    a = datetime.combine(d0, datetime.min.time(), TZ).isoformat()
    b = datetime.combine(d1 + timedelta(days=1), datetime.min.time(), TZ).isoformat()
    return a, b


def month_range(ym: str) -> tuple[date, date]:
    y, m = map(int, ym.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def shift_month(ym: str, n: int) -> str:
    y, m = map(int, ym.split("-"))
    i = y * 12 + (m - 1) + n
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def today() -> date:
    return datetime.now(TZ).date()


# ---------- currency conversion ----------

_fx_down_until = 0.0  # after a failed fetch, requests skip the network for a minute


def fetch_rate(cur: str, day: str) -> tuple[float, str] | None:
    """HOME_CURRENCY per 1 `cur` on `day`, or the last business day before it."""
    try:
        r = httpx.get(f"https://api.frankfurter.dev/v1/{day}",
                      params={"from": cur, "to": HOME_CURRENCY}, timeout=5)
        r.raise_for_status()
        return float(r.json()["rates"][HOME_CURRENCY]), "frankfurter"
    except Exception as e:
        log.warning("frankfurter rate %s %s failed: %s", cur, day, e)
    if HOME_CURRENCY == "CAD":  # Bank of Canada only publishes CAD rates
        series = f"FX{cur}CAD"
        try:
            start = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
            r = httpx.get(f"https://www.bankofcanada.ca/valet/observations/{series}/json",
                          params={"start_date": start, "end_date": day}, timeout=5)
            r.raise_for_status()
            obs = [o for o in r.json()["observations"] if series in o]
            if obs:
                return float(obs[-1][series]["v"]), "bankofcanada"
        except Exception as e:
            log.warning("bank of canada rate %s %s failed: %s", cur, day, e)
    return None


def fx_rate(cur: str, day: str) -> float | None:
    global _fx_down_until
    day = min(day, today().isoformat())
    with db() as c:
        r = c.execute("SELECT rate FROM fx_rates WHERE day=? AND currency=?", (day, cur)).fetchone()
    if r:
        return r["rate"]
    if time.time() < _fx_down_until:
        return None
    got = fetch_rate(cur, day)
    if not got:
        _fx_down_until = time.time() + 60
        return None
    with db() as c:
        c.execute("INSERT OR REPLACE INTO fx_rates(day, currency, rate, source) VALUES (?,?,?,?)",
                  (day, cur, *got))
    return got[0]


def convert(cents: int, cur: str, ts: str) -> dict:
    """tx money columns for `cents` hundredths of `cur` spent at `ts`. Call it before opening a
    write transaction: it may hit the network and writes the rate cache. If no rate is
    available the purchase is stored as pending (amount 0) and convert_pending() fills it in."""
    if cur == HOME_CURRENCY:
        return {"amount_cents": cents, "orig_amount_cents": None, "orig_currency": None,
                "fx_rate": None, "fx_pending": 0}
    rate = fx_rate(cur, ts[:10])
    if rate is None:
        return {"amount_cents": 0, "orig_amount_cents": cents, "orig_currency": cur,
                "fx_rate": None, "fx_pending": 1}
    return {"amount_cents": round(cents * rate * (1 + FX_FEE_PCT / 100)), "orig_amount_cents": cents,
            "orig_currency": cur, "fx_rate": rate, "fx_pending": 0}


def convert_pending(limit: int = 50) -> int:
    with db() as c:
        rows = c.execute("SELECT id, ts, orig_amount_cents, orig_currency FROM tx WHERE fx_pending=1 LIMIT ?",
                         (limit,)).fetchall()
    done = 0
    for r in rows:
        m = convert(r["orig_amount_cents"], r["orig_currency"], r["ts"])
        if m["fx_pending"]:
            continue
        with db() as c:
            c.execute("UPDATE tx SET amount_cents=?, fx_rate=?, fx_pending=0 WHERE id=? AND fx_pending=1",
                      (m["amount_cents"], m["fx_rate"], r["id"]))
        done += 1
    return done


# ---------- categorization ----------

_cat_lock = threading.Lock()


def category_names(c) -> list[str]:
    return [r["name"] for r in c.execute("SELECT name FROM categories ORDER BY position")]


def rule_category(merchant: str, valid: set[str]) -> str | None:
    m = merchant.lower()
    for cat, needles in load_rules().items():
        if cat in valid and any(n.lower() in m for n in needles):
            return cat
    return None


def ai_category(merchant: str, cats: list[str]) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": AI_MODEL,
                "max_tokens": 20,
                "system": ("You categorize card purchases made in Vancouver, Canada. "
                           "Reply with exactly one category name from the list and nothing else."),
                "messages": [{"role": "user", "content":
                              f"Merchant: {merchant}\nCategories: {', '.join(cats)}"}],
            },
            timeout=20,
        )
        r.raise_for_status()
        answer = "".join(b.get("text", "") for b in r.json().get("content", [])).strip().strip(".")
    except Exception as e:  # network, auth, rate limit: retried by the worker later
        log.warning("AI categorize failed for %r: %s", merchant, e)
        return None
    for c in cats:
        if c.lower() == answer.lower():
            return c
    for c in cats:  # tolerate "Category: Dining" style answers
        if c.lower() in answer.lower():
            return c
    return OTHER


DECISIONS_INSTRUCTIONS = (
    "Categorize this card purchase for one person living in Vancouver, BC, Canada. The input is a merchant "
    "name: an Apple Pay merchant name or a raw bank statement descriptor. Ignore processor prefixes and noise "
    "(SQ *, TST-, NBX*, SP, DD/ = DoorDash, AMZN MKTP, PAYPAL *, store numbers, city/province, phone numbers, "
    "reference codes). Abbreviations: CHV or CHEV = Chevron gas station. Decide from the merchant name alone. "
    "Prefer the most specific category (coffee shop = Coffee, gas station = Transport). If the name is "
    "ambiguous or unrecognizable, choose Other.")

# Seeded into categories.description (editable via PATCH /categories); sent to the AI with each choice
CATEGORY_HINTS = {
    "Groceries": "Supermarkets and food stores (Save-On-Foods, Superstore, Safeway, T&T, Costco, No Frills, Nesters).",
    "Dining": "Restaurants, fast food, takeout and food delivery (McDonald's, Subway, DoorDash, Uber Eats, Skip).",
    "Coffee": "Cafes and coffee shops (Starbucks, Tim Hortons, JJ Bean, Matchstick, Pallet, 49th Parallel).",
    "Transport": "Transit, rideshare, taxis, car share, gas stations, parking, ferries, flights (Compass, Uber "
                 "trips, Lyft, Grab, Evo, Modo, Shell, Esso, Chevron, Petro-Canada, PayByPhone, Impark).",
    "Shopping": "General retail and online stores (Amazon, Best Buy, London Drugs, Shoppers, Canadian Tire, IKEA, "
                "Winners, Temu, AliExpress).",
    "Health & Fitness": "Gyms, sports, golf, yoga, pharmacy and health (Genesis Athletic Club, Goodlife, golf courses).",
    "Subscriptions": "Recurring digital services and software (Netflix, Spotify, YouTube, iCloud/Apple.com/bill, "
                     "Claude/Anthropic, OpenAI, Porkbun, Tailscale).",
    "Car": "Car parts, repairs, maintenance, insurance and registration (Lordco, NAPA, RockAuto, FCP Euro, ICBC, "
           "car washes). Gas goes in Transport.",
    "Alcohol": "Liquor stores, bars, pubs and breweries (BC Liquor, BCL, private liquor stores, taprooms).",
    "Fun": "Games, entertainment and hobbies (Steam, PlayStation, Nintendo, concerts, events, arcades).",
    "Date": "Only when the merchant clearly suggests a date night, e.g. a cinema like Cineplex. Never for an "
            "ordinary restaurant.",
    "Internet": "Home internet and phone service (Shaw, Telus, Rogers, Novus, Freedom, Fido).",
    "Clothing": "Clothing, shoes and activewear (Gymshark, Lululemon, Aritzia, Uniqlo, H&M, Zara).",
    "Self Development": "Courses, books, learning and professional tools (Udemy, Coursera, Indigo).",
    OTHER: "Fits none of the above, or the merchant can't be identified.",
}


# ---------- needs vs wants ----------
# Fallback until a merchant is rated (or with no AI): 1 essential, 0 optional, missing = depends
CATEGORY_NEED = {"Groceries": 1, "Transport": 1, "Car": 1, "Internet": 1,
                 "Dining": 0, "Coffee": 0, "Alcohol": 0, "Fun": 0, "Date": 0, "Clothing": 0,
                 "Subscriptions": 0, "Self Development": 0}
NEED_QUESTION = {
    "type": "choice", "name": "need",
    "instructions": ("Is a typical purchase at this merchant an essential need or an optional want, for one "
                     "person living in Vancouver, BC? Judge the usual purchase there, not edge cases: grocery "
                     "stores and pharmacies are needs even though some items are treats; restaurants, cafes "
                     "and bars are wants; gym memberships, streaming and software subscriptions are wants; "
                     "gas, transit, parking, insurance, utilities, internet, phone and rent are needs."),
    "choices": [
        {"value": "need", "description": "Essential: groceries, housing and rent, utilities, internet and phone, "
                                         "transit, gas and parking, insurance, car repairs, medicine and health care, "
                                         "basic household supplies."},
        {"value": "want", "description": "Optional: restaurants, takeout and delivery, coffee shops, bars and alcohol, "
                                         "entertainment, games and hobbies, travel and treats, most clothing and "
                                         "gadgets, subscriptions and memberships."}]}


NEED_TRUST = 0.5  # below this, a category default beats the AI's need/want call


def _need_case_sql() -> str:
    q = lambda s: s.replace("'", "''")
    whens = " ".join(f"WHEN '{q(k)}' THEN {v}" for k, v in CATEGORY_NEED.items())
    return f"CASE COALESCE(NULLIF(t.category, ''), mc.category, '{OTHER}') {whens} END"


def rate_needs_pending(limit: int = 300) -> int:
    """AI-rate need vs want for merchants that have a category but no rating yet."""
    if not OPENAI_API_KEY:
        return 0
    with db() as c:
        rows = c.execute(
            f"""SELECT x.merchant_key, MIN(x.merchant) AS merchant, MIN(x.cat) AS cat FROM ({TXQ}) x
                LEFT JOIN merchant_need n ON n.merchant_key = x.merchant_key
                WHERE n.merchant_key IS NULL AND x.cat_source != 'pending' AND x.amount_cents > 0
                GROUP BY x.merchant_key LIMIT ?""", (limit,)).fetchall()
    done = 0
    for i in range(0, len(rows), 32):
        chunk = rows[i:i + 32]
        answers = decide_many([(f"Merchant: {r['merchant']}\nCategory: {r['cat']}", [NEED_QUESTION], "need")
                               for r in chunk])
        with db() as c:
            for r, a in zip(chunk, answers):
                if not a:
                    continue
                c.execute("""INSERT OR IGNORE INTO merchant_need(merchant_key, need, confidence, source)
                             VALUES (?,?,?, 'ai')""",
                          (r["merchant_key"], int(a["need"]["choice"] == "need"), float(a["need"].get("confidence") or 0)))
                done += 1
    return done


def enrich_pending():
    """Background work after new purchases arrive: categories first (needs use them), then needs."""
    categorize_pending()
    rate_needs_pending()


def decide(input_text: str, questions: list[dict], what: str = "") -> dict[str, dict] | None:
    """One OpenAI Decisions API request. Returns {question name: answer}, or None on any failure
    (network, auth, rate limit, beta API change); callers fall back or retry later.
    Question types: choice -> answer["choice"] + ["confidence"]; predicate -> ["probability"]."""
    if not OPENAI_API_KEY:
        return None
    try:
        r = httpx.post(
            "https://api.openai.com/v1/decisions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": DECISIONS_MODEL, "input": input_text, "questions": questions},
            timeout=20)
        r.raise_for_status()
        answers = {a.get("name"): a for a in r.json()["answers"]}
        for q in questions:
            a = answers.get(q["name"])
            if a is None:
                raise ValueError(f"no answer for {q['name']!r}")
            if q["type"] == "choice" and a.get("choice") not in {ch["value"] for ch in q["choices"]}:
                raise ValueError(f"unexpected choice {a.get('choice')!r}")
        return answers
    except Exception as e:
        log.warning("Decisions request failed (%s) for %r: %s", what or questions[0]["name"], input_text[:80], e)
        return None


def decide_many(jobs: list[tuple[str, list[dict], str]]) -> list[dict | None]:
    """Run several decide() calls in parallel (no batch endpoint); results in order."""
    if not jobs:
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda j: decide(*j), jobs))


def decisions_category(merchant: str, cats: list[tuple[str, str]]) -> tuple[str, float] | None:
    """(category, confidence), or None on failure (retried later). Always one of `cats`."""
    a = decide(f"Merchant: {merchant}", [{
        "type": "choice", "name": "category", "instructions": DECISIONS_INSTRUCTIONS,
        "choices": [{"value": n, "description": d or n} for n, d in cats]}], "categorize")
    if not a:
        return None
    return a["category"]["choice"], float(a["category"].get("confidence") or 0)


def ai_enabled() -> bool:
    return bool(OPENAI_API_KEY or ANTHROPIC_API_KEY)


def categorize_pending(limit: int = 200) -> int:
    """Assign a category to merchants that have none yet: keyword rules first, then AI
    (OpenAI Decisions if configured, else Claude). Returns how many were set."""
    if not _cat_lock.acquire(blocking=False):
        return 0
    try:
        with db() as c:
            cat_rows = c.execute("SELECT name, COALESCE(description, '') AS d FROM categories ORDER BY position").fetchall()
            pending = c.execute(
                """SELECT t.merchant_key, MIN(t.merchant) AS merchant FROM tx t
                   LEFT JOIN merchant_category mc ON mc.merchant_key = t.merchant_key
                   WHERE mc.merchant_key IS NULL AND (t.category IS NULL OR t.category = '')
                   GROUP BY t.merchant_key LIMIT ?""", (limit,)).fetchall()
        cats = [r["name"] for r in cat_rows]
        done = 0
        for p in pending:
            cat, src, conf = rule_category(p["merchant"], set(cats)), "rule", None
            if not cat and OPENAI_API_KEY:
                got = decisions_category(p["merchant"], [(r["name"], r["d"]) for r in cat_rows])
                if got:
                    (cat, conf), src = got, "ai"
            elif not cat:
                cat, src = ai_category(p["merchant"], cats), "ai"
            if not cat:
                continue
            with db() as c:
                c.execute("""INSERT OR IGNORE INTO merchant_category(merchant_key, category, source, confidence)
                             VALUES (?,?,?,?)""", (p["merchant_key"], cat, src, conf))
            done += 1
        return done
    finally:
        _cat_lock.release()


# ---------- recurring ----------

STEP_DAYS = {"weekly": 7, "biweekly": 14}


def log_recurring(c, r, d: date) -> int:
    ts = datetime.combine(d, datetime.min.time(), TZ).replace(hour=9).isoformat(timespec="seconds")
    if r["kind"] == "income":
        cur = c.execute(
            """INSERT OR IGNORE INTO income(ts, amount_cents, payer, account_id, source)
               VALUES (?,?,?,?, 'recurring')""", (ts, r["amount_cents"], r["name"], r["account_id"] or income_account(c)))
    else:
        cur = c.execute(
            """INSERT OR IGNORE INTO tx(ts, amount_cents, merchant, card, category, merchant_key, source)
               VALUES (?,?,?,?,?,?, 'recurring')""",
            (ts, r["amount_cents"], r["name"], r["card"], r["category"], merchant_key(r["name"])))
    return cur.rowcount


def last_occurrence_before(anchor: str, step: int, d: date) -> str | None:
    """Latest anchor + k*step strictly before d (None if d is on/before the anchor)."""
    a = date.fromisoformat(anchor)
    if d <= a:
        return None
    return (a + timedelta(days=((d - a).days - 1) // step * step)).isoformat()


def run_recurring(now: date | None = None) -> int:
    now = now or today()
    cur = now.strftime("%Y-%m")
    added = 0
    with db() as c:
        for r in c.execute("SELECT * FROM recurring WHERE active=1").fetchall():
            if r["freq"] in STEP_DAYS:  # weekly / biweekly, every occurrence from the anchor on
                step = timedelta(days=STEP_DAYS[r["freq"]])
                d = date.fromisoformat(r["last_date"]) + step if r["last_date"] else date.fromisoformat(r["anchor"])
                last = r["last_date"]
                while d <= now:
                    added += log_recurring(c, r, d)
                    last = d.isoformat()
                    d += step
                if last != r["last_date"]:
                    c.execute("UPDATE recurring SET last_date=? WHERE id=?", (last, r["id"]))
                continue
            ym = shift_month(r["last_run"], 1) if r["last_run"] else cur
            last = r["last_run"]
            while ym <= cur:
                d0, d1 = month_range(ym)
                d = d0.replace(day=min(r["day"], d1.day))
                if d > now:
                    break
                added += log_recurring(c, r, d)
                last = ym
                ym = shift_month(ym, 1)
            if last != r["last_run"]:
                c.execute("UPDATE recurring SET last_run=? WHERE id=?", (last, r["id"]))
    return added


def worker():
    while True:
        try:
            run_recurring()
            convert_pending()
            enrich_pending()
            maybe_send_weekly()
        except Exception:
            log.exception("worker error")
        time.sleep(WORKER_INTERVAL)


init_db()


@app.on_event("startup")
def start_worker():
    if os.environ.get("DISABLE_WORKER") != "1":
        threading.Thread(target=worker, daemon=True).start()


# ---------- auth ----------

# Your own devices' Tailscale IPs: they're let in without the token. Safe only because the
# container port is bound to 127.0.0.1, so every request arrives through Caddy, which sets
# X-Forwarded-For to the real client address (a client can't inject its own).
TRUSTED_DEVICES = {ip.strip() for ip in os.environ.get("TRUSTED_DEVICES", "").split(",") if ip.strip()}


# Caddy adds this header (header_up in the Caddyfile). Without it, a forwarded IP isn't believed:
# anything on the server itself (a host-network container) could reach 127.0.0.1:8090 and claim one.
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")


def client_ip(request: Request) -> str:
    """The address Caddy saw. Caddy appends the real client last, so take the last hop."""
    direct = request.client.host if request.client else ""
    if PROXY_SECRET and not hmac.compare_digest(request.headers.get("x-spend-proxy", ""), PROXY_SECRET):
        return direct
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() if xff else direct


def is_trusted(request: Request) -> bool:
    return client_ip(request) in TRUSTED_DEVICES


def auth(request: Request, authorization: str = Header(default="")):
    if not API_TOKEN:
        raise HTTPException(500, "API_TOKEN not set on server")
    if authorization != f"Bearer {API_TOKEN}" and not is_trusted(request):
        raise HTTPException(401, "bad token")


A = [Depends(auth)]


# ---------- shared query ----------

# amount_cents is *your share*: the charge minus what other people owe or paid back (shares).
# full_cents is what the card was charged. Everything that totals spending reads amount_cents.
TXQ = f"""SELECT t.id, t.ts, t.amount_cents - COALESCE(sh.cents, 0) AS amount_cents, t.merchant, t.card,
                t.category, t.created_at, t.merchant_key, t.source, t.note, t.orig_amount_cents,
                t.orig_currency, t.fx_rate, t.fx_pending,
                t.amount_cents AS full_cents, COALESCE(sh.cents, 0) AS shared_cents, COALESCE(sh.owed, 0) AS owed_cents,
                COALESCE(NULLIF(t.category, ''), mc.category, 'Other') AS cat,
                COALESCE(cc.color, '#898781') AS color,
                CASE WHEN NULLIF(t.category, '') IS NOT NULL THEN 'manual'
                     ELSE COALESCE(mc.source, 'pending') END AS cat_source,
                CASE WHEN NULLIF(t.category, '') IS NULL THEN mc.confidence END AS cat_conf,
                -- your call or a confident AI call first, then the category's default, then an unsure AI call
                COALESCE(CASE WHEN mn.source = 'user' OR mn.confidence >= {NEED_TRUST} THEN mn.need END,
                         {_need_case_sql()}, mn.need) AS need,
                CASE WHEN mn.source = 'user' OR mn.confidence >= {NEED_TRUST} THEN mn.source
                     WHEN ({_need_case_sql()}) IS NOT NULL THEN 'category' ELSE mn.source END AS need_source
         FROM tx t
         LEFT JOIN (SELECT tx_id, SUM(amount_cents) AS cents,
                           SUM(CASE WHEN paid_day IS NULL THEN amount_cents ELSE 0 END) AS owed
                    FROM shares GROUP BY tx_id) sh ON sh.tx_id = t.id
         LEFT JOIN merchant_category mc ON mc.merchant_key = t.merchant_key
         LEFT JOIN merchant_need mn ON mn.merchant_key = t.merchant_key
         LEFT JOIN categories cc ON cc.name = COALESCE(NULLIF(t.category, ''), mc.category, 'Other')"""


# An AI pick worth a human look: it fell back to Other (couldn't place the merchant) or was unsure.
# Confirming or changing it in the dashboard makes it a 'user' choice, which clears the flag.
REVIEW_SQL = f"(cat_source = 'ai' AND (cat = '{OTHER}' OR COALESCE(cat_conf, 0) < {AI_REVIEW_BELOW}))"


def needs_review(r) -> bool:
    return r["cat_source"] == "ai" and (r["cat"] == OTHER or (r["cat_conf"] or 0) < AI_REVIEW_BELOW)


def row_out(r):
    return {
        "id": r["id"], "ts": r["ts"], "date": r["ts"][:10],
        "amount": r["amount_cents"] / 100, "merchant": r["merchant"], "card": r["card"],
        "full_amount": r["full_cents"] / 100, "shared": r["shared_cents"] / 100, "owed": r["owed_cents"] / 100,
        "category": r["cat"], "color": r["color"], "category_source": r["cat_source"],
        "category_confidence": r["cat_conf"], "category_review": needs_review(r),
        "need": None if r["need"] is None else bool(r["need"]),
        "need_source": r["need_source"],
        "source": r["source"], "note": r["note"],
        "orig_amount": r["orig_amount_cents"] / 100 if r["orig_amount_cents"] is not None else None,
        "orig_currency": r["orig_currency"], "fx_rate": r["fx_rate"], "fx_pending": bool(r["fx_pending"]),
    }


def budget(c) -> float:
    r = c.execute("SELECT value FROM settings WHERE key='monthly_budget'").fetchone()
    return float(r["value"]) if r else ENV_BUDGET


# ---------- ingest ----------

class TxIn(BaseModel):
    amount: str | float
    merchant: str
    card: str = ""
    ts: str | None = None
    category: str | None = None
    note: str = ""
    source: str = "tap"
    currency: str | None = None  # overrides the currency detected in `amount`


def prepare_money(raw, ts: str, currency: str | None = None) -> dict:
    cents, cur = parse_money(raw)
    cur = (currency or cur).strip().upper()
    if cur != HOME_CURRENCY:
        log.info("foreign amount %r -> %s", raw, cur)
    return convert(cents, cur, ts)


def insert(c, ts, money: dict, merchant, card, category=None, source="tap", note="") -> int | None:
    merchant = merchant.strip() or "Unknown"
    cur = c.execute(
        """INSERT OR IGNORE INTO tx(ts, amount_cents, orig_amount_cents, orig_currency, fx_rate, fx_pending,
                                    merchant, card, category, merchant_key, source, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, money["amount_cents"], money["orig_amount_cents"], money["orig_currency"], money["fx_rate"],
         money["fx_pending"], merchant, card.strip(), category or None, merchant_key(merchant), source, note))
    return cur.lastrowid if cur.rowcount == 1 else None


@app.post("/tx", dependencies=A)
def add_tx(t: TxIn, bg: BackgroundTasks):
    try:
        ts = parse_ts(t.ts)
        money = prepare_money(t.amount, ts, t.currency)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db() as c:
        if t.category:
            check_category(c, t.category)
        new_id = insert(c, ts, money, t.merchant, t.card, t.category, t.source, t.note)
    bg.add_task(enrich_pending)
    return {"ok": True, "id": new_id, "duplicate": new_id is None}


class Batch(BaseModel):
    lines: str  # one transaction per line: ts|amount|card|merchant


@app.post("/tx/batch", dependencies=A)
def add_batch(b: Batch, bg: BackgroundTasks):
    added = dupes = 0
    errors = []
    parsed = []  # parse and convert first: conversion may hit the network
    for n, line in enumerate(b.lines.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            errors.append(f"line {n}: expected ts|amount|card|merchant")
            continue
        ts, amount, card, merchant = parts
        try:
            ts = parse_ts(ts)
            parsed.append((ts, prepare_money(amount, ts), merchant, card))
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    with db() as c:
        for ts, money, merchant, card in parsed:
            ok = insert(c, ts, money, merchant, card)
            added += ok is not None
            dupes += ok is None
    if errors:
        log.warning("batch rejected lines: %s", errors)
    bg.add_task(enrich_pending)
    return {"ok": True, "added": added, "duplicates": dupes, "errors": errors}


# ---------- transactions ----------

@app.get("/tx", dependencies=A)
def list_tx(start: str | None = None, end: str | None = None, category: str | None = None,
            q: str | None = None, limit: int = 100, offset: int = 0, review: bool = False,
            need: str | None = None):  # need | want | unrated
    where, args = [], []
    if start or end:
        a, b = local_bounds(date.fromisoformat(start or "1970-01-01"),
                            date.fromisoformat(end or "2999-12-31"))
        where += ["t.ts >= ?", "t.ts < ?"]
        args += [a, b]
    if q:
        where.append("(t.merchant LIKE ? OR t.note LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    sql = f"SELECT * FROM ({TXQ} {'WHERE ' + ' AND '.join(where) if where else ''})"
    outer = []
    if category:
        outer.append("cat = ?")
        args.append(category)
    if review:
        outer.append(REVIEW_SQL)
    if need in ("need", "want"):
        outer.append(f"need = {1 if need == 'need' else 0}")
    elif need == "unrated":
        outer.append("need IS NULL")
    if outer:
        sql += " WHERE " + " AND ".join(outer)
    with db() as c:
        agg = c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM ({sql})", args).fetchone()
        rows = c.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"count": agg["n"], "total": agg["s"] / 100, "items": [row_out(r) for r in rows]}


class TxPatch(BaseModel):
    category: str | None = None
    apply_to_merchant: bool = False
    merchant: str | None = None
    amount: str | float | None = None
    ts: str | None = None
    note: str | None = None
    card: str | None = None
    need: bool | None = None   # essential (true) or optional (false), for every purchase at this merchant


@app.patch("/tx/{tx_id}", dependencies=A)
def edit_tx(tx_id: int, p: TxPatch):
    with db() as c:
        r = c.execute("SELECT * FROM tx WHERE id=?", (tx_id,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    money = None
    if p.amount is not None:  # resolved before the write transaction: may need a rate
        try:
            cents, cur = parse_money(p.amount)
            ts = parse_ts(p.ts) if p.ts is not None else r["ts"]
        except ValueError as e:
            raise HTTPException(422, str(e))
        if cur != HOME_CURRENCY:
            money = convert(cents, cur, ts)
        elif cents != r["amount_cents"]:
            # A home-currency amount typed over a converted one (e.g. the statement's figure):
            # keep what was charged abroad, drop the estimated rate.
            money = {"amount_cents": cents, "orig_amount_cents": r["orig_amount_cents"],
                     "orig_currency": r["orig_currency"], "fx_rate": None, "fx_pending": 0}
    with db() as c:
        try:
            if p.merchant is not None:
                c.execute("UPDATE tx SET merchant=?, merchant_key=? WHERE id=?",
                          (p.merchant.strip(), merchant_key(p.merchant), tx_id))
            if money is not None:
                split = c.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM shares WHERE tx_id=?", (tx_id,)).fetchone()[0]
                if split and money["amount_cents"] < split:
                    raise HTTPException(422, f"${split / 100:.2f} of this is split with other people; "
                                             "lower their shares first")
                c.execute("""UPDATE tx SET amount_cents=?, orig_amount_cents=?, orig_currency=?,
                             fx_rate=?, fx_pending=? WHERE id=?""",
                          (money["amount_cents"], money["orig_amount_cents"], money["orig_currency"],
                           money["fx_rate"], money["fx_pending"], tx_id))
            if p.ts is not None:
                c.execute("UPDATE tx SET ts=? WHERE id=?", (parse_ts(p.ts), tx_id))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "an identical transaction already exists")
        if p.note is not None:
            c.execute("UPDATE tx SET note=? WHERE id=?", (p.note, tx_id))
        if p.card is not None:
            c.execute("UPDATE tx SET card=? WHERE id=?", (p.card, tx_id))
        if p.need is not None:
            key = c.execute("SELECT merchant_key FROM tx WHERE id=?", (tx_id,)).fetchone()[0]
            c.execute("""INSERT INTO merchant_need(merchant_key, need, source) VALUES (?,?, 'user')
                         ON CONFLICT(merchant_key) DO UPDATE SET need=excluded.need, source='user',
                         confidence=NULL, updated_at=CURRENT_TIMESTAMP""", (key, int(p.need)))
        if p.category is not None:
            if p.category and not c.execute("SELECT 1 FROM categories WHERE name=?", (p.category,)).fetchone():
                raise HTTPException(422, f"unknown category {p.category!r}")
            if p.apply_to_merchant and p.category:
                key = c.execute("SELECT merchant_key FROM tx WHERE id=?", (tx_id,)).fetchone()[0]
                c.execute("""INSERT INTO merchant_category(merchant_key, category, source) VALUES (?,?, 'user')
                             ON CONFLICT(merchant_key) DO UPDATE SET category=excluded.category,
                             source='user', confidence=NULL, updated_at=CURRENT_TIMESTAMP""", (key, p.category))
                c.execute("UPDATE tx SET category=NULL WHERE merchant_key=? AND source != 'recurring'", (key,))
                c.execute("UPDATE tx SET category=NULL WHERE id=?", (tx_id,))
            else:
                c.execute("UPDATE tx SET category=? WHERE id=?", (p.category or None, tx_id))
    return {"ok": True}


@app.delete("/tx/{tx_id}", dependencies=A)
def delete_tx(tx_id: int):
    with db() as c:
        n = c.execute("DELETE FROM tx WHERE id=?", (tx_id,)).rowcount
        c.execute("DELETE FROM shares WHERE tx_id=?", (tx_id,))
    if not n:
        raise HTTPException(404, "not found")
    return {"ok": True}


# ---------- shares (split bills / IOUs) ----------

def shares_room(c, tx_id: int, excluding: int | None = None) -> int:
    """Cents of the charge not yet split off to other people (your share can't go below 0)."""
    t = c.execute("SELECT amount_cents FROM tx WHERE id=?", (tx_id,)).fetchone()
    if not t:
        raise HTTPException(404, "purchase not found")
    used = c.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM shares WHERE tx_id=? AND id IS NOT ?",
                     (tx_id, excluding)).fetchone()[0]
    return t["amount_cents"] - used


def share_out(r) -> dict:
    out = {"id": r["id"], "tx_id": r["tx_id"], "person": r["person"], "amount": r["amount_cents"] / 100,
           "paid": r["paid_day"] is not None, "paid_day": r["paid_day"], "from_statement": r["bank_fp"] is not None}
    if "merchant" in r.keys():
        out.update(merchant=r["merchant"], date=r["ts"][:10], full_amount=r["full_cents"] / 100)
    return out


SHAREQ = "SELECT s.*, t.merchant, t.ts, t.amount_cents AS full_cents FROM shares s JOIN tx t ON t.id = s.tx_id"


class ShareIn(BaseModel):
    person: str
    amount: str | float
    paid: bool = False


class SharePatch(BaseModel):
    person: str | None = None
    amount: str | float | None = None
    paid: bool | None = None


@app.get("/tx/{tx_id}/shares", dependencies=A)
def list_tx_shares(tx_id: int):
    with db() as c:
        return [share_out(r) for r in c.execute(SHAREQ + " WHERE s.tx_id=? ORDER BY s.id", (tx_id,))]


@app.post("/tx/{tx_id}/shares", dependencies=A)
def add_share(tx_id: int, sh: ShareIn):
    if not sh.person.strip():
        raise HTTPException(422, "who owes it?")
    try:
        cents = parse_amount(sh.amount)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db() as c:
        room = shares_room(c, tx_id)
        if cents <= 0 or cents > room:
            raise HTTPException(422, f"share must be between $0.01 and ${room / 100:.2f}")
        sid = c.execute("INSERT INTO shares(tx_id, person, amount_cents, paid_day) VALUES (?,?,?,?)",
                        (tx_id, sh.person.strip(), cents, today().isoformat() if sh.paid else None)).lastrowid
    return {"ok": True, "id": sid}


@app.patch("/shares/{sid}", dependencies=A)
def edit_share(sid: int, p: SharePatch):
    with db() as c:
        r = c.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if p.person is not None and p.person.strip():
            c.execute("UPDATE shares SET person=? WHERE id=?", (p.person.strip(), sid))
        if p.amount is not None:
            try:
                cents = parse_amount(p.amount)
            except ValueError as e:
                raise HTTPException(422, str(e))
            room = shares_room(c, r["tx_id"], excluding=sid)
            if cents <= 0 or cents > room:
                raise HTTPException(422, f"share must be between $0.01 and ${room / 100:.2f}")
            c.execute("UPDATE shares SET amount_cents=? WHERE id=?", (cents, sid))
        if p.paid is not None:
            c.execute("UPDATE shares SET paid_day=? WHERE id=?", (today().isoformat() if p.paid else None, sid))
    return {"ok": True}


@app.delete("/shares/{sid}", dependencies=A)
def delete_share(sid: int):
    with db() as c:
        if not c.execute("DELETE FROM shares WHERE id=?", (sid,)).rowcount:
            raise HTTPException(404, "not found")
    return {"ok": True}


@app.get("/shares", dependencies=A)
def list_shares(paid_since: str | None = None):
    """Everything still owed, plus paybacks since `paid_since` (default: 60 days)."""
    since = paid_since or (today() - timedelta(days=60)).isoformat()
    with db() as c:
        rows = c.execute(SHAREQ + " WHERE s.paid_day IS NULL OR s.paid_day >= ? ORDER BY t.ts DESC, s.id",
                         (since,)).fetchall()
    items = [share_out(r) for r in rows]
    return {"items": items, "owed": round(sum(i["amount"] for i in items if not i["paid"]), 2),
            "paid": round(sum(i["amount"] for i in items if i["paid"]), 2)}


# ---------- categories ----------

@app.get("/categories", dependencies=A)
def get_categories():
    with db() as c:
        return [{"name": r["name"], "color": r["color"],
                 "budget": r["budget_cents"] / 100 if r["budget_cents"] is not None else None,
                 "position": r["position"], "description": r["description"]}
                for r in c.execute("SELECT * FROM categories ORDER BY position")]


class CategoryIn(BaseModel):
    name: str
    color: str | None = None
    budget: float | None = None
    description: str = ""   # what belongs here; helps the AI choose it


@app.post("/categories", dependencies=A)
def add_category(cat: CategoryIn):
    name = cat.name.strip()
    if not name:
        raise HTTPException(422, "name required")
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM categories WHERE name != ?", (OTHER,)).fetchone()[0]
        color = cat.color or PALETTE[n % len(PALETTE)][0]
        try:
            c.execute("INSERT INTO categories(name, color, budget_cents, position, description) VALUES (?,?,?,?,?)",
                      (name, color, round(cat.budget * 100) if cat.budget else None, n, cat.description.strip()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "category exists")
    return {"ok": True}


class CategoryPatch(BaseModel):
    new_name: str | None = None
    color: str | None = None
    budget: float | None = None
    clear_budget: bool = False
    description: str | None = None


@app.patch("/categories/{name}", dependencies=A)
def edit_category(name: str, p: CategoryPatch):
    with db() as c:
        if not c.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
            raise HTTPException(404, "not found")
        if p.color:
            c.execute("UPDATE categories SET color=? WHERE name=?", (p.color, name))
        if p.description is not None:
            c.execute("UPDATE categories SET description=? WHERE name=?", (p.description.strip(), name))
        if p.clear_budget:
            c.execute("UPDATE categories SET budget_cents=NULL WHERE name=?", (name,))
        elif p.budget is not None:
            c.execute("UPDATE categories SET budget_cents=? WHERE name=?", (round(p.budget * 100), name))
        if p.new_name and p.new_name.strip() != name:
            if name == OTHER:
                raise HTTPException(422, "Other can't be renamed")
            new = p.new_name.strip()
            try:
                c.execute("UPDATE categories SET name=? WHERE name=?", (new, name))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "category exists")
            for t in ("tx", "recurring"):
                c.execute(f"UPDATE {t} SET category=? WHERE category=?", (new, name))
            c.execute("UPDATE merchant_category SET category=? WHERE category=?", (new, name))
    return {"ok": True}


@app.delete("/categories/{name}", dependencies=A)
def delete_category(name: str):
    if name == OTHER:
        raise HTTPException(422, "Other can't be deleted")
    with db() as c:
        if not c.execute("DELETE FROM categories WHERE name=?", (name,)).rowcount:
            raise HTTPException(404, "not found")
        for t in ("tx", "recurring"):
            c.execute(f"UPDATE {t} SET category=? WHERE category=?", (OTHER, name))
        c.execute("UPDATE merchant_category SET category=?, source='user' WHERE category=?", (OTHER, name))
    return {"ok": True}


# ---------- recurring ----------

def rec_out(r):
    return {"id": r["id"], "name": r["name"], "amount": r["amount_cents"] / 100, "day": r["day"],
            "category": r["category"], "card": r["card"], "active": bool(r["active"]),
            "last_run": r["last_run"], "kind": r["kind"], "freq": r["freq"], "anchor": r["anchor"],
            "last_date": r["last_date"], "account_id": r["account_id"]}


def check_recurring_shape(kind: str, freq: str, anchor: str | None):
    if kind not in ("spend", "income"):
        raise HTTPException(422, "kind must be spend or income")
    if freq not in ("monthly", *STEP_DAYS):
        raise HTTPException(422, "freq must be monthly, weekly or biweekly")
    if freq in STEP_DAYS:
        try:
            date.fromisoformat(anchor or "")
        except ValueError:
            raise HTTPException(422, "weekly/biweekly needs anchor: the first date, YYYY-MM-DD")


@app.get("/recurring", dependencies=A)
def list_recurring():
    with db() as c:
        return [rec_out(r) for r in c.execute("SELECT * FROM recurring ORDER BY day, name")]


class RecurringIn(BaseModel):
    name: str
    amount: str | float
    day: int | None = None        # monthly: day of month
    category: str | None = None
    card: str = ""
    log_this_month: bool = True   # monthly: also log this month's charge if its day has passed
    kind: str = "spend"           # spend | income
    freq: str = "monthly"         # monthly | weekly | biweekly
    anchor: str | None = None     # weekly/biweekly: first date; every occurrence since is logged
    account_id: int | None = None


@app.post("/recurring", dependencies=A)
def add_recurring(r: RecurringIn):
    check_recurring_shape(r.kind, r.freq, r.anchor)
    if r.freq == "monthly" and not (r.day and 1 <= r.day <= 31):
        raise HTTPException(422, "day must be 1-31")
    try:
        cents = parse_amount(r.amount)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if cents <= 0:
        raise HTTPException(422, "amount must be more than 0")
    if r.category and r.kind == "spend":
        with db() as c:
            check_category(c, r.category)
    cur = today().strftime("%Y-%m")
    last = shift_month(cur, -1) if r.log_this_month else cur
    day = r.day if r.freq == "monthly" else date.fromisoformat(r.anchor).day
    with db() as c:
        cur_id = c.execute(
            """INSERT INTO recurring(name, amount_cents, day, category, card, last_run, kind, freq, anchor, account_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r.name.strip(), cents, day, (r.category or None) if r.kind == "spend" else None, r.card, last,
             r.kind, r.freq, r.anchor if r.freq in STEP_DAYS else None, r.account_id or None)).lastrowid
    run_recurring()
    return {"ok": True, "id": cur_id}


class RecurringPatch(BaseModel):
    name: str | None = None
    amount: str | float | None = None
    day: int | None = None
    category: str | None = None
    card: str | None = None
    active: bool | None = None
    freq: str | None = None
    anchor: str | None = None
    account_id: int | None = None   # 0 clears it


def schedule_from_now(freq: str, anchor: str | None, day: int | None) -> dict:
    """Bookkeeping so run_recurring() logs only occurrences after today (today's included if
    it's the day). Monthly: this month counts as done once its day has passed."""
    t = today()
    if freq in STEP_DAYS:
        return {"last_date": last_occurrence_before(anchor, STEP_DAYS[freq], t)}
    ym = t.strftime("%Y-%m")
    return {"last_run": ym if day and min(day, month_range(ym)[1].day) < t.day else shift_month(ym, -1)}


def check_category(c, name: str) -> None:
    if not c.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
        raise HTTPException(422, f"unknown category {name!r}")


@app.patch("/recurring/{rid}", dependencies=A)
def edit_recurring(rid: int, p: RecurringPatch):
    with db() as c:
        r = c.execute("SELECT * FROM recurring WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        freq = p.freq or r["freq"]
        anchor = p.anchor if p.anchor is not None else r["anchor"]
        check_recurring_shape(r["kind"], freq, anchor)
        sets = {}
        if p.name is not None: sets["name"] = p.name.strip()
        if p.amount is not None:
            try:
                sets["amount_cents"] = parse_amount(p.amount)
            except ValueError as e:
                raise HTTPException(422, str(e))
            if sets["amount_cents"] <= 0:
                raise HTTPException(422, "amount must be more than 0")
        if p.day is not None: sets["day"] = max(1, min(31, p.day))
        if p.category: check_category(c, p.category)
        if p.category is not None: sets["category"] = p.category or None
        if p.card is not None: sets["card"] = p.card
        if p.account_id is not None: sets["account_id"] = p.account_id or None
        day = sets.get("day", r["day"])
        if freq != r["freq"] or anchor != r["anchor"]:
            # new schedule: log from now on. Past dates under the new schedule would be new
            # timestamps, so dedup wouldn't catch them and every past entry would double.
            sets.update(freq=freq, anchor=anchor if freq in STEP_DAYS else None,
                        **schedule_from_now(freq, anchor, day))
        if p.active is not None:
            sets["active"] = int(p.active)
            if p.active:  # resuming: don't back-fill the paused period
                sets.update(schedule_from_now(freq, anchor, day))
        for k, v in sets.items():
            c.execute(f"UPDATE recurring SET {k}=? WHERE id=?", (v, rid))
    run_recurring()
    return {"ok": True}


@app.delete("/recurring/{rid}", dependencies=A)
def delete_recurring(rid: int):
    """Stops future entries. Already-logged ones stay (delete them from the list if wanted)."""
    with db() as c:
        if not c.execute("DELETE FROM recurring WHERE id=?", (rid,)).rowcount:
            raise HTTPException(404, "not found")
    return {"ok": True}


# ---------- subscription finder ----------
# Merchants charged on a steady schedule at a steady price, not yet set up as recurring.
# Pattern first (free), then one AI yes/no per candidate merchant (cached) to drop
# regular-but-not-subscription places like your usual cafe.

SUB_PERIODS = [("weekly", 7, 2), ("biweekly", 14, 3), ("monthly", 30.4, 4)]  # name, days, tolerance
SUB_QUESTION = {"type": "predicate", "name": "subscription", "instructions": (
    "Is this merchant a subscription, membership or recurring bill that charges automatically on a "
    "schedule (streaming, software, cloud storage, phone or internet, gym membership, insurance, rent, "
    "loan payments)? Answer false for places someone just visits often, like a coffee shop, grocery "
    "store, restaurant, gas station or transit fare, even if the visits look regular.")}


def subscription_candidates(c) -> list[dict]:
    since = (today() - timedelta(days=400)).isoformat()
    rows = c.execute(f"SELECT * FROM ({TXQ}) WHERE ts >= ? AND amount_cents > 0 ORDER BY ts", (since,)).fetchall()
    recs = c.execute("SELECT name, amount_cents, freq FROM recurring WHERE active=1 AND kind='spend'").fetchall()
    covered = {merchant_key(r["name"]) for r in recs}
    dismissed = {r[0] for r in c.execute("SELECT merchant_key FROM subscription_checks WHERE dismissed=1")}
    by = {}
    for r in rows:
        by.setdefault(r["merchant_key"], []).append(r)
    out = []
    for key, txs in by.items():
        if key in dismissed or any(key == k or (len(k) > 3 and (k in key or key in k)) for k in covered):
            continue
        days = sorted({date.fromisoformat(t["ts"][:10]) for t in txs})
        if len(days) < 3 and not (len(days) == 2 and (days[1] - days[0]).days > 20):
            continue  # weekly/biweekly need 3+ charges; monthly can show up with 2
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        gaps.sort()
        median = gaps[len(gaps) // 2]
        period = next(((n, d, tol) for n, d, tol in SUB_PERIODS if abs(median - d) <= tol), None)
        if not period:
            continue
        name, plen, tol = period
        if sum(abs(g - plen) <= tol for g in gaps) < max(1, round(len(gaps) * .7)):
            continue  # irregular
        amounts = sorted(t["amount_cents"] for t in txs)
        mid = amounts[len(amounts) // 2]
        if any(abs(a - mid) > max(100, mid * .15) for a in amounts):
            continue  # price jumps around: not a fixed charge
        last = txs[-1]
        last_day = days[-1]
        if (today() - last_day).days > plen * 1.6 + tol:
            continue  # stopped: probably cancelled
        if any(r["freq"] == name and abs(r["amount_cents"] - last["amount_cents"]) <= max(5, r["amount_cents"] * .02)
               for r in recs):
            continue  # same price on the same schedule as a recurring entry: it's that one under another name
        out.append({"merchant_key": key, "name": last["merchant"], "freq": name, "amount": last["amount_cents"] / 100,
                    "charges": len(days), "first": days[0].isoformat(), "last": last_day.isoformat(),
                    "next": (last_day + timedelta(days=round(plen))).isoformat(),
                    "category": last["cat"], "card": last["card"]})
    return out


@app.get("/subscriptions/suggestions", dependencies=A)
def subscription_suggestions():
    with db() as c:
        cands = subscription_candidates(c)
        known = {r["merchant_key"]: r["probability"] for r in
                 c.execute("SELECT merchant_key, probability FROM subscription_checks WHERE probability IS NOT NULL")}
    todo = [s for s in cands if s["merchant_key"] not in known]
    answers = decide_many([(f"Merchant: {s['name']}\nCategory: {s['category']}\nCharged {s['charges']} times, "
                            f"{s['freq']}, about ${s['amount']:.2f} each", [SUB_QUESTION], "subscription")
                           for s in todo]) if OPENAI_API_KEY else []
    with db() as c:
        for s, a in zip(todo, answers):
            if a:
                p = float(a["subscription"].get("probability") or 0)
                known[s["merchant_key"]] = p
                c.execute("""INSERT INTO subscription_checks(merchant_key, probability) VALUES (?,?)
                             ON CONFLICT(merchant_key) DO UPDATE SET probability=excluded.probability,
                             checked_at=CURRENT_TIMESTAMP""", (s["merchant_key"], p))
    out = []
    for s in cands:
        p = known.get(s["merchant_key"])
        if p is not None and p < 0.5:
            continue  # AI: regular visits, not a subscription
        out.append({**s, "probability": p})
    return sorted(out, key=lambda s: (-(s["probability"] or 0.5), s["name"]))


@app.post("/subscriptions/{key}/dismiss", dependencies=A)
def dismiss_subscription(key: str):
    with db() as c:
        c.execute("""INSERT INTO subscription_checks(merchant_key, dismissed) VALUES (?, 1)
                     ON CONFLICT(merchant_key) DO UPDATE SET dismissed=1""", (key,))
    return {"ok": True}


@app.post("/subscriptions/{key}/accept", dependencies=A)
def accept_subscription(key: str):
    """Turn a suggestion into a recurring entry that logs from the NEXT charge on, so nothing
    already logged (by a tap or a statement) is logged twice."""
    with db() as c:
        s = next((x for x in subscription_candidates(c) if x["merchant_key"] == key), None)
    if not s:
        raise HTTPException(404, "no such suggestion")
    last, nxt = date.fromisoformat(s["last"]), date.fromisoformat(s["next"])
    body = {"name": s["name"], "amount": s["amount"], "kind": "spend", "freq": s["freq"], "card": s["card"],
            "category": s["category"] if s["category"] != OTHER else None}
    if s["freq"] == "monthly":
        cur = today().strftime("%Y-%m")
        # charged this month already -> start next month; otherwise this month's charge is still to come
        log_now = last.strftime("%Y-%m") != cur
        body.update(day=last.day, log_this_month=log_now)
    else:
        while nxt <= today():  # first occurrence after today; past ones were already logged
            nxt += timedelta(days=STEP_DAYS[s["freq"]])
        body["anchor"] = nxt.isoformat()
    r = add_recurring(RecurringIn(**body))
    with db() as c:  # done with this suggestion either way
        c.execute("""INSERT INTO subscription_checks(merchant_key, dismissed) VALUES (?, 1)
                     ON CONFLICT(merchant_key) DO UPDATE SET dismissed=1""", (key,))
    return {"ok": True, "recurring_id": r["id"], **body}


# ---------- income ----------

INCQ ="SELECT i.*, a.name AS account FROM income i LEFT JOIN accounts a ON a.id = i.account_id"


def inc_out(r):
    return {"id": r["id"], "ts": r["ts"], "date": r["ts"][:10], "amount": r["amount_cents"] / 100,
            "payer": r["payer"], "account_id": r["account_id"], "account": r["account"],
            "source": r["source"], "note": r["note"]}


def income_ts(raw: str | None) -> str:
    """A bare date means midday, so it can't slip a day across time zones."""
    if raw and len(raw.strip()) == 10:
        raw = raw.strip() + "T12:00:00"
    return parse_ts(raw)


def check_account(c, account_id):
    if account_id and not c.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise HTTPException(422, f"unknown account {account_id}")


class IncomeIn(BaseModel):
    amount: str | float
    payer: str
    ts: str | None = None
    account_id: int | None = None
    note: str = ""
    source: str = "manual"


@app.get("/income", dependencies=A)
def list_income(start: str | None = None, end: str | None = None, limit: int = 100, offset: int = 0):
    where, args = "", []
    if start or end:
        a, b = local_bounds(date.fromisoformat(start or "1970-01-01"), date.fromisoformat(end or "2999-12-31"))
        where, args = "WHERE ts >= ? AND ts < ?", [a, b]
    sql = f"SELECT * FROM ({INCQ}) {where}"
    with db() as c:
        agg = c.execute(f"SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM ({sql})", args).fetchone()
        rows = c.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"count": agg["n"], "total": agg["s"] / 100, "items": [inc_out(r) for r in rows]}


def income_account(c) -> int | None:
    """The account income lands in when none is given (Settings), if it still exists and is open."""
    r = c.execute("""SELECT a.id FROM settings s JOIN accounts a ON a.id = CAST(s.value AS INTEGER)
                     WHERE s.key='income_account' AND a.active=1""").fetchone()
    return r[0] if r else None


@app.post("/income", dependencies=A)
def add_income(i: IncomeIn):
    if not i.payer.strip():
        raise HTTPException(422, "payer required")
    try:
        cents, ts = parse_amount(i.amount), income_ts(i.ts)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if cents <= 0:
        raise HTTPException(422, "amount must be more than 0")
    with db() as c:
        check_account(c, i.account_id)
        cur = c.execute("""INSERT OR IGNORE INTO income(ts, amount_cents, payer, account_id, source, note)
                           VALUES (?,?,?,?,?,?)""",
                        (ts, cents, i.payer.strip(), i.account_id or income_account(c), i.source, i.note))
    new_id = cur.lastrowid if cur.rowcount == 1 else None
    return {"ok": True, "id": new_id, "duplicate": new_id is None}


class IncomePatch(BaseModel):
    amount: str | float | None = None
    payer: str | None = None
    ts: str | None = None
    account_id: int | None = None   # 0 clears it
    note: str | None = None


@app.patch("/income/{iid}", dependencies=A)
def edit_income(iid: int, p: IncomePatch):
    sets = {}
    try:
        if p.amount is not None: sets["amount_cents"] = parse_amount(p.amount)
        if p.ts is not None: sets["ts"] = income_ts(p.ts)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if p.payer is not None: sets["payer"] = p.payer.strip()
    if p.note is not None: sets["note"] = p.note
    if p.account_id is not None: sets["account_id"] = p.account_id or None
    with db() as c:
        if not c.execute("SELECT 1 FROM income WHERE id=?", (iid,)).fetchone():
            raise HTTPException(404, "not found")
        check_account(c, sets.get("account_id"))
        try:
            for k, v in sets.items():
                c.execute(f"UPDATE income SET {k}=? WHERE id=?", (v, iid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "an identical income entry already exists")
    return {"ok": True}


@app.delete("/income/{iid}", dependencies=A)
def delete_income(iid: int):
    with db() as c:
        if not c.execute("DELETE FROM income WHERE id=?", (iid,)).rowcount:
            raise HTTPException(404, "not found")
    return {"ok": True}


def income_between(c, d0: date, d1: date) -> tuple[int, int]:
    a, b = local_bounds(d0, d1)
    r = c.execute("SELECT COALESCE(SUM(amount_cents),0) s, COUNT(*) n FROM income WHERE ts >= ? AND ts < ?",
                  (a, b)).fetchone()
    return r["s"], r["n"]


# ---------- accounts + net worth ----------

# type -> is it a liability (balance = amount owed, subtracted from net worth)
ACCOUNT_TYPES = {"chequing": False, "savings": False, "investment": False, "cash": False,
                 "other_asset": False, "credit": True, "loan": True}


def balances_upto(c, day: str) -> dict[int, dict]:
    """Balance per account on `day`: the latest recorded balance on or before it, plus income
    paid into the account after that date (a paycheque into chequing raises it; into a card
    or loan it's a payment, so it lowers what's owed). Income dated the same day as the
    recorded balance is assumed to be in it already."""
    rows = c.execute(
        """SELECT b.account_id, b.day, b.balance_cents, a.type,
                  (SELECT COALESCE(SUM(i.amount_cents), 0) FROM income i
                    WHERE i.account_id = b.account_id
                      AND substr(i.ts, 1, 10) > b.day AND substr(i.ts, 1, 10) <= ?) AS income_since
           FROM balances b
           JOIN (SELECT account_id, MAX(day) AS d FROM balances WHERE day <= ? GROUP BY account_id) m
             ON m.account_id = b.account_id AND m.d = b.day
           JOIN accounts a ON a.id = b.account_id""", (day, day)).fetchall()
    out = {}
    for r in rows:
        sign = -1 if ACCOUNT_TYPES.get(r["type"]) else 1
        out[r["account_id"]] = {"day": r["day"], "cents": r["balance_cents"] + sign * r["income_since"],
                                "recorded": r["balance_cents"], "income_since": r["income_since"]}
    return out


def net_worth_at(c, accounts, day: str) -> dict:
    b = balances_upto(c, day)
    tracked = {r["id"] for r in c.execute("SELECT DISTINCT account_id AS id FROM balances")}
    assets = sum(v["cents"] for k, v in b.items() if k in accounts and not ACCOUNT_TYPES[accounts[k]])
    liabilities = sum(v["cents"] for k, v in b.items() if k in accounts and ACCOUNT_TYPES[accounts[k]])
    return {"assets": assets / 100, "liabilities": liabilities / 100, "net": (assets - liabilities) / 100,
            # some account had balances recorded, but none yet by this day
            "partial": any(a in tracked and a not in b for a in accounts), "has_data": bool(b)}


def check_type(t: str):
    if t not in ACCOUNT_TYPES:
        raise HTTPException(422, f"type must be one of {', '.join(ACCOUNT_TYPES)}")


class AccountIn(BaseModel):
    name: str
    type: str
    balance: str | float | None = None   # optional opening balance, as of today
    position: int | None = None


@app.get("/accounts", dependencies=A)
def list_accounts():
    t = today().isoformat()
    with db() as c:
        accs = c.execute("SELECT * FROM accounts ORDER BY active DESC, position, id").fetchall()
        b = balances_upto(c, t)
        types = {r["id"]: r["type"] for r in accs}
        nw = net_worth_at(c, types, t)
        # latest statement row per account, so the dashboard can show when a CSV is overdue
        imp = {r[0]: r[1] for r in c.execute("SELECT account_id, MAX(day) FROM bank_rows GROUP BY account_id")}
    items = [{"id": r["id"], "name": r["name"], "type": r["type"], "liability": ACCOUNT_TYPES[r["type"]],
              "active": bool(r["active"]), "position": r["position"],
              "balance": b[r["id"]]["cents"] / 100 if r["id"] in b else None,
              "recorded_balance": b[r["id"]]["recorded"] / 100 if r["id"] in b else None,
              "income_since": b[r["id"]]["income_since"] / 100 if r["id"] in b else 0,
              "as_of": b[r["id"]]["day"] if r["id"] in b else None,
              "imported_through": imp.get(r["id"])} for r in accs]
    return {"items": items, "assets": nw["assets"], "liabilities": nw["liabilities"], "net_worth": nw["net"],
            "types": list(ACCOUNT_TYPES)}


@app.post("/accounts", dependencies=A)
def add_account(a: AccountIn):
    name = a.name.strip()
    if not name:
        raise HTTPException(422, "name required")
    check_type(a.type)
    try:
        bal = parse_amount(a.balance) if a.balance not in (None, "") else None
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db() as c:
        pos = a.position if a.position is not None else \
            c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM accounts").fetchone()[0]
        try:
            aid = c.execute("INSERT INTO accounts(name, type, position) VALUES (?,?,?)",
                            (name, a.type, pos)).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "account exists")
        if bal is not None:
            c.execute("INSERT INTO balances(account_id, day, balance_cents) VALUES (?,?,?)",
                      (aid, today().isoformat(), bal))
    return {"ok": True, "id": aid}


class AccountPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    active: bool | None = None
    position: int | None = None


@app.patch("/accounts/{aid}", dependencies=A)
def edit_account(aid: int, p: AccountPatch):
    if p.type is not None:
        check_type(p.type)
    with db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            raise HTTPException(404, "not found")
        sets = {k: v for k, v in {"name": p.name.strip() if p.name else None, "type": p.type,
                                  "position": p.position}.items() if v is not None}
        if p.active is not None:
            sets["active"] = int(p.active)
        try:
            for k, v in sets.items():
                c.execute(f"UPDATE accounts SET {k}=? WHERE id=?", (v, aid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "account exists")
    return {"ok": True}


@app.delete("/accounts/{aid}", dependencies=A)
def delete_account(aid: int):
    """Removes the account and its balance history (to keep history, close it instead:
    record a final balance of 0 and PATCH active=false). Income keeps its amounts."""
    with db() as c:
        if not c.execute("DELETE FROM accounts WHERE id=?", (aid,)).rowcount:
            raise HTTPException(404, "not found")
        c.execute("DELETE FROM balances WHERE account_id=?", (aid,))
        c.execute("DELETE FROM bank_rows WHERE account_id=?", (aid,))
        c.execute("UPDATE income SET account_id=NULL WHERE account_id=?", (aid,))
        c.execute("UPDATE recurring SET account_id=NULL WHERE account_id=?", (aid,))
    return {"ok": True}


class BalanceIn(BaseModel):
    balance: str | float
    day: str | None = None   # YYYY-MM-DD, default today


@app.get("/accounts/{aid}/balances", dependencies=A)
def list_balances(aid: int):
    with db() as c:
        rows = c.execute("SELECT day, balance_cents FROM balances WHERE account_id=? ORDER BY day DESC",
                         (aid,)).fetchall()
    return [{"day": r["day"], "balance": r["balance_cents"] / 100} for r in rows]


@app.post("/accounts/{aid}/balances", dependencies=A)
def set_balance(aid: int, b: BalanceIn):
    """One balance per account per day; posting the same day again replaces it."""
    try:
        cents = parse_amount(b.balance)
        day = date.fromisoformat(b.day).isoformat() if b.day else today().isoformat()
    except ValueError as e:
        raise HTTPException(422, str(e))
    if day > today().isoformat():
        raise HTTPException(422, "balance date is in the future")
    with db() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
            raise HTTPException(404, "not found")
        c.execute("INSERT OR REPLACE INTO balances(account_id, day, balance_cents) VALUES (?,?,?)",
                  (aid, day, cents))
    return {"ok": True}


@app.delete("/accounts/{aid}/balances/{day}", dependencies=A)
def delete_balance(aid: int, day: str):
    with db() as c:
        if not c.execute("DELETE FROM balances WHERE account_id=? AND day=?", (aid, day)).rowcount:
            raise HTTPException(404, "not found")
    return {"ok": True}


@app.get("/stats/networth", dependencies=A)
def stats_networth(months: int = 12, end: str | None = None):
    """Net worth at each month end (or today, for the current month)."""
    months = max(1, min(months, 120))
    t = today()
    end = end or t.strftime("%Y-%m")
    out = []
    with db() as c:
        types = {r["id"]: r["type"] for r in c.execute("SELECT id, type FROM accounts")}
        for i in range(months - 1, -1, -1):
            ym = shift_month(end, -i)
            d0, d1 = month_range(ym)
            m = {"month": ym, "label": d0.strftime("%b"), "future": d0 > t}
            if not m["future"]:
                nw = net_worth_at(c, types, min(d1, t).isoformat())
                m.update(nw if nw["has_data"] else {"net": None, "partial": False})
            out.append(m)
    return {"months": out}


# ---------- bank statement import ----------
# Stateless two-step: /import/preview parses and classifies; /import/commit re-parses the same
# CSV text, applies the user's per-row choices, and writes everything in one transaction.

MAX_CSV_BYTES = 3_000_000
IMPORT_ACTIONS = {"purchase", "income", "matched", "skip", "payback"}
PAYBACK_DAYS = 45    # an e-Transfer can pay back a purchase from up to this many days earlier
IOU_DAYS = 120       # ...or settle an IOU this old


def bank_fingerprint(account_id: int, r: bankcsv.Row) -> str:
    key = f"{account_id}|{r.day}|{r.amount_cents}|{r.description.lower()}|{r.occurrence}"
    return hashlib.sha1(key.encode()).hexdigest()


def _words(s: str) -> set[str]:
    return {w for w in merchant_key(s).split() if len(w) > 2}


def match_purchase(r: bankcsv.Row, txs: list, used: set):
    """An already-logged purchase this statement row is (tap, manual, recurring, imported).
    Statements post 0-6 days after the tap. CAD amounts must match exactly; converted
    foreign purchases within 5% (the statement has the real rate)."""
    d = date.fromisoformat(r.day)
    best = None
    for t in txs:
        if t["id"] in used or t["fx_pending"]:
            continue
        lag = (d - date.fromisoformat(t["ts"][:10])).days
        if not -2 <= lag <= 6:
            continue
        diff = abs(t["amount_cents"] - r.amount_cents)
        if t["orig_currency"]:
            if diff > max(50, abs(r.amount_cents) * 0.05) or (t["amount_cents"] > 0) != (r.amount_cents > 0):
                continue
        elif diff:
            continue
        score = (len(_words(t["merchant"]) & _words(r.description)), -diff, -abs(lag))
        if best is None or score > best[0]:
            best = (score, t)
    return best[1] if best else None


def match_income(r: bankcsv.Row, account_id: int, incs: list, used: set):
    """A logged income entry (e.g. the recurring paycheque) this deposit is: within 4 days and
    3% (pay changes slightly; the statement's amount then replaces it)."""
    d, amt = date.fromisoformat(r.day), -r.amount_cents
    best = None
    for i in incs:
        if i["id"] in used or i["account_id"] not in (None, account_id):
            continue
        gap = abs((d - date.fromisoformat(i["ts"][:10])).days)
        diff = abs(i["amount_cents"] - amt)
        if gap > 4 or diff > amt * 0.03:
            continue
        score = (-diff, i["account_id"] == account_id, -gap)
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else None


IMPORT_ROW_INSTRUCTIONS = (
    "Classify this line from one person's bank or credit card statement (Vancouver, BC). Their own accounts: "
    "{accounts}. Moving money between those accounts is a transfer, not spending or income: paying off one of "
    "their credit cards, contributing to their TFSA/RRSP/FHSA or investments, moving to or from savings. An "
    "Interac e-Transfer TO another person (rent, splitting a bill, paying a friend) is spending. An e-Transfer "
    "FROM another person is usually them paying back their share (refund) unless it looks like pay or a gift. "
    "Use the description, direction and amount.")
ROW_CHOICES = {  # (direction, liability) -> choices; value -> (import action, reason)
    ("out", False): [("spending", "A purchase, bill, fee, or payment to another person or business (rent, utilities, an e-Transfer to someone)."),
                     ("own_transfer", "Money moved to another of their own accounts: savings, TFSA/RRSP/FHSA, investments, another bank."),
                     ("card_payment", "A payment toward one of their own credit cards.")],
    ("in", False): [("income", "Pay or salary, government benefits or tax credits, interest, a gift."),
                    ("refund", "Money back: a merchant refund, or someone paying them back for something they covered."),
                    ("own_transfer", "Money moved in from another of their own accounts.")],
    ("in", True): [("card_payment", "A payment made toward this credit card's balance."),
                   ("refund", "A merchant refund or credit for an earlier purchase.")],
}
ROW_ACTION = {"spending": ("purchase", "spending"), "refund": ("purchase", "refund / paid back"),
              "income": ("income", "income"), "own_transfer": ("skip", "transfer between your accounts"),
              "card_payment": ("skip", "credit card payment (its purchases come from the card)")}
PAYBACK_QUESTION = (
    "A friend sent this person an Interac e-Transfer. People in Vancouver often split dinners, drinks, tickets, "
    "trips and rides, then e-Transfer their share. Which of these recent purchases is it most likely paying back? "
    "Their share is usually half or an even split of the bill, sometimes plus tip, and arrives within days. "
    "Pick the purchase, or 'income' if it doesn't look like paying back any of them.")
SAME_PURCHASE = {"type": "predicate", "name": "same", "instructions": (
    "Are these the same purchase? The statement line is how the bank describes it, often with processor "
    "prefixes (SQ *, TST-, NBX*), store numbers and the city. The logged purchase came from Apple Pay or was "
    "entered by hand. Amounts and dates already line up; judge whether it's the same merchant.")}


def ai_review_statement(c, acc, items: list[dict], rows: dict) -> None:
    """Second opinion from the AI on the rows keywords are bad at, in place. Only rows not
    already matched or seen; failures leave the keyword answer."""
    liability = ACCOUNT_TYPES[acc["type"]]
    names = [f"{r['name']} ({r['type']})" for r in c.execute("SELECT name, type FROM accounts WHERE active=1")]
    instr = IMPORT_ROW_INSTRUCTIONS.format(accounts="; ".join(names) or "unknown")
    jobs, targets = [], []
    for it in items:
        r = rows[it["i"]]
        line = f"Account: {acc['name']} ({acc['type']})\nStatement line: {r.description}\n" \
               f"Money {'in' if r.amount_cents < 0 else 'out'}: ${abs(r.amount_cents) / 100:.2f} on {r.day}"
        if it["action"] == "matched" and it["match"]["kind"] == "tx" and not (
                _words(it["match"]["name"]) & _words(r.description)):
            m = it["match"]
            jobs.append((f"Statement line: {r.description}, ${abs(r.amount_cents) / 100:.2f} on {r.day}\n"
                         f"Logged purchase: {m['name']}, ${abs(m['amount']):.2f} on {m['ts'][:10]}", [SAME_PURCHASE], "same purchase"))
            targets.append((it, "same"))
        elif it["action"] == "income" and it.get("payback_options"):
            opts = it["payback_options"]
            jobs.append((f"Interac e-Transfer received from {it['person']}: ${-r.amount_cents / 100:.2f} on {r.day} "
                         f"(statement line: {r.description})",
                         [{"type": "choice", "name": "payback", "instructions": PAYBACK_QUESTION,
                           "choices": [{"value": o["target"], "description":
                                        f"{o['name']}, ${o['amount']:.2f} on {o['date']} ({o['category']})"} for o in opts]
                           + [{"value": "refund", "description": "Paying them back, but for something not listed (an older bill, a ticket bought elsewhere)."},
                              {"value": "income", "description": "Not a payback: pay, a gift, or a sale."}]}],
                         "payback"))
            targets.append((it, "payback"))
        elif it["action"] in ("purchase", "income", "skip") and (not liability or r.amount_cents < 0):
            key = ("in" if r.amount_cents < 0 else "out", liability)
            jobs.append((line, [{"type": "choice", "name": "kind", "instructions": instr,
                                 "choices": [{"value": v, "description": d} for v, d in ROW_CHOICES[key]]}], "import row"))
            targets.append((it, "kind"))
    for (it, what), a in zip(targets, decide_many(jobs)):
        if not a:
            continue
        if what == "payback":
            choice, conf = a["payback"]["choice"], float(a["payback"].get("confidence") or 0)
            it["ai"] = {"choice": choice, "confidence": conf}
            o = next((o for o in it["payback_options"] if o["target"] == choice), None)
            if o and conf >= 0.5:
                it.update(action="payback", ai_unsure=conf < AI_REVIEW_BELOW,
                          payback={**o, "person": it["person"]},
                          reason=f"AI: {it['person']} paying back their share of {o['name']} ({round(conf * 100)}%)")
            elif o:
                it["ai_unsure"] = True
                it["reason"] = f"AI: maybe paying back {o['name']} ({round(conf * 100)}% sure)"
            elif choice in ROW_ACTION:   # refund -> money back as a negative purchase, as before
                action, label = ROW_ACTION[choice]
                it.update(action=action, ai_unsure=conf < AI_REVIEW_BELOW, reason=f"AI: {label} ({round(conf * 100)}%)")
            continue
        if what == "same":
            p = float(a["same"].get("probability") or 0)
            if p < 0.5:  # a coincidence of amount and date: keep it as a new purchase
                it.update(action="purchase", reason=f"AI: not the logged “{it['match']['name']}” ({round(p * 100)}% same)",
                          match=None, fix=None, ai_unsure=p > 0.25)
            continue
        choice, conf = a["kind"]["choice"], float(a["kind"].get("confidence") or 0)
        action, label = ROW_ACTION[choice]
        it["ai"] = {"choice": choice, "confidence": conf}
        it["ai_unsure"] = conf < AI_REVIEW_BELOW
        if action != it["action"] or label not in it["reason"]:
            it.update(action=action, reason=f"AI: {label} ({round(conf * 100)}%)")
        elif it["ai_unsure"]:  # agrees with the keywords, but not confidently: say so
            it["reason"] += f" · AI {round(conf * 100)}% sure"


def match_iou(r: bankcsv.Row, ious: list, used: set):
    """An open IOU this e-Transfer settles: same amount (within 1%), owed before it arrived.
    The sender's name matching the IOU's person wins ties."""
    amt, best = -r.amount_cents, None
    who = _words(bankcsv.transfer_person(r.description))
    for s in ious:
        if s["id"] in used or s["ts"][:10] > r.day or abs(s["amount_cents"] - amt) > max(1, amt // 100):
            continue
        score = (bool(who & _words(s["person"])), -abs(s["amount_cents"] - amt), s["ts"])
        if best is None or score > best[0]:
            best = (score, s)
    return best[1] if best else None


def payback_options(r: bankcsv.Row, recent: list) -> list[dict]:
    """Purchases this e-Transfer could be paying back: before it, with room left to split off."""
    amt = -r.amount_cents
    lo = (date.fromisoformat(r.day) - timedelta(days=PAYBACK_DAYS)).isoformat()
    opts = [t for t in recent if lo <= t["ts"][:10] <= r.day and t["amount_cents"] >= amt]
    opts.sort(key=lambda t: t["ts"], reverse=True)
    return [{"target": f"tx:{t['id']}", "name": t["merchant"], "date": t["ts"][:10], "amount": t["full_cents"] / 100,
             "category": t["cat"]} for t in opts[:8]]


def classify_statement(c, acc, st: bankcsv.Statement, use_ai: bool = False) -> list[dict]:
    liability = ACCOUNT_TYPES[acc["type"]]
    seen = {r["fingerprint"]: r["action"] for r in
            c.execute("SELECT fingerprint, action FROM bank_rows WHERE account_id=?", (acc["id"],))}
    days = sorted(r.day for r in st.rows)
    a, b = local_bounds(date.fromisoformat(days[0]) - timedelta(days=7), date.fromisoformat(days[-1]) + timedelta(days=3))
    linked_tx = {r[0] for r in c.execute("SELECT tx_id FROM bank_rows WHERE tx_id IS NOT NULL")}
    linked_inc = {r[0] for r in c.execute("SELECT income_id FROM bank_rows WHERE income_id IS NOT NULL")}
    txs = [r for r in c.execute("SELECT id, ts, amount_cents, merchant, orig_currency, fx_pending FROM tx "
                                "WHERE ts >= ? AND ts < ?", (a, b)) if r["id"] not in linked_tx]
    incs = [r for r in c.execute("SELECT id, ts, amount_cents, payer, account_id FROM income "
                                 "WHERE ts >= ? AND ts < ?", (a, b)) if r["id"] not in linked_inc]
    used_tx, used_inc = set(), set()
    # e-Transfers in can be a friend paying back their share: open IOUs first, then recent purchases
    pa, pb = local_bounds(date.fromisoformat(days[0]) - timedelta(days=IOU_DAYS), date.fromisoformat(days[-1]))
    ious = c.execute(SHAREQ + " WHERE s.paid_day IS NULL AND t.ts >= ? AND t.ts < ?", (pa, pb)).fetchall()
    pa = local_bounds(date.fromisoformat(days[0]) - timedelta(days=PAYBACK_DAYS), date.fromisoformat(days[-1]))[0]
    recent = c.execute(f"SELECT * FROM ({TXQ}) WHERE ts >= ? AND ts < ? AND full_cents > 0 AND source != 'recurring'",
                       (pa, pb)).fetchall()
    used_iou = set()
    out = []
    for r in sorted(st.rows, key=lambda r: (r.day, r.i)):  # earlier rows claim earlier taps
        fp = bank_fingerprint(acc["id"], r)
        it = {"i": r.i, "date": r.day, "description": r.description, "amount": r.amount_cents / 100,
              "money_in": r.amount_cents < 0, "match": None, "fix": None, "reason": ""}
        if fp in seen:
            it.update(action="seen", reason=f"imported before ({seen[fp]})")
            out.append(it)
            continue
        desc = r.description
        if liability:  # credit card / loan: money out is spending; money in is a payment or a refund
            if r.amount_cents < 0 and bankcsv.has(desc, bankcsv.CARD_PAYMENT_RECEIVED):
                action, reason = "skip", "card payment"
            else:
                action, reason = "purchase", "" if r.amount_cents > 0 else "refund"
        elif r.amount_cents > 0:
            if bankcsv.is_card_payment(desc):
                action, reason = "skip", "credit card payment (its purchases come from the card)"
            elif bankcsv.is_own_transfer(desc):
                action, reason = "skip", "transfer between your accounts"
            else:
                action, reason = "purchase", ""
        else:
            if bankcsv.is_own_transfer(desc):
                action, reason = "skip", "transfer between your accounts"
            else:
                action, reason = "income", ""
        if action == "purchase":
            m = match_purchase(r, txs, used_tx)
            if m:
                used_tx.add(m["id"])
                action = "matched"
                it["match"] = {"kind": "tx", "id": m["id"], "ts": m["ts"], "name": m["merchant"],
                               "amount": m["amount_cents"] / 100}
                if m["amount_cents"] != r.amount_cents:
                    it["fix"] = r.amount_cents / 100
        elif action == "income" and bankcsv.E_TRANSFER.search(desc):
            iou = match_iou(r, ious, used_iou)
            if iou:
                used_iou.add(iou["id"])
                action, reason = "payback", f"settles what {iou['person']} owed for {iou['merchant']}"
                it["payback"] = {"target": f"share:{iou['id']}", "person": iou["person"], "name": iou["merchant"],
                                 "date": iou["ts"][:10], "amount": iou["amount_cents"] / 100}
            it["payback_options"] = payback_options(r, recent)
            it["person"] = bankcsv.transfer_person(desc)
        if action == "income":
            m = match_income(r, acc["id"], incs, used_inc)
            if m:
                used_inc.add(m["id"])
                action = "matched"
                it["match"] = {"kind": "income", "id": m["id"], "ts": m["ts"], "name": m["payer"],
                               "amount": m["amount_cents"] / 100}
                if m["amount_cents"] != -r.amount_cents:
                    it["fix"] = -r.amount_cents / 100
        it.update(action=action, reason=reason)
        out.append(it)
    if use_ai and OPENAI_API_KEY:
        ai_review_statement(c, acc, [it for it in out if it["action"] != "seen"], {r.i: r for r in st.rows})
    return sorted(out, key=lambda x: x["i"])


def closing_balance(st: bankcsv.Statement) -> tuple[str, int] | None:
    """(day, balance) of the chronologically last row that carries a balance."""
    rows = [r for r in st.rows if r.balance_cents is not None]
    if not rows:
        return None
    last_day = max(r.day for r in rows)
    same = [r for r in rows if r.day == last_day]
    r = same[0] if st.newest_first else same[-1]
    return r.day, r.balance_cents


def settle_payback(c, target: str, r: bankcsv.Row, fp: str, i: int) -> int:
    """Record an e-Transfer as someone's share of a purchase: settle an open IOU, or split a new
    share off the purchase. Returns the purchase id."""
    kind, _, sid = target.partition(":")
    amt = -r.amount_cents
    if kind == "share" and sid.isdigit():
        s = c.execute("SELECT * FROM shares WHERE id=? AND paid_day IS NULL", (int(sid),)).fetchone()
        if not s:
            raise HTTPException(422, f"row {i}: that IOU is gone or already paid")
        # what they actually sent is their share, as long as it fits in the purchase
        cents = min(amt, shares_room(c, s["tx_id"], excluding=s["id"]))
        c.execute("UPDATE shares SET paid_day=?, bank_fp=?, amount_cents=? WHERE id=?", (r.day, fp, cents, s["id"]))
        return s["tx_id"]
    if kind == "tx" and sid.isdigit():
        if amt > shares_room(c, int(sid)):
            raise HTTPException(422, f"row {i}: the payback is more than what's left of that purchase")
        c.execute("INSERT INTO shares(tx_id, person, amount_cents, paid_day, bank_fp) VALUES (?,?,?,?,?)",
                  (int(sid), bankcsv.transfer_person(r.description), amt, r.day, fp))
        return int(sid)
    raise HTTPException(422, f"row {i}: bad payback target {target!r}")


class ImportIn(BaseModel):
    account_id: int
    csv: str
    flip: bool = False             # amounts came out backwards
    date_order: str = "auto"       # auto | mdy | dmy
    actions: dict[int, str] = {}   # commit: row i -> purchase | income | matched | skip | payback
    paybacks: dict[int, str] = {}  # commit: row i -> "share:<id>" (settles an IOU) | "tx:<id>" (new share)


def load_statement(c, p: ImportIn):
    if len(p.csv.encode()) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV is larger than 3 MB")
    acc = c.execute("SELECT * FROM accounts WHERE id=?", (p.account_id,)).fetchone()
    if not acc:
        raise HTTPException(404, "account not found")
    try:
        st = bankcsv.parse_statement(p.csv, flip=p.flip, date_order=p.date_order)
    except ValueError as e:
        raise HTTPException(422, f"Couldn't read this CSV: {e}")
    return acc, st


@app.post("/import/preview", dependencies=A)
def import_preview(p: ImportIn):
    with db() as c:
        acc, st = load_statement(c, p)
        items = classify_statement(c, acc, st, use_ai=True)  # commit applies the reviewed actions as sent
        latest = c.execute("SELECT MAX(day) FROM balances WHERE account_id=?", (acc["id"],)).fetchone()[0]
    bal = closing_balance(st) if not ACCOUNT_TYPES[acc["type"]] else None
    counts = {}
    for it in items:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    return {"account": acc["name"], "liability": ACCOUNT_TYPES[acc["type"]], "columns": st.columns,
            "date_order": st.date_order, "ambiguous_dates": st.ambiguous_dates, "sign": st.sign,
            "rows": items, "counts": counts, "warnings": st.warnings[:20],
            "first_day": min(r.day for r in st.rows), "last_day": max(r.day for r in st.rows),
            # recorded either way (history for the net worth chart); a newer balance stays current
            "balance": {"day": bal[0], "amount": bal[1] / 100,
                        "newer_balance_day": latest if latest and latest > bal[0] else None} if bal else None}


@app.post("/import/commit", dependencies=A)
def import_commit(p: ImportIn, bg: BackgroundTasks):
    with db() as c:
        acc, st = load_statement(c, p)
        items = classify_statement(c, acc, st)
        rows = {r.i: r for r in st.rows}
        plan = []
        for it in items:
            if it["action"] == "seen":
                continue
            act = p.actions.get(it["i"], it["action"])
            if act not in IMPORT_ACTIONS:
                raise HTTPException(422, f"row {it['i']}: unknown action {act!r}")
            if act == "matched" and not it["match"]:
                raise HTTPException(422, f"row {it['i']}: nothing to match")
            if act in ("income", "payback") and not it["money_in"]:
                raise HTTPException(422, f"row {it['i']}: money out can't be {act}")
            if act == "payback" and not (p.paybacks.get(it["i"]) or (it.get("payback") or {}).get("target")):
                raise HTTPException(422, f"row {it['i']}: which purchase is it paying back?")
            plan.append((it, act))

        done = {"purchase": 0, "income": 0, "matched": 0, "skip": 0, "fixed": 0, "payback": 0}
        for it, act in plan:
            r = rows[it["i"]]
            # noon, with the account in the minutes: the same day/amount/description on two
            # different cards must stay two purchases (the dedup index ignores the card)
            ts = parse_ts(f"{r.day}T12:{acc['id'] % 60:02d}:{r.occurrence % 60:02d}")
            tx_id = inc_id = None
            if act == "purchase":
                money = {"amount_cents": r.amount_cents, "orig_amount_cents": None, "orig_currency": None,
                         "fx_rate": None, "fx_pending": 0}
                tx_id = insert(c, ts, money, r.description, acc["name"], source="bank")
                if tx_id is None:  # an identical row exists already (same time, amount and name)
                    tx_id = c.execute("SELECT id FROM tx WHERE ts=? AND COALESCE(orig_amount_cents, amount_cents)=? "
                                      "AND merchant=?", (ts, r.amount_cents, r.description.strip() or "Unknown")).fetchone()["id"]
            elif act == "income":
                cur = c.execute("""INSERT OR IGNORE INTO income(ts, amount_cents, payer, account_id, source)
                                   VALUES (?,?,?,?, 'bank')""", (ts, -r.amount_cents, r.description, acc["id"]))
                inc_id = cur.lastrowid if cur.rowcount == 1 else c.execute(
                    "SELECT id FROM income WHERE ts=? AND amount_cents=? AND payer=?",
                    (ts, -r.amount_cents, r.description)).fetchone()["id"]
            elif act == "payback":
                tx_id = settle_payback(c, p.paybacks.get(it["i"]) or it["payback"]["target"], r,
                                       bank_fingerprint(acc["id"], r), it["i"])
            elif act == "matched":
                m = it["match"]
                if m["kind"] == "tx":
                    tx_id = m["id"]
                    if it["fix"] is not None:  # the statement's CAD amount replaces the estimate
                        c.execute("UPDATE tx SET amount_cents=?, fx_rate=NULL, fx_pending=0 WHERE id=?",
                                  (r.amount_cents, tx_id))
                else:
                    inc_id = m["id"]
                    if it["fix"] is not None:
                        c.execute("UPDATE income SET amount_cents=? WHERE id=?", (-r.amount_cents, inc_id))
                done["fixed"] += it["fix"] is not None
            c.execute("""INSERT OR IGNORE INTO bank_rows(fingerprint, account_id, day, amount_cents, description,
                         action, tx_id, income_id) VALUES (?,?,?,?,?,?,?,?)""",
                      (bank_fingerprint(acc["id"], r), acc["id"], r.day, r.amount_cents, r.description,
                       act, tx_id, inc_id))
            done[act] += 1

        bal = closing_balance(st) if not ACCOUNT_TYPES[acc["type"]] else None
        if bal and bal[0] <= today().isoformat():
            c.execute("INSERT OR REPLACE INTO balances(account_id, day, balance_cents) VALUES (?,?,?)",
                      (acc["id"], bal[0], bal[1]))
    bg.add_task(enrich_pending)
    return {"ok": True, **done, "balance": {"day": bal[0], "amount": bal[1] / 100} if bal else None}


# ---------- budgets ----------

BUDGET_HISTORY = 6   # complete months used for the "typical" suggestion


def budget_history(c) -> dict:
    """Spending per category over the last BUDGET_HISTORY complete months, counting only months
    the tracker has data for. `typical` is the median, so a one-off (the car in April) doesn't
    inflate it; `average` is there for comparison."""
    first = c.execute("SELECT MIN(ts) FROM tx").fetchone()[0]
    now = today().strftime("%Y-%m")
    have = {r[0] for r in c.execute("SELECT DISTINCT substr(ts, 1, 7) FROM tx")}   # months with any data
    months = [m for m in (shift_month(now, -i) for i in range(BUDGET_HISTORY, 0, -1))
              if first and m >= first[:7] and m in have]
    per = {}   # category -> [cents per month]
    inc = []
    for i, ym in enumerate(months):
        d0, d1 = month_range(ym)
        for r in rows_between(c, d0, d1):
            per.setdefault(r["cat"], [0] * len(months))[i] += r["amount_cents"]
        inc.append(income_between(c, d0, d1)[0])
    out = {}
    for cat, vals in per.items():
        out[cat] = {"typical": statistics.median(vals) / 100, "average": sum(vals) / len(vals) / 100,
                    "last": vals[-1] / 100}
    return {"months": months, "cats": out, "income_avg": sum(inc) / len(inc) / 100 if inc else 0}


@app.get("/budgets", dependencies=A)
def get_budgets():
    with db() as c:
        h = budget_history(c)
        cats = c.execute("SELECT * FROM categories ORDER BY position").fetchall()
        total = budget(c)
    none = {"typical": 0, "average": 0, "last": 0}
    items = [{"name": r["name"], "color": r["color"],
              "budget": r["budget_cents"] / 100 if r["budget_cents"] is not None else None,
              **{k: round(v, 2) for k, v in h["cats"].get(r["name"], none).items()}} for r in cats]
    return {"total": total or None, "items": items, "months": h["months"],
            "income_avg": round(h["income_avg"], 2)}


class BudgetsIn(BaseModel):
    total: float | None = None                       # overall monthly budget; null clears it
    categories: dict[str, float | None] = {}         # name -> monthly budget; null clears; missing = unchanged


@app.put("/budgets", dependencies=A)
def put_budgets(b: BudgetsIn):
    if (b.total is not None and b.total < 0) or any(v is not None and v < 0 for v in b.categories.values()):
        raise HTTPException(422, "budgets can't be negative")
    with db() as c:
        known = {r[0] for r in c.execute("SELECT name FROM categories")}
        if unknown := set(b.categories) - known:
            raise HTTPException(422, f"unknown categories: {', '.join(sorted(unknown))}")
        if "total" in b.model_fields_set:
            if b.total:
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('monthly_budget', ?)", (str(b.total),))
            else:
                c.execute("DELETE FROM settings WHERE key='monthly_budget'")
        for name, v in b.categories.items():
            c.execute("UPDATE categories SET budget_cents=? WHERE name=?", (round(v * 100) if v else None, name))
    return {"ok": True}


# ---------- settings ----------

class SettingsIn(BaseModel):
    monthly_budget: float | None = None
    income_account: int | None = None   # only changed when sent; 0 or null clears it


@app.get("/settings", dependencies=A)
def get_settings(request: Request):
    with db() as c:
        return {"monthly_budget": budget(c) or None, "income_account": income_account(c), "ai_enabled": ai_enabled(),
                "ai_model": f"OpenAI Decisions ({DECISIONS_MODEL})" if OPENAI_API_KEY else AI_MODEL,
                "ai_review_below": AI_REVIEW_BELOW, "trusted_device": is_trusted(request),
                "weekly_summary": bool(NTFY_URL)}


@app.put("/settings", dependencies=A)
def put_settings(s: SettingsIn):
    with db() as c:
        if "monthly_budget" not in s.model_fields_set:
            pass   # the Budgets editor owns it now; Settings leaves it alone
        elif s.monthly_budget is None:
            c.execute("DELETE FROM settings WHERE key='monthly_budget'")
        else:
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('monthly_budget', ?)",
                      (str(s.monthly_budget),))
        if "income_account" in s.model_fields_set:
            if s.income_account:
                check_account(c, s.income_account)
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('income_account', ?)",
                          (str(s.income_account),))
            else:
                c.execute("DELETE FROM settings WHERE key='income_account'")
    return {"ok": True}


# ---------- stats ----------

def rows_between(c, d0: date, d1: date):
    a, b = local_bounds(d0, d1)
    return c.execute(f"SELECT * FROM ({TXQ}) WHERE ts >= ? AND ts < ? ORDER BY ts", (a, b)).fetchall()


@app.get("/stats/month", dependencies=A)
def stats_month(month: str | None = None):
    now = today()
    ym = month or now.strftime("%Y-%m")
    d0, d1 = month_range(ym)
    is_current = d0 <= now <= d1
    is_future = d0 > now
    upto = now if is_current else d1
    p0, p1 = month_range(shift_month(ym, -1))
    p_upto = p0.replace(day=min(upto.day, p1.day))

    with db() as c:
        rows = rows_between(c, d0, d1)
        prev = rows_between(c, p0, p1)
        cats = c.execute("SELECT * FROM categories ORDER BY position").fetchall()
        total_budget = budget(c)
        inc_cents, inc_count = income_between(c, d0, d1)
        prev_inc_cents, _ = income_between(c, p0, p1)

    total = sum(r["amount_cents"] for r in rows) / 100
    income = inc_cents / 100
    net = income - total
    days = {}
    for r in rows:
        d = days.setdefault(r["ts"][:10], {"total": 0, "count": 0})
        d["total"] += r["amount_cents"] / 100
        d["count"] += 1
    day_list = []
    for i in range(d1.day):
        dd = d0 + timedelta(days=i)
        v = days.get(dd.isoformat(), {"total": 0, "count": 0})
        day_list.append({"date": dd.isoformat(), "total": round(v["total"], 2), "count": v["count"],
                         "future": dd > now})

    by_cat, prev_cat = {}, {}
    for r in rows:
        by_cat.setdefault(r["cat"], [0, 0, 0])
        by_cat[r["cat"]][0] += r["amount_cents"]
        by_cat[r["cat"]][1] += 1
        if r["source"] == "recurring":   # scheduled bills: excluded from the budget pace check
            by_cat[r["cat"]][2] += r["amount_cents"]
    for r in prev:
        prev_cat[r["cat"]] = prev_cat.get(r["cat"], 0) + r["amount_cents"]
    cat_list = []
    for cr in cats:
        tot, cnt, fixed = by_cat.get(cr["name"], [0, 0, 0])
        b = cr["budget_cents"]
        if tot == 0 and not b and not prev_cat.get(cr["name"]):
            continue
        cat_list.append({"name": cr["name"], "color": cr["color"], "total": tot / 100, "count": cnt,
                         "recurring": fixed / 100,
                         "budget": b / 100 if b is not None else None,
                         "prev_total": prev_cat.get(cr["name"], 0) / 100})
    cat_list.sort(key=lambda x: x["total"], reverse=True)

    merchants = {}
    for r in rows:
        m = merchants.setdefault(r["merchant"], {"name": r["merchant"], "total": 0, "count": 0,
                                                  "color": r["color"], "category": r["cat"]})
        m["total"] += r["amount_cents"] / 100
        m["count"] += 1

    elapsed = (upto - d0).days + 1 if not is_future else 0
    daily_avg = total / elapsed if elapsed else 0
    prev_total = sum(r["amount_cents"] for r in prev) / 100
    prev_same_point = sum(r["amount_cents"] for r in prev if r["ts"][:10] <= p_upto.isoformat()) / 100

    return {
        "month": ym,
        "label": d0.strftime("%B %Y"),
        "is_current": is_current,
        "total": round(total, 2),
        "count": len(rows),
        "budget": total_budget or None,
        "daily_avg": round(daily_avg, 2),
        "projected": round(daily_avg * d1.day, 2) if is_current else None,
        "prev_total": round(prev_total, 2),
        "prev_same_point": round(prev_same_point, 2),
        "prev_label": p0.strftime("%B"),
        "income": round(income, 2),
        "income_count": inc_count,
        "prev_income": round(prev_inc_cents / 100, 2),
        "net": round(net, 2),
        "savings_rate": round(net / income * 100, 1) if income > 0 else None,
        "days": day_list,
        "categories": cat_list,
        "top_merchants": sorted(merchants.values(), key=lambda m: m["total"], reverse=True)[:8],
        "review_count": sum(1 for r in rows if needs_review(r)),
        # needs vs wants (refunds net out); unrated = merchant and category both undecided
        "essential": round(sum(r["amount_cents"] for r in rows if r["need"] == 1) / 100, 2),
        "optional": round(sum(r["amount_cents"] for r in rows if r["need"] == 0) / 100, 2),
        "unrated": round(sum(r["amount_cents"] for r in rows if r["need"] is None) / 100, 2),
        "prev_optional": round(sum(r["amount_cents"] for r in prev if r["need"] == 0) / 100, 2),
    }


@app.get("/stats/trend", dependencies=A)
def stats_trend(months: int = 6, end: str | None = None):
    end = end or today().strftime("%Y-%m")
    out = []
    with db() as c:
        cats = {r["name"]: r["color"] for r in c.execute("SELECT name, color FROM categories")}
        for i in range(months - 1, -1, -1):
            ym = shift_month(end, -i)
            d0, d1 = month_range(ym)
            rows = rows_between(c, d0, d1)
            by = {}
            for r in rows:
                by[r["cat"]] = by.get(r["cat"], 0) + r["amount_cents"] / 100
            total = round(sum(by.values()), 2)
            income = income_between(c, d0, d1)[0] / 100
            out.append({"month": ym, "label": d0.strftime("%b"), "total": total,
                        "income": round(income, 2), "net": round(income - total, 2),
                        "by_category": {k: round(v, 2) for k, v in by.items()}})
    return {"months": out, "colors": cats}


@app.get("/summary", dependencies=A)
def summary():
    now = datetime.now(TZ)
    t = now.date()
    ym = t.strftime("%Y-%m")
    d0, d1 = month_range(ym)

    with db() as c:
        month_rows = rows_between(c, d0, t)
        week_rows = rows_between(c, t - timedelta(days=6), t)
        hist = rows_between(c, t - timedelta(days=56), t - timedelta(days=1))
        recent = c.execute(f"SELECT * FROM ({TXQ}) ORDER BY ts DESC, id DESC LIMIT 5").fetchall()
        total_budget = budget(c)
        income_month = income_between(c, d0, t)[0] / 100

    month_total = sum(r["amount_cents"] for r in month_rows) / 100
    today_total = sum(r["amount_cents"] for r in month_rows if r["ts"][:10] == t.isoformat()) / 100
    daily_avg = month_total / t.day

    # usual spend on this weekday: mean over the last 8 same weekdays
    same = [t - timedelta(days=7 * k) for k in range(1, 9)]
    per_day = {}
    for r in hist:
        per_day[r["ts"][:10]] = per_day.get(r["ts"][:10], 0) + r["amount_cents"] / 100
    usual = sum(per_day.get(d.isoformat(), 0) for d in same) / len(same)

    last7 = []
    for i in range(6, -1, -1):
        d = t - timedelta(days=i)
        last7.append({"date": d.isoformat(), "label": d.strftime("%a")[0],
                      "total": round(sum(r["amount_cents"] for r in week_rows
                                         if r["ts"][:10] == d.isoformat()) / 100, 2)})

    by_cat = {}
    for r in month_rows:
        e = by_cat.setdefault(r["cat"], {"name": r["cat"], "total": 0, "color": r["color"]})
        e["total"] += r["amount_cents"] / 100
    by_merchant = {}
    for r in month_rows:
        by_merchant[r["merchant"]] = by_merchant.get(r["merchant"], 0) + r["amount_cents"] / 100

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "month_label": t.strftime("%B"),
        "today": round(today_total, 2),
        "usual_today": round(usual, 2),
        "month": round(month_total, 2),
        "budget": total_budget or None,
        "budget_pct": round(month_total / total_budget * 100, 1) if total_budget else None,
        "daily_avg": round(daily_avg, 2),
        "projected": round(daily_avg * d1.day, 2),
        "count_month": len(month_rows),
        "income_month": round(income_month, 2),
        "net_month": round(income_month - month_total, 2),
        "last7": last7,
        "top_categories": [{"name": v["name"], "total": round(v["total"], 2), "color": v["color"]}
                           for v in sorted(by_cat.values(), key=lambda v: v["total"], reverse=True)[:3]],
        "top_merchants": [{"name": k, "total": round(v, 2)} for k, v in
                          sorted(by_merchant.items(), key=lambda kv: kv[1], reverse=True)[:3]],
        "recent": [row_out(r) for r in recent],
    }


# ---------- weekly summary ----------

def dollars(cents_or_dollars: float) -> str:
    v = round(cents_or_dollars)
    return f"{'-' if v < 0 else ''}${abs(v):,}"


def upcoming_recurring(c, d0: date, d1: date) -> list[tuple[date, str, int, str]]:
    """Recurring entries due in [d0, d1]: (day, name, cents, kind), by date."""
    out = []
    for r in c.execute("SELECT * FROM recurring WHERE active=1"):
        if r["freq"] in STEP_DAYS:
            step = timedelta(days=STEP_DAYS[r["freq"]])
            d = date.fromisoformat(r["last_date"]) + step if r["last_date"] else date.fromisoformat(r["anchor"])
            while d < d0:
                d += step
            while d <= d1:
                out.append((d, r["name"], r["amount_cents"], r["kind"]))
                d += step
        else:
            for ym in sorted({d0.strftime("%Y-%m"), d1.strftime("%Y-%m")}):
                if r["last_run"] and ym <= r["last_run"]:
                    continue   # already logged (or skipped) for that month
                m0, m1 = month_range(ym)
                d = m0.replace(day=min(r["day"], m1.day))
                if d0 <= d <= d1:
                    out.append((d, r["name"], r["amount_cents"], r["kind"]))
    return sorted(out)


def weekly_summary(end: date | None = None) -> dict:
    """The week Monday..`end` (a Sunday by default: the last one on or before today)."""
    t = today()
    end = end or t - timedelta(days=(t.weekday() - 6) % 7)
    start = end - timedelta(days=6)
    with db() as c:
        week = rows_between(c, start, end)
        weeks = [rows_between(c, start - timedelta(days=7 * k), start - timedelta(days=7 * k - 6)) for k in range(1, 9)]
        weeks = [w for w in weeks if w]   # a week with no purchases at all is a gap in the data, not $0
        typical = statistics.median(sum(r["amount_cents"] for r in w) for w in weeks) / 100 if len(weeks) >= 3 else None
        coming = upcoming_recurring(c, end + timedelta(days=1), end + timedelta(days=7))
        # the month as it stood at `end` (a past week's preview mustn't count later purchases)
        d0, d1 = month_range(end.strftime("%Y-%m"))
        month_rows = rows_between(c, d0, end)
        budgets = {r["name"]: r["budget_cents"] / 100 for r in c.execute("SELECT name, budget_cents FROM categories")
                   if r["budget_cents"]}
        total_budget = budget(c)
    total = sum(r["amount_cents"] for r in week) / 100
    owed = list_shares()
    accs = list_accounts()["items"]

    lines = []
    if typical is not None:
        diff = total - typical
        lines.append(f"{'▲' if diff > 0 else '▼'} {dollars(abs(diff))} {'more' if diff > 0 else 'less'} than your usual week ({dollars(typical)})"
                     if abs(diff) >= 10 else f"About your usual week ({dollars(typical)})")
    by_cat = {}
    for r in week:
        by_cat[r["cat"]] = by_cat.get(r["cat"], 0) + r["amount_cents"]
    top = [f"{k} {dollars(v / 100)}" for k, v in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)[:3] if v > 0]
    if top:
        lines.append("Top: " + ", ".join(top))
    big = max(week, key=lambda r: r["amount_cents"], default=None)
    if big and big["amount_cents"] > 0:
        lines.append(f"Biggest: {big['merchant']} {dollars(big['amount_cents'] / 100)}")

    # the month so far, against the budgets
    pace = ((end - d0).days + 1) / d1.day
    m_total = sum(r["amount_cents"] for r in month_rows) / 100
    mline = f"{d0.strftime('%B')}: {dollars(m_total)}"
    if total_budget:
        mline += f" of {dollars(total_budget)} ({round(m_total / total_budget * 100)}%, {d1.day - end.day} days left)"
    lines.append(mline)
    cats = {}
    for r in month_rows:
        x = cats.setdefault(r["cat"], {"name": r["cat"], "total": 0, "recurring": 0})
        x["total"] += r["amount_cents"] / 100
        if r["source"] == "recurring":
            x["recurring"] += r["amount_cents"] / 100
    for name, b in budgets.items():
        cats.setdefault(name, {"name": name, "total": 0, "recurring": 0})["budget"] = b
    cats = [x for x in cats.values() if x.get("budget")]
    over = [c for c in cats if c["total"] > c["budget"]]
    ahead = [c for c in cats if c["total"] <= c["budget"]
             and c["total"] - c["recurring"] > (c["budget"] - c["recurring"]) * pace * 1.1]
    if over:
        lines.append("Over budget: " + ", ".join(f"{c['name']} {dollars(c['total'])}/{dollars(c['budget'])}" for c in over))
    if ahead:
        lines.append("Ahead of pace: " + ", ".join(f"{c['name']} {dollars(c['total'])}/{dollars(c['budget'])}" for c in ahead))

    # things to do
    if owed["owed"]:
        people = sorted({i["person"] for i in owed["items"] if not i["paid"]})
        lines.append(f"Owed to you: {dollars(owed['owed'])} ({', '.join(people[:3])}{'…' if len(people) > 3 else ''})")
    review = sum(1 for r in month_rows if needs_review(r))
    if review:
        lines.append(f"{review} AI categor{'y' if review == 1 else 'ies'} to confirm")
    due = [a["name"] for a in accs if a["active"] and a["imported_through"]
           and (end - date.fromisoformat(a["imported_through"])).days > 35]
    if due:
        lines.append("CSV due: " + ", ".join(due))
    spend_next = [x for x in coming if x[3] == "spend"]
    if spend_next:
        lines.append("Next week: " + ", ".join(f"{n} {dollars(cents / 100)} {d.strftime('%a')}" for d, n, cents, _ in spend_next[:4]))

    title = f"Week of {start.strftime('%b')} {start.day}–{end.day if end.month == start.month else end.strftime('%b %-d')}: {dollars(total)} spent"
    return {"start": start.isoformat(), "end": end.isoformat(), "total": round(total, 2),
            "typical": round(typical, 2) if typical is not None else None, "title": title, "lines": lines}


def send_ntfy(title: str, message: str) -> None:
    if not NTFY_URL:
        raise RuntimeError("NTFY_URL isn't set")
    # JSON publishing keeps the title UTF-8 safe (headers would have to be latin-1)
    r = httpx.post(NTFY_URL, json={"topic": NTFY_TOPIC, "title": title, "message": message,
                                   "click": PUBLIC_URL, "tags": ["moneybag"]},
                   headers={"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}, timeout=15)
    r.raise_for_status()


def maybe_send_weekly(now: datetime | None = None) -> bool:
    """Send once per week, from Sunday 7pm; if the server was down then, catch up until Monday noon."""
    if not NTFY_URL:
        return False
    now = now or datetime.now(TZ)
    sunday = now.date() - timedelta(days=(now.weekday() - WEEKLY_DAY) % 7)
    due = datetime.combine(sunday, datetime.min.time(), TZ).replace(hour=WEEKLY_HOUR)
    if now < due or now > due + timedelta(hours=17):
        return False
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='weekly_sent'").fetchone()
    if r and r["value"] >= sunday.isoformat():
        return False
    s = weekly_summary(sunday)
    send_ntfy(s["title"], "\n".join(s["lines"]))
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('weekly_sent', ?)", (sunday.isoformat(),))
    log.info("weekly summary sent for %s", sunday)
    return True


@app.get("/summary/weekly", dependencies=A)
def get_weekly(end: str | None = None):
    return weekly_summary(date.fromisoformat(end) if end else None)


@app.post("/summary/weekly/send", dependencies=A)
def send_weekly_now():
    """Send the latest Mon-Sun week's summary now (to test the notification)."""
    s = weekly_summary()
    try:
        send_ntfy(s["title"], "\n".join(s["lines"]))
    except (RuntimeError, httpx.HTTPError) as e:
        raise HTTPException(502, f"couldn't send: {e}")
    return {"ok": True, **s}


# ---------- dashboard + health ----------

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True}
