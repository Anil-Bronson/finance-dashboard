# Handoff: native iOS widget for the spend tracker

## Context
- Personal spend tracker: iPhone Apple Pay Shortcut → FastAPI/SQLite server on my homelab
  (repo: github.com/Anil-Bronson/finance-dashboard) → web dashboard + an Übersicht Mac widget
  (widget/spend.widget/index.jsx; read it, it's the design reference).
- Goal now: a native WidgetKit widget for **me only**, as a learning step. Later, maybe a
  standalone on-device app for non-technical friends (data on their phone, Shortcut logs taps
  via an App Intent, TestFlight). Don't build the friends' version yet.

## Decisions so far
- New `ios/` folder in the repo. iOS 17+, SwiftUI + WidgetKit, host app + widget extension.
  Generate the project with XcodeGen (`ios/project.yml`) so there's no hand-edited .xcodeproj.
- I'm on a free Apple account for now (builds expire after 7 days), so **avoid App Groups**.
  The extension caches its last good response in its own UserDefaults.
- Sizes: small (month total / budget + bar), medium (+ 7-day bars + top categories),
  large (+ 5 recent), plus lock-screen accessoryCircular/Rectangular/Inline. Mark amounts
  `.privacySensitive()`.
- Follow system light/dark. Category colors come from the DB as light-mode hex. Map them to
  dark steps in dark mode with the same table as the Übersicht widget's DARK map / main.py PALETTE:
  2a78d6→3987e5, eb6834→d95926, 1baf7a→199e70, eda100→c98500, e87ba4→d55181,
  008300→008300, 6250d6→9085e9, e34948→e66767; Other = 898781.
  Bar tone: ok / warn (projection over budget) / over (budget_pct ≥ 100).
- Timeline refresh about every 15 min. On a failed fetch, show cached data dimmed with an "offline" note.
- Tapping the widget opens https://spend.anilbronson.dev/ (widgetURL → host app's onOpenURL → open in Safari).

## Server facts
- `GET https://spend.anilbronson.dev/summary`. Reachable **only over Tailscale**.
- Auth: my iPhone (Tailscale 100.87.49.95) is in the server's TRUSTED_DEVICES, so requests from
  the phone need **no token**. Add an optional token field (e.g. an AppIntentConfiguration
  parameter) for other cases. Never commit a real token.
- JSON: as_of, month_label, today, usual_today, month, budget (nullable), budget_pct (nullable),
  daily_avg, projected, count_month, income_month, net_month,
  last7[{date,label,total}], top_categories[{name,total,color}], top_merchants[{name,total}],
  recent[{id, ts, date, amount, merchant, card, category, color, category_source, source, note}].
  Amounts are CAD floats. Decode defensively (optionals for anything nullable).
- `ts` is local time with an offset, but bank-imported rows use an odd `dayT12:<account>:<n>` form.
  Fall back to the first 10 chars (`date`) when ISO parsing fails.
- Server changes happen on the homelab: any new field goes into summary() in app/main.py,
  with a test in test_api.py and a redeploy. On the Mac, only edit `ios/`.

## Not done yet
Nothing has been written. Start with project.yml, the Codable model + fetch/cache, then the widget views.
