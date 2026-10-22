"""Bank / credit card statement CSV parsing. Pure functions, no database.

Banks disagree on everything: TD exports have no header row, Simplii uses "Funds Out" /
"Funds In", Wealthsimple a single signed amount, RBC "CAD$", and slash dates can be
MM/DD or DD/MM. parse_statement() sniffs all of that and returns rows with one sign
convention: amount_cents > 0 is money OUT of the account (a purchase, a payment made),
< 0 is money IN (income, a refund, a card payment received).
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
YMD = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$")
XYZ = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?:[ T].*)?$")
MDY_TEXT = re.compile(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$")
DMY_TEXT = re.compile(r"(\d{1,2})[-\s]([A-Za-z]{3})[A-Za-z]*\.?[-\s,]+(\d{4}|\d{2})$")
NUM = re.compile(r"\(?\s*[-+]?\s*(?:CA\$|C\$|\$)?\s*-?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*(?:CAD)?\s*-?", re.I)


@dataclass
class Row:
    i: int                      # data row number in the file, from 0
    day: str                    # YYYY-MM-DD
    description: str
    amount_cents: int           # > 0 money out, < 0 money in
    balance_cents: int | None = None
    occurrence: int = 0         # nth identical (day, amount, description) row in this file


@dataclass
class Statement:
    rows: list[Row]
    columns: dict               # role -> header name (or "column N")
    date_order: str             # ymd | mdy | dmy | text
    ambiguous_dates: bool       # slash dates that fit both MM/DD and DD/MM
    sign: str                   # debit_credit | negative_out | positive_out
    newest_first: bool
    warnings: list[str] = field(default_factory=list)


def num(cell: str) -> int | None:
    """'1,234.56', '-12.00', '(12.00)', '$5', '12.00-' -> cents; None if not a plain number."""
    s = (cell or "").strip()
    if not s or not NUM.fullmatch(s):
        return None
    neg = "-" in s or "(" in s
    d = re.sub(r"[^\d.,]", "", s)
    if "," in d and "." in d:
        d = d.replace(",", "")
    elif "," in d:
        head, _, tail = d.rpartition(",")
        d = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else d.replace(",", "")
    cents = round(float(d) * 100)
    return -cents if neg else cents


def parse_day(s: str, order: str = "mdy") -> date | None:
    s = (s or "").strip()
    try:
        if m := YMD.match(s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := XYZ.match(s):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            y += 2000 if y < 100 else 0
            return date(y, a, b) if order == "mdy" else date(y, b, a)
        if m := MDY_TEXT.match(s):
            return date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        if m := DMY_TEXT.match(s):
            y = int(m[3])
            return date(y + (2000 if y < 100 else 0), MONTHS[m[2].lower()], int(m[1]))
    except (ValueError, KeyError):
        return None
    return None


def any_day(s: str) -> date | None:
    return parse_day(s, "mdy") or parse_day(s, "dmy")


def detect_order(cells: list[str]) -> tuple[str, bool]:
    """(order, ambiguous) for a date column."""
    mdy = dmy = False
    kind = "text"
    for s in cells:
        s = s.strip()
        if YMD.match(s):
            kind = "ymd"
        elif m := XYZ.match(s):
            kind = "slash"
            a, b = int(m[1]), int(m[2])
            mdy |= b > 12
            dmy |= a > 12
    if kind != "slash":
        return kind, False
    if mdy and dmy:
        raise ValueError("the dates mix MM/DD and DD/MM formats")
    return ("dmy" if dmy else "mdy"), not (mdy or dmy)


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z$]+", name.lower())


def _find(names, keys, used):
    for j, n in enumerate(names):
        if j in used:
            continue
        low, toks = n.lower(), _tokens(n)
        if any((" " in k and k in low) or k in toks for k in keys):
            return j
    return None


def _roles_from_header(header: list[str]) -> dict[str, int | list[int]]:
    used: set[int] = set()
    roles: dict = {}

    def take(role, keys):
        j = _find(header, keys, used)
        if j is not None:
            roles[role] = j
            used.add(j)

    take("date", ["transaction date", "trans date"])
    if "date" not in roles:
        take("date", ["date", "posted", "posting"])
    take("balance", ["balance"])
    take("debit", ["debit", "debits", "withdrawal", "withdrawals", "funds out", "money out", "paid out",
                   "charges", "outflow"])
    take("credit", ["credit", "credits", "deposit", "deposits", "funds in", "money in", "paid in", "inflow"])
    take("amount", ["amount", "cad$", "cad", "value"])
    # "Description 1" + "Description 2" (RBC) are joined; a "transaction"/"memo" column is often
    # just a type code (WD, DEP), so it's only used when nothing better exists
    pick = lambda pat: [j for j, n in enumerate(header) if j not in used and re.search(pat, n, re.I)]
    roles["description"] = pick(r"description|details|merchant|payee|name|narrative|particulars") \
        or pick(r"transaction|memo")
    return roles


def _roles_from_data(rows: list[list[str]]) -> dict:
    """No header row (TD): infer columns from content."""
    width = max(len(r) for r in rows)
    sample = rows[:50]

    def share(j, test):
        vals = [r[j] for r in sample if j < len(r) and r[j].strip()]
        return (sum(1 for v in vals if test(v)) / len(vals)) if vals else 0

    date_j = next((j for j in range(width) if share(j, lambda v: any_day(v) is not None) > .8), None)
    nums = [j for j in range(width) if j != date_j and share(j, lambda v: num(v) is not None) > .8]
    texts = [j for j in range(width) if j != date_j and j not in nums]
    desc_j = max(texts, key=lambda j: sum(len(r[j]) for r in sample if j < len(r)), default=None)
    roles: dict = {"date": date_j, "description": [desc_j] if desc_j is not None else []}
    if len(nums) >= 3:
        roles.update(debit=nums[0], credit=nums[1], balance=nums[2])
    elif len(nums) == 2:
        # debit/credit if each row fills exactly one of them, else amount + balance
        one = all(bool(r[nums[0]].strip()) != bool(r[nums[1]].strip()) for r in sample if len(r) > nums[1])
        roles.update(debit=nums[0], credit=nums[1]) if one else roles.update(amount=nums[0], balance=nums[1])
    elif nums:
        roles["amount"] = nums[0]
    return roles


def clean_description(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s.isupper() else s


def parse_statement(text: str, flip: bool = False, date_order: str = "auto") -> Statement:
    lines = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]
    if not lines:
        raise ValueError("the file is empty")
    # skip preamble lines (account number etc.): data starts at the first row with a date
    first = next((k for k, r in enumerate(lines) if any(any_day(c) for c in r)), None)
    if first is None:
        raise ValueError("no dates found; is this a transactions CSV?")
    header = lines[first - 1] if first > 0 and not any(num(c) is not None for c in lines[first - 1]) else None
    data = lines[first:]
    roles = _roles_from_header(header) if header else _roles_from_data(data)
    if roles.get("date") is None or not roles.get("description"):
        raise ValueError("couldn't find the date and description columns")
    if not any(k in roles for k in ("amount", "debit", "credit")):
        raise ValueError("couldn't find an amount column")

    get = lambda r, j: r[j] if j is not None and j < len(r) else ""
    order, ambiguous = detect_order([get(r, roles["date"]) for r in data])
    if date_order in ("mdy", "dmy") and order in ("mdy", "dmy"):
        order = date_order

    raw = []
    warnings = []
    for r in data:
        d = parse_day(get(r, roles["date"]), "dmy" if order == "dmy" else "mdy")
        if d is None:
            warnings.append(f"skipped a line without a date: {','.join(r)[:60]}")
            continue
        if "amount" in roles:
            v = num(get(r, roles["amount"]))
        else:
            deb, cre = num(get(r, roles.get("debit"))), num(get(r, roles.get("credit")))
            v = None if deb is None and cre is None else abs(deb or 0) - abs(cre or 0)
        if v is None:
            warnings.append(f"skipped a line without an amount: {','.join(r)[:60]}")
            continue
        desc = clean_description(" ".join(get(r, j) for j in roles["description"] if get(r, j).strip()))
        raw.append((d, desc or "Unknown", v, num(get(r, roles.get("balance")))))
    if not raw:
        raise ValueError("no transactions found")

    if "amount" in roles:  # one signed column: the more common sign is spending
        negatives = sum(1 for x in raw if x[2] < 0)
        sign = "negative_out" if negatives > len(raw) - negatives else "positive_out"
    else:
        sign = "debit_credit"
    mult = -1 if sign == "negative_out" else 1
    if flip:
        mult = -mult

    rows, seen = [], {}
    for i, (d, desc, v, bal) in enumerate(raw):
        key = (d, v, desc.lower())
        rows.append(Row(i, d.isoformat(), desc, v * mult, bal, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    name = lambda j: (header[j].strip() if header and j < len(header) else f"column {j + 1}")
    columns = {k: (", ".join(name(j) for j in v) if isinstance(v, list) else name(v))
               for k, v in roles.items() if v is not None and v != []}
    return Statement(rows, columns, order, ambiguous, sign, rows[0].day > rows[-1].day, warnings)


# ---- what a row probably is ----

CARD_PAYMENT_RECEIVED = ["payment", "pymt", "paiement", "thank you", "merci", "autopay"]
CARD_WORDS = ["visa", "mastercard", "master card", "amex", "american express", "credit card"]
PAY_WORDS = ["payment", "pymt", "pmt", "paiement", "bill pay", "preauth", "autopay"]
TRANSFER = ["transfer", "tfr", "xfer", "to savings", "from savings", "tfsa", "rrsp", "fhsa", "resp",
            "contribution", "internal", "between accounts"]
# TD writes e-Transfers as "SEND E-TFR" / "E-TFR AUTODEP"
E_TRANSFER = re.compile(r"\b(?:interac\s+)?e[-\s]?(?:transfer|tfr)\b|\betfr\b", re.I)


def has(desc: str, words: list[str]) -> bool:
    """Any of `words` as a whole word or phrase (a trailing s allowed): "resp" mustn't match
    "Crespo", nor "amex" "Gamexchange"."""
    d = desc.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}s?(?![a-z0-9])", d) for w in words)


def is_card_payment(desc: str) -> bool:
    """Money out of chequing toward a credit card: a card word *and* a payment word, so
    "VISA DEBIT PURCHASE SAFEWAY" stays a purchase."""
    return has(desc, ["card payment", "card pymt"]) or (has(desc, CARD_WORDS) and has(desc, PAY_WORDS))


def transfer_person(desc: str) -> str:
    """Who sent an e-Transfer, from descriptions like "INTERAC E-TRANSFER FROM: JAMIE LEE 1234"
    or "e-Transfer received - Jamie Lee". Falls back to the whole description."""
    m = re.search(r"\b(?:from|de|received|reçu)\b\s*[:\-–]?\s*(.+)$", desc, re.I)
    name = re.sub(r"[\s\-–:#]*[A-Z0-9]*\d[A-Z0-9]*\s*$", "", (m.group(1) if m else desc)).strip(" -–:")
    name = E_TRANSFER.sub(" ", name).strip(" -–:") or desc.strip()
    return name.title() if name.isupper() or name.islower() else name


def is_own_transfer(desc: str) -> bool:
    """Moving money between your own accounts. Interac e-Transfers are payments to or from
    people (rent, paying a friend back), so they count as spending or income."""
    return has(E_TRANSFER.sub(" ", desc), TRANSFER)
