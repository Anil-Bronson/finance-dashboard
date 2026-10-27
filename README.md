# Spend tracker

My own finance tracker: every purchase, paycheque, account and subscription in one place, running on the home server at **https://spend.anilbronson.dev** (tailnet only).

```
Apple Pay tap ──▶ iPhone Shortcut ──▶ POST /tx ───────┐
bank statement CSV ──▶ Import (preview, then save) ───┤
paycheque, gym, iCloud… ──▶ recurring entries ────────┼──▶ FastAPI + SQLite (Docker, port 127.0.0.1:8090)
cash, anything else ──▶ "+ Add" in the dashboard ─────┘        │  behind Caddy (HTTPS, Porkbun DNS-01)
                                                               ├─ /          dashboard
          OpenAI Decisions API ◀── categories, need vs want,   ├─ /summary   Mac desktop widget (Übersicht)
          import second opinions, subscription checks          └─ nightly backups → /mnt/storage
```

---

## Using it

### Getting in
- **Your MacBook Pro** opens the dashboard straight away. Its Tailscale address is in `TRUSTED_DEVICES`.
- **Any other browser** asks for the API token once (`grep API_TOKEN ~/spend-tracker/.env`) and remembers it. Safari may forget it in Private Browsing or in a Home Screen web app.
- **Tailscale has to be on.** The site only resolves to the server's Tailscale IP.

### Privacy
- **Hide all amounts:** the eye button in the top bar, next to the gear, turns every amount on the dashboard into ••••. Useful for screen sharing or using it in public.
- **Hide net worth only:** the eye button on the Accounts card hides balances and net worth, and leaves spending visible.
- Both are remembered per device, so hiding on the Mac doesn't hide on the phone.

### Dashboard
- **Top summary:** spent so far against your budget, with a pace marker, the daily average, a projected month-end, and a comparison with last month. **Optional (wants)** shows how much of the month was optional; tap it to list every optional purchase.
- **Daily spend calendar:** each day is shaded by spend (darker purple = more). Tap a day to see its purchases.
- **Categories:** this month vs last, with budget markers. Tap one to filter the list.
- **Cash flow:** income, spending, net and savings rate for the month, plus the month's income entries. Use "+ Income" to log one.
- **Accounts · net worth:** balances, assets minus what you owe, and a chart of net worth by month.
- **Transactions:**
  - Search, edit, or delete. A negative amount is a refund.
  - Tap a category pill to change it, or to confirm an **AI?** pick.
  - "N to review" filters to the AI's unsure picks.
- **Year chart:** Jan–Dec spending bars, with a green line for each month's income. Tap a bar to open that month.
- **Top merchants and Recurring:** your repeating payments and income, plus **"Looks like a subscription"** suggestions.
- **Settings (gear):** budget, category names/colors/budgets, light or dark mode.
- `?month=2026-09` in the URL opens a specific month.

### How money gets in
| Source | How |
|---|---|
| **Apple Pay** | The Transaction automation runs *Auto Apple Pay*, which sends merchant, amount and card straight to `/tx`. There's no queue, so a tap made while Tailscale is off or the server is unreachable isn't logged; add it with **+ Add**. A declined tap sends empty fields, which the server skips (it logs `skipped tx with empty amount`). The older *Log Purchase* / *Flush Purchases* queue → `/tx/batch` still works. |
| **Bank statements** | Accounts card → **Import CSV**: pick the account, then the CSV exported from the bank. Nothing is saved until you've reviewed the preview (below). |
| **Recurring** | Paycheque, gym, iCloud…: monthly, weekly or every 2 weeks, as spending or income (into an account). Each one is logged automatically on its day. Pause or remove anytime; past entries stay. |
| **+ Add** | Cash, or anything else. The **Purchase / Income** switch is at the top of the form. |

### Statement import (the preview)
- **Already logged:** Apple Pay taps, recurring entries and older history are matched, so nothing is counted twice. Foreign-currency taps take the statement's exact CAD amount, and a paycheque whose amount changed takes the real figure.
- **Skipped:** credit card payments and transfers between your own accounts (TFSA, savings…). Each card's purchases come from that card's own CSV. Interac e-Transfers count: *to* someone is spending, *from* someone usually means they're paying you back.
- **AI second opinion:** the AI re-checks the rows keywords are bad at (transfer vs spending, income vs paid back, card payment vs refund) and confirms matches whose names don't share a word. Rows it's unsure about are outlined in amber.
- **Re-imports are safe:** rows seen before are ignored, so overlapping date ranges are fine, and every choice you made is remembered.
- **Balances:** a chequing or savings statement's closing balance becomes the account balance, unless you've entered a newer one.
- **Formats:** TD-style files without a header row, Debit/Credit or signed Amount columns, and MM/DD, DD/MM or ISO dates. If something comes out backwards, use **Flip them** or **switch**.

