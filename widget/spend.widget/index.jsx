// Spend — Übersicht widget
// Install: copy the whole `spend.widget` folder into
//   ~/Library/Application Support/Übersicht/widgets/
// Then fill in TOKEN below. Click the widget to open the full dashboard.

import { run } from "uebersicht";

const BASE = "https://spend.anilbronson.dev";
const URL = `${BASE}/summary`;
const TOKEN = "paste-your-API_TOKEN-here";
const openDashboard = () => run(`open "${BASE}/"`);

export const command = `curl -s --max-time 8 -H "Authorization: Bearer ${TOKEN}" "${URL}"`;
export const refreshFrequency = 60 * 1000;

// Position + glass card. Change top/right to move it.
export const className = `
  top: 28px;
  right: 28px;
  width: 290px;
  padding: 20px 20px 16px;
  border-radius: 18px;
  background: rgba(22, 22, 24, 0.62);
  -webkit-backdrop-filter: blur(24px) saturate(160%);
  border: 1px solid rgba(255, 255, 255, 0.07);
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
  color: rgba(255, 255, 255, 0.92);
  font-family: -apple-system, "SF Pro Text", "Helvetica Neue", sans-serif;
  font-size: 12px;
  -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums;
`;

// Keep last good data when the server is unreachable (phone off tailnet, etc.)
export const initialState = { data: null, stale: false };
export const updateState = (event, prev) => {
  try {
    const data = JSON.parse(event.output);
    if (data && typeof data.month === "number") return { data, stale: false };
  } catch (e) {}
  return { ...prev, stale: true };
};

const money = (n, cents = true) =>
  new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency: "CAD",
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: cents ? 2 : 0,
  }).format(n);

const ago = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d`;
  return new Date(iso).toLocaleDateString("en-CA", { month: "short", day: "numeric" });
};

const C = {
  muted: "rgba(255,255,255,0.45)",
  faint: "rgba(255,255,255,0.10)",
  ok: "#7ee0a1",
  warn: "#f2c14e",
  over: "#ff7a6b",
};

// Category colors are stored as light-surface steps; use the matching dark step on this dark card.
const DARK = { "#2a78d6": "#3987e5", "#eb6834": "#d95926", "#1baf7a": "#199e70", "#eda100": "#c98500",
               "#e87ba4": "#d55181", "#6250d6": "#9085e9", "#e34948": "#e66767" };
const darkStep = (hex) => DARK[(hex || "").toLowerCase()] || hex || "#898781";

const label = {
  fontSize: 10,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: C.muted,
  marginBottom: 8,
};

const row = { display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "3px 0" };

export const render = ({ data, stale }) => {
  if (!data) return <div style={{ color: C.muted }}>Spend · waiting for server…</div>;

  const pct = data.budget_pct;
  const projectedPct = data.budget ? (data.projected / data.budget) * 100 : null;
  const tone =
    pct == null ? C.ok : pct >= 100 ? C.over : projectedPct > 100 ? C.warn : C.ok;
  const maxDay = Math.max(1, ...data.last7.map((d) => d.total));

  const vsUsual = data.usual_today > 0 ? data.today - data.usual_today : null;

  return (
    <div onClick={openDashboard} title="Open dashboard"
         style={{ opacity: stale ? 0.55 : 1, transition: "opacity .3s", cursor: "pointer" }}>
      {/* header */}
      <div style={{ ...row, padding: 0, marginBottom: 6 }}>
        <span style={{ ...label, marginBottom: 0 }}>{data.month_label}</span>
        <span style={{ color: C.muted }}>
          today <span style={{ color: "white" }}>{money(data.today)}</span>
          {vsUsual !== null && (
            <span style={{ marginLeft: 4, fontSize: 10 }}> · usually {money(data.usual_today, false)}</span>
          )}
        </span>
      </div>

      {/* month total */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <span style={{ fontSize: 30, fontWeight: 600, letterSpacing: "-0.02em" }}>
          {money(data.month)}
        </span>
        {data.budget && <span style={{ color: C.muted }}>/ {money(data.budget, false)}</span>}
      </div>

      {data.budget && (
        <div style={{ height: 4, borderRadius: 2, background: C.faint, margin: "10px 0 8px", overflow: "hidden" }}>
          <div style={{ width: `${Math.min(pct, 100)}%`, height: "100%", background: tone, borderRadius: 2 }} />
        </div>
      )}

      <div style={{ color: C.muted, marginBottom: 16 }}>
        {money(data.daily_avg)}/day · on pace for{" "}
        <span style={{ color: tone }}>{money(data.projected, false)}</span>
      </div>

      {/* last 7 days */}
      <div style={{ display: "flex", gap: 6, alignItems: "flex-end", height: 44, marginBottom: 4 }}>
        {data.last7.map((d, i) => (
          <div key={d.date} style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%" }}>
            <div
              title={money(d.total)}
              style={{
                height: `${Math.max(d.total > 0 ? 8 : 3, (d.total / maxDay) * 100)}%`,
                background: i === 6 ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.28)",
                borderRadius: 3,
              }}
            />
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 6, marginBottom: 16 }}>
        {data.last7.map((d) => (
          <div key={d.date} style={{ flex: 1, textAlign: "center", fontSize: 9, color: C.muted }}>
            {d.label}
          </div>
        ))}
      </div>

      {/* top categories */}
      {data.top_categories.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={label}>Top this month</div>
          {data.top_categories.map((c) => (
            <div key={c.name} style={row}>
              <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span style={{ width: 7, height: 7, borderRadius: 4, background: darkStep(c.color) }} />
                {c.name}
              </span>
              <span style={{ color: C.muted }}>{money(c.total, false)}</span>
            </div>
          ))}
        </div>
      )}

      {/* recent */}
      <div>
        <div style={label}>Recent</div>
        {data.recent.map((t) => (
          <div key={t.id} style={row}>
            <span style={{ display: "flex", alignItems: "center", gap: 7, overflow: "hidden", maxWidth: 160 }}>
              <span title={t.category} style={{ width: 7, height: 7, borderRadius: 4, flex: "none", background: darkStep(t.color) }} />
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.merchant}</span>
            </span>
            <span style={{ display: "flex", gap: 10 }}>
              <span style={{ color: C.muted, fontSize: 11 }}>{ago(t.ts)}</span>
              <span style={{ minWidth: 58, textAlign: "right" }}>{money(t.amount)}</span>
            </span>
          </div>
        ))}
        {data.recent.length === 0 && <div style={{ color: C.muted }}>No purchases yet</div>}
      </div>

      {stale && <div style={{ marginTop: 10, fontSize: 10, color: C.muted }}>offline · showing last update</div>}
    </div>
  );
};