### Categories, needs vs wants, review
**A purchase's category, in order of priority:**
1. What you set on that purchase.
2. What you set for the merchant (with "use for every … purchase" ticked).
3. A keyword rule in `data/categories.json` (no restart needed after editing).
4. The AI: one request per new merchant, saved permanently.
5. Otherwise **Other**, retried later.

**AI? review:** an AI pick under 75% confidence, or one that fell back to Other, is tagged **AI?**. Choose a category (even the same one) to confirm it.

**Needs vs wants:**
- Each merchant is rated by the AI. Until then, or when the AI is under 50% sure, the category decides:
  - **Needs:** Groceries, Transport, Car, Internet
  - **Wants:** Dining, Coffee, Alcohol, Fun, Date, Clothing, Subscriptions, Self Development
  - **Depends on the merchant:** everything else
- To set it yourself, tap a category pill → **Usually: Need / Want**.

**Merchant grouping:** order and reference codes are ignored, so all `AMZN Mktp CA*…` orders count as one merchant ("Amazon").

**Subscription finder:** it looks for merchants charged at a steady price on a steady schedule that aren't recurring yet, and the AI filters out regular visits like your café. **Add** starts logging it from the next charge; **✕** hides it for good.

### Accounts and balances
- **Account types:** chequing, savings, investments, cash, other assets, credit cards and loans. For cards and loans, enter **what you owe** as a positive number.
- **Updating balances:** tap a balance to update it, as often as you like. Every update is kept for the net worth chart. A faded bar means some account had no balance yet that month, and balances older than a month show in amber.
- **Income raises balances:** an account's balance is the last one entered, plus income paid into it since. That's how each paycheque raises Wealthsimple automatically. Spending isn't linked to accounts, so withdrawals only come off when you next update the balance or import a statement.
- **Closing vs deleting:** "Close account" records $0 and keeps the history; Delete removes the account entirely.

### Foreign currency
- **Conversion:** `US$12.00`, `€8,50`, `£3.20`, `¥1,200` and `12.00 USD` are converted to CAD at that day's rate (ECB via Frankfurter, with the Bank of Canada as fallback), plus `FX_FEE_PCT`. A plain `$` means CAD.
- **Display:** totals are in CAD, and the list also shows what was charged.
- **Offline:** if no rate can be fetched, the purchase shows "converting…" and is filled in within 5 minutes.
- **Exact statement amounts:** type the CAD figure over it to match your statement, or import the statement.

---

## Running it

### Files
| Path | What |
|---|---|
| `app/main.py` | the API, the background worker and the AI features |
| `app/bankcsv.py` | statement CSV parsing (no database) |
| `app/static/index.html` | the dashboard: one file, no build step |
| `app/backup.py`, `scripts/backup.sh` | nightly backups |
| `data/spend.db` | **the whole database** (SQLite, WAL). Never commit or delete it. |
| `data/categories.json` | keyword rules |
| `widget/spend.widget/` | Mac desktop widget. Its copy on the Mac holds the token. |

### Settings (`.env`, private, git-ignored)
| Variable | Default | |
|---|---|---|
| `API_TOKEN` | (required) | for the Shortcut, the widget and other browsers |
| `TRUSTED_DEVICES` | none | Tailscale IPs that need no token, comma-separated (now: the MacBook) |
| `OPENAI_API_KEY` | none | turns on the AI features |
| `DECISIONS_MODEL` | `gpt-6-luna` | OpenAI Decisions model |
| `AI_REVIEW_BELOW` | `0.75` | AI picks below this confidence get **AI?** |
| `ANTHROPIC_API_KEY`, `AI_MODEL` | none, `claude-haiku-5-5` | fallback categorizer if there's no OpenAI key |
| `MONTHLY_BUDGET` | `0` | starting value; Settings overrides it |
| `HOME_CURRENCY`, `FX_FEE_PCT` | `CAD`, `2.5` | currency conversion |
| `TZ_NAME` | `America/Vancouver` | local time for dates and backups |
| `WORKER_INTERVAL` | `300` | seconds between background runs (recurring, conversions, AI) |

After editing `.env`, run `docker compose up -d`.

### Everyday commands (in `~/spend-tracker`)
```bash
scripts/backup.sh                       # back up first
docker compose up -d --build            # deploy a code change
docker logs --tail 30 spend             # logs
curl -s localhost:8090/health           # {"ok":true}
# tests: the server has no python venv, so run them in the app image
docker run --rm --network none -v "$PWD":/src:ro -w /src spend-tracker-spend python test_api.py
```

### Backups
- **Schedule:** cron runs `scripts/backup.sh` at 03:30:
  ```
  30 3 * * * /home/anil/spend-tracker/scripts/backup.sh >> /home/anil/.local/state/spend-backup.log 2>&1
  ```
- **How:** it takes an online SQLite backup inside the container, checks it with `integrity_check`, and keeps **30 days** in `data/backups/`. It then copies them to **`/mnt/storage/backups/spend-tracker/`** (90 days), on different disks from the server's main drive. If the disk enclosure is off, that copy is skipped and the run reports failure.

**Restore:**
```bash
cd ~/spend-tracker && docker compose stop spend
cp data/spend.db ~/spend-before-restore.db                 # keep the current one, just in case
rm -f data/spend.db data/spend.db-wal data/spend.db-shm    # root-owned, but data/ is yours so rm works
cp /mnt/storage/backups/spend-tracker/spend-YYYY-MM-DD.db data/spend.db
docker compose up -d
```

### Security
- **Tailnet only:** the DNS record points at the Tailscale IP. The tailnet includes other people's devices, so they're **not** trusted; only `TRUSTED_DEVICES` skip the token.
- **Caddy is the only way in:** the container listens on `127.0.0.1` only, and trusted devices are identified by the address Caddy reports. A forged header is rejected (tested).
- **Secrets** (token, OpenAI key) live only in `.env`.

### AI cost
OpenAI Decisions bills input tokens only, at about $0.10 per million: roughly **$0.0001 per new merchant**. Each merchant is asked once and the answer is saved. All of October 2026's setup and testing came to a few cents.

---

## API reference

All endpoints need `Authorization: Bearer <API_TOKEN>` (or a trusted device), except `/` and `/health`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/tx` | add one (`amount, merchant, card, ts, category, note, source, currency`); used by the *Auto Apple Pay* Shortcut. An empty `amount` is skipped: 200 `{"skipped": "empty amount"}`, nothing saved |
| POST | `/tx/batch` | older phone queue flush, `{"lines": "ts\|amount\|card\|merchant\n…"}`; empty-amount lines count as `skipped` |
| GET | `/tx` | `start, end, category, q, review, need (need\|want\|unrated), limit, offset` |
| PATCH / DELETE | `/tx/{id}` | edit (`category` + `apply_to_merchant`, `need`, `amount`, `ts`, `merchant`, `note`, `card`) |
| GET / POST / PATCH / DELETE | `/categories[/{name}]` | categories, budgets, colors, `description` (guides the AI) |
| GET / POST / PATCH / DELETE | `/recurring[/{id}]` | `kind` spend\|income, `freq` monthly\|weekly\|biweekly, `day` or `anchor`, `account_id` |
| GET | `/subscriptions/suggestions` | possible subscriptions not yet recurring |
| POST | `/subscriptions/{key}/accept` · `/dismiss` | add as recurring (from the next charge) · hide |
| GET / POST / PATCH / DELETE | `/income[/{id}]` | `amount, payer, ts, account_id, note` |
| GET / POST | `/tx/{id}/shares` | split a purchase: who owes you what; POST `{person, amount, paid}` |
| PATCH / DELETE | `/shares/{id}` | edit a share; `{paid: true}` marks it paid back |
| GET | `/shares` | open IOUs plus paybacks from the last 60 days |
| GET | `/summary/weekly` | the weekly summary (Mon–Sun); `?end=YYYY-MM-DD` for another week |
| POST | `/summary/weekly/send` | push it now through ntfy |
| GET / PUT | `/budgets` | budgets with typical/average/last month per category; PUT `{total, categories}` |
| GET / POST / PATCH / DELETE | `/accounts[/{id}]` | accounts with current balances and net worth |
| GET / POST | `/accounts/{id}/balances` | balance history; POST `{balance, day}` (one per day) |
| DELETE | `/accounts/{id}/balances/{day}` | remove one balance |
| POST | `/import/preview` | `{account_id, csv, flip, date_order}` → rows with a proposed action |
| POST | `/import/commit` | same + `actions: {row: purchase\|income\|matched\|skip}` |
| GET | `/stats/month?month=YYYY-MM` | the dashboard's month: totals, days, categories, income, needs/wants, review count |
| GET | `/stats/trend?months=12&end=YYYY-MM` | monthly spend, income and net |
| GET | `/stats/networth?months=12&end=YYYY-MM` | net worth per month end |
| GET / PUT | `/settings` | budget; also reports AI status and whether this device is trusted |
| GET | `/summary` | Mac widget |

## Known limits
- **Automatic capture** is Apple Pay taps and recurring entries only. Card swipes, online card-number purchases and debit come in through statement imports or + Add.
- **Recurring entries assume the charge happened.** Update or pause them when a price changes or you cancel. A statement import corrects the amounts.
- **Account balances only follow income,** not spending, until the next balance update or statement import.
- **Statement formats:** tested on TD's format and files modelled on other banks' exports. If a new bank's CSV won't read, the error says why.
