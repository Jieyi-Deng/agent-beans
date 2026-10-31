// Dashboard (design 3a): a briefing first, the verdict, the one decision waiting on the owner,
// three numbers and the chart with the team's moves on it; the full numbers follow below. All of
// it is read from the same Insights-shaped rows the Analyst reads.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, date, onDay, onDays, useData, type Action, type CampaignRow, type DashboardData,
  type Deliverable, type DraftNode, type Kpi, type Metrics } from "./api";
import { go, type Route } from "./route";
import { Av } from "./ui";

type Ready = Extract<DashboardData, { ready: true }>;

const usd = (n: number | null | undefined, digits = 2) =>
  n == null ? "–" : "$" + n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const int = (n: number | null | undefined) => (n == null ? "–" : n.toLocaleString("en-US"));
const pct = (n: number | null | undefined) => (n == null ? "–" : (n * 100).toFixed(2) + "%");
const ratio = (n: number | null | undefined) => (n == null ? "–" : n.toFixed(2) + "x");

function fmt(k: Kpi): string {
  if (k.fmt === "money") return usd(k.value);
  if (k.fmt === "pct") return pct(k.value);
  if (k.fmt === "ratio") return ratio(k.value);
  return int(k.value);
}

function Tile({ k, d, open, onOpen }: { k: Kpi; d: Ready; open: boolean; onOpen: (key: string | null) => void }) {
  const ev = d.evidence;
  return (
    <button className={"kpi" + (k.hero ? " hero" : "")} aria-expanded={open}
            onMouseEnter={() => onOpen(k.key)} onMouseLeave={() => onOpen(null)}
            onFocus={() => onOpen(k.key)} onBlur={() => onOpen(null)}>
      <span className="k">{k.label}</span>
      <span className="v">{fmt(k)}</span>
      <span className={"d " + k.tone}>{k.sub}</span>
      {open && (
        <span className="tip" onClick={(e) => e.stopPropagation()}>
          <span className="m">{ev.rows} Insights rows · {ev.first} to {ev.last}</span>
          <span style={{ display: "block", marginTop: 5 }}>
            <b>{fmt(k)}</b> = {k.formula}, {onDays(d.start, d.end)}, every ad in the account.
          </span>
          <span className="m" style={{ display: "block", marginTop: 6 }}>
            fields: {ev.fields} · level={ev.level} · time_increment=1
          </span>
          {ev.used_in && (
            <span style={{ display: "block", marginTop: 7 }}>
              The Analyst read these rows for{" "}
              <a href={`#/deliverables/${ev.used_in.key}`} onMouseDown={(e) => e.preventDefault()}>{ev.used_in.title}</a>
            </span>
          )}
        </span>
      )}
    </button>
  );
}

function Chip({ id }: { id: string }) {
  return <span className="chip" title="Evidence id: one Insights row, or a figure computed from them">{id}</span>;
}

function cells(m: Metrics, roas: boolean) {
  const none = m.impressions === 0;
  return (
    <>
      <td className="num">{none ? "–" : usd(m.spend)}</td>
      <td className="num">{none ? "–" : roas ? ratio(m.roas) : int(m.impressions)}</td>
      <td className="num">{none ? "–" : pct(m.ctr)}</td>
      <td className="num">{none ? "–" : int(m.results)}</td>
      <td className={"num " + m.tone}>{none ? "–" : usd(m.cpr)}</td>
    </>
  );
}

function Table({ d }: { d: Ready }) {
  const roas = d.kpis.some((k) => k.key === "roas");
  const name = d.result_name.charAt(0).toUpperCase() + d.result_name.slice(1);
  return (
    <table className="dtbl">
      <thead><tr><th>Name</th><th>Status</th><th className="num">Budget/day</th><th className="num">Spend</th>
        <th className="num">{roas ? "ROAS" : "Impr."}</th><th className="num">CTR</th><th className="num">{name}s</th>
        <th className="num">Cost/{d.result_name}</th><th className="num" style={{ paddingRight: 0 }}>Evidence</th></tr></thead>
      <tbody>
        {d.campaigns.map((c) => [
          <tr className="cmp" key={c.id}>
            <td>{c.name} <span className="mono muted" style={{ fontSize: 10.5, fontWeight: 400 }}>{c.id}</span>
              {c.in_learning && <span className="mono muted" style={{ fontSize: 10.5, fontWeight: 400 }}> · learning</span>}</td>
            <td><span className={"lbl " + c.status}>{c.status_label}</span></td>
            <td className="num">{usd(c.budget, 0)}</td>
            {cells(c, roas)}
            <td style={{ textAlign: "right", paddingRight: 0 }}>
              <span className="mono muted" style={{ fontSize: 10.5 }}>{c.rows} rows</span></td>
          </tr>,
          ...c.ads.map((a) => (
            <tr className="sub" key={a.id}>
              <td>{a.name} <span className="mono muted" style={{ fontSize: 10.5 }}>{a.id}</span></td>
              <td><span className={"lbl " + a.status}>{a.status_label}</span></td>
              <td className="num" />
              {cells(a, roas)}
              <td style={{ textAlign: "right", paddingRight: 0 }}>
                {a.evidence ? <Chip id={a.evidence} />
                  : a.waits ? <span className="mono muted" style={{ fontSize: 10.5 }}>waits on you</span> : null}
              </td>
            </tr>
          )),
        ])}
      </tbody>
    </table>
  );
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

function DraftRow({ n, depth }: { n: DraftNode; depth: number }) {
  const details = Object.entries(n.details).slice(0, 4);
  return (
    <>
      <div className="adraft" style={{ paddingLeft: depth * 18 }}>
        <span className="dlvl">{n.level}</span>
        <span className="nm">{n.name || <span className="muted">Untitled</span>}
          {n.id && <span className="mono muted" style={{ fontSize: 10.5 }}> {n.id}</span>}</span>
        {n.status && <span className="lbl">{n.status.toLowerCase()}</span>}
        <span className="mono muted dt">
          {details.map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(" · ")}
          {n.nested.length > 0 && `${details.length ? " · " : ""}${n.nested.map((k) => k.replace(/_/g, " ")).join(", ")} set`}
        </span>
      </div>
      {n.children.map((c, i) => <DraftRow key={`${c.id}-${i}`} n={c} depth={depth + 1} />)}
    </>
  );
}

/** Unpublished drafts staged in Ads Manager: a real account's through the Ads MCP, or the sandbox's
 *  synthetic ones. PGT never edits them. */
function Drafts({ tick }: { tick: number }) {
  const { data, error } = useData(() => api.drafts(), [tick]);
  const c = data?.counts;
  return (
    <div className="card" style={{ padding: "12px 14px 8px" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6, flexWrap: "wrap", gap: 8 }}>
        <span className="h3">Drafts in Ads Manager</span>
        <span className="mono muted" style={{ fontSize: 10.5 }}>
          {c ? `${plural(c["campaign"] ?? 0, "campaign")} · ${plural(c["ad set"] ?? 0, "ad set")} · ${plural(c["ad"] ?? 0, "ad")} · ` : ""}
          read only, not published</span>
      </div>
      {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
      {data && !data.available && <div className="muted" style={{ fontSize: 12, padding: "4px 0" }}>Drafts could not be read: {data.reason}</div>}
      {data?.available && data.drafts.length === 0 && <div className="muted" style={{ fontSize: 12, padding: "4px 0" }}>No drafts are waiting in Ads Manager.</div>}
      {data?.drafts.map((d, i) => <DraftRow key={`${d.id}-${i}`} n={d} depth={0} />)}
    </div>
  );
}


const usd0 = (n: number) => "$" + Math.round(n).toLocaleString("en-US");
const cap1 = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

/** Width of an element, kept current, so the chart draws at its real size and its text stays
 *  the same size as the page's instead of scaling with the box. */
function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/** One line of "What the team did": the same flag raised on several days in a row reads once. */
type DidRow = { first: number; last: number; a: Action };

function group(actions: Action[]): DidRow[] {
  const out: DidRow[] = [];
  const shape = (a: Action) => `${a.who.actor}|${a.verb}|${a.text.replace(/\d+/g, "#")}|${a.link?.key ?? ""}`;
  for (const a of actions) {
    const prev = out[out.length - 1];
    if (prev && shape(prev.a) === shape(a) && !a.link && a.day <= prev.last + 1) {
      prev.last = a.day;
      prev.a = a;
    } else out.push({ first: a.day, last: a.day, a });
  }
  return out;
}

const days = (g: DidRow) => onDays(g.first, g.last);

/** The few words a chart callout has room for: "Created 2 paused ads (...), after ..." reads
 *  "Created 2 paused ads". */
function short(a: Action): string {
  const t = a.text.split(" (")[0].split(", after")[0].split("; ")[0];
  return t.length > 34 ? t.slice(0, 33).trimEnd() + "…" : t;
}

const CHAR = 6.2; // average width of 11.5px Schibsted Grotesk, for sizing callout boxes

function Chart({ d }: { d: Ready }) {
  const [ref, W] = useWidth<HTMLDivElement>();
  const H = 250, L = 44, R = 70, T = 52, B = 28;
  const s = d.series;
  const n = s.length;
  const vals = [...s.map((x) => x.cpr).filter((v): v is number => v != null), d.target];
  const span = Math.max(...vals) - Math.min(...vals) || 1;
  const step = span > 12 ? 5 : span > 5 ? 2 : 1;
  const lo = Math.max(0, Math.floor((Math.min(...vals) - span * 0.25) / step) * step);
  const hi = Math.ceil((Math.max(...vals) + span * 0.15) / step) * step;
  const pw = Math.max(1, W - L - R), ph = H - T - B;
  const x = (i: number) => L + (n === 1 ? pw / 2 : (pw * i) / (n - 1));
  const y = (v: number) => T + (ph * (hi - v)) / (hi - lo || 1);
  const ticks: number[] = [];
  const every = Math.max(step, Math.ceil((hi - lo) / 4 / step) * step);
  for (let v = lo; v <= hi + 1e-9; v += every) ticks.push(v);
  const pts = s.map((p, i) => (p.cpr == null ? null : ([x(i), y(p.cpr)] as const)));
  const line = pts.filter(Boolean).map((p) => `${p![0].toFixed(1)},${p![1].toFixed(1)}`).join(" ");
  const over = s.map((p, i) => ({ p, i })).filter(({ p }) => p.cpr != null && p.cpr > d.target);
  const name = d.result_name;

  // Callouts for what changed the account (approved changes), one per day, kept apart.
  const callouts: { day: number; i: number; label: string; bx: number; bw: number }[] = [];
  const byDay = new Map<number, Action[]>();
  for (const a of d.actions.filter((a) => a.link)) byDay.set(a.day, [...(byDay.get(a.day) ?? []), a]);
  let edge = L;
  for (const [day, list] of byDay) {
    const i = s.findIndex((p) => p.day === day);
    if (i < 0 || W === 0) continue;
    const label = list.length > 1 ? `${short(list[0])} and ${list.length - 1} more` : short(list[0]);
    const bw = Math.round(Math.max(label.length, 6) * CHAR + 18);
    let bx = x(i) + 4;
    if (bx + bw > W - 2) bx = x(i) - 4 - bw;
    if (bx < edge) continue;
    callouts.push({ day, i, label, bx, bw });
    edge = bx + bw + 8;
  }
  // As many dates as fit without touching ("Nov 10" is about 6 characters), always the last one.
  const labelEvery = n > 1 ? Math.max(1, Math.ceil((7 * CHAR + 12) / (pw / (n - 1)))) : 1;
  const xl = s.map((_, i) => i)
    .filter((i) => i === n - 1 || (i % labelEvery === 0 && n - 1 - i >= labelEvery));

  return (
    <div ref={ref} className="db-chart">
      {W > 0 && (
        <svg width={W} height={H} role="img"
             aria-label={`Cost per ${name} each day, ${onDays(d.start, d.end)}, against your `
                         + `${usd(d.target)} goal`}>
          <rect x={L} y={y(d.target)} width={pw} height={T + ph - y(d.target)} fill="var(--good-pale)" opacity=".55" />
          {ticks.map((v) => (
            <text key={v} x={L - 10} y={y(v) + 4} textAnchor="end" className="db-ax">{usd0(v)}</text>
          ))}
          <text x={L + 8} y={T + ph - 8} className="db-under">Under your goal</text>
          <line x1={L} x2={L + pw} y1={y(d.target)} y2={y(d.target)} stroke="var(--ink)" strokeWidth="1.5" strokeDasharray="5 4" />
          <text x={L + pw + 6} y={y(d.target) + 4} className="db-goal">{usd0(d.target)} goal</text>
          {callouts.map((c) => (
            <g key={c.day}>
              <line x1={x(c.i)} x2={x(c.i)} y1={T - 18} y2={T + ph} stroke="var(--rule-strong)" />
              <rect x={c.bx} y={4} width={c.bw} height={34} rx="2" fill="var(--surface)" stroke="var(--rule-strong)" />
              <text x={c.bx + 8} y={18} className="db-ax">{onDay(c.day)}</text>
              <text x={c.bx + 8} y={32} className="db-call">{c.label}</text>
            </g>
          ))}
          <polyline fill="none" stroke="var(--blue)" strokeWidth="2.2" strokeLinejoin="round" points={line} />
          {pts.map((p, i) => p && (
            <circle key={i} cx={p[0]} cy={p[1]} r="7" fill="transparent">
              <title>{onDay(s[i].day)}: {usd(s[i].cpr)} per {name}, {usd(s[i].spend)} spent</title>
            </circle>
          ))}
          {over.map(({ i }) => <circle key={"o" + i} cx={pts[i]![0]} cy={pts[i]![1]} r="4.5" fill="var(--signal)" pointerEvents="none" />)}
          {over.length > 0 && (() => {
            const lastOver = over[over.length - 1];
            const label = over.length === 1 ? `${usd(lastOver.p.cpr)} on ${onDay(lastOver.p.day)}`
              : `Over your goal on ${over.length} days`;
            const px = pts[lastOver.i]![0];
            const anchor = px > L + pw * 0.7 ? "end" : "start";
            return <text x={px + (anchor === "end" ? 0 : 8)} y={Math.max(T - 4, pts[lastOver.i]![1] - 12)}
                         textAnchor={anchor} className="db-over">{label}</text>;
          })()}
          {xl.map((i) => (
            <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                  className="db-ax">{onDay(s[i].day)}</text>
          ))}
        </svg>
      )}
    </div>
  );
}

const NEEDS: Record<string, [string, string]> = {
  TestPlan: ["A test plan is ready", "Review the plan"],
  ChangeSet: ["Changes are ready to apply", "Review the changes"],
  CreativeSet: ["New ads are ready", "Review the ads"],
};

function Needs({ item }: { item: Deliverable | undefined }) {
  if (!item) {
    return (
      <div className="db-needs quiet">
        <span className="tag g">Nothing waiting</span>
        <div className="db-needs-t">Nothing needs you right now</div>
        <p>The team posts in the Team Room when a plan or a change needs your approval.</p>
        <a className="btn ghost" href="#/room">Open the Team Room</a>
      </div>
    );
  }
  const [head, button] = NEEDS[item.kind] ?? [item.title, "Review it"];
  return (
    <div className="db-needs">
      <div className="row" style={{ gap: 10 }}><Av who={item.from} /><span className="tag b">Needs you</span></div>
      <div className="db-needs-t">{head}</div>
      <p>The {item.from.name} sent {item.sub.charAt(0).toLowerCase() + item.sub.slice(1)}. Nothing changes in your account until you approve.</p>
      <a className="btn pri" href={`#/deliverables/${item.key}`}>{button}</a>
    </div>
  );
}

function verdict(d: Ready) {
  const k = (key: string) => d.kpis.find((x) => x.key === key)?.value ?? null;
  const cpr = k("cpr"), results = k("results") ?? 0, spend = k("spend") ?? 0, revenue = k("revenue");
  const good = cpr != null && cpr <= d.target;
  const gap = cpr == null ? 0 : Math.abs(d.target - cpr);
  const ads = d.campaigns.flatMap((c) => c.ads).filter((a) => a.status === "live" && a.results >= 3 && a.cpr != null);
  const best = ads.sort((a, b) => a.cpr! - b.cpr!)[0];
  const tail = d.series.slice(-2).filter((p) => p.cpr != null);
  const rising = cpr != null && tail.length === 2 && tail.every((p) => p.cpr! > cpr * 1.1);
  const said = [
    `${results.toLocaleString("en-US")} ${d.result_name}s from ${usd0(spend)} of ads` +
      (revenue != null ? ` brought in ${usd0(revenue)} in sales.` : "."),
    best ? `“${best.name}” is the best ad running, at ${usd(best.cpr)} per ${d.result_name}.` : "",
    rising ? `Cost per ${d.result_name} rose on the last two days.` : "",
  ].filter(Boolean).join(" ");
  return { cpr, good, gap, said };
}

function Briefing({ d, waiting }: { d: Ready; waiting: Deliverable | undefined }) {
  const v = verdict(d);
  const k = (key: string) => d.kpis.find((x) => x.key === key)?.value ?? null;
  const ndays = d.end - d.start + 1;
  const results = k("results") ?? 0, spend = k("spend") ?? 0, revenue = k("revenue"), roas = k("roas");
  const ctr = k("ctr");
  return (
    <>
      <div className="db-top">
        <div className="db-verdict">
          <div className="row" style={{ gap: 10 }}>
            <span className={v.good ? "tag g" : "tag s"}>{v.good ? "On target" : "Over target"}</span>
            <span className="db-cap">Your goal: a {d.result_name} for {usd0(d.target)} or less</span>
          </div>
          {v.cpr == null ? <h2>No {d.result_name}s in these days yet.</h2> : (
            <h2>You paid <em className={v.good ? "" : "over"}>{usd(v.cpr)} per {d.result_name}</em>,
              {" "}{usd(v.gap)} {v.good ? "under" : "over"} your {usd0(d.target)} goal.</h2>
          )}
          <p>{v.said}</p>
        </div>
        <Needs item={waiting} />
      </div>
      <div className="db-trio">
        <div><span className="db-cap">{cap1(d.result_name)}s</span><span className="db-big">{results.toLocaleString("en-US")}</span>
          <span className="db-cap">about {Math.round(results / ndays)} a day</span></div>
        {revenue != null ? (
          <div><span className="db-cap">Sales from ads</span><span className="db-big">{usd0(revenue)}</span>
            <span className="db-cap">{roas != null ? `${usd(roas)} back for every $1 spent` : "purchase value"}</span></div>
        ) : (
          <div><span className="db-cap">People who clicked</span><span className="db-big">{ctr != null ? (ctr * 100).toFixed(2) + "%" : "–"}</span>
            <span className="db-cap">of everyone who saw an ad</span></div>
        )}
        <div><span className="db-cap">Spent</span><span className="db-big">{usd0(spend)}</span>
          <span className="db-cap">{usd0(spend / ndays)} a day, {spend / ndays <= d.daily_cap ? "under" : "over"} your {usd0(d.daily_cap)} cap</span></div>
      </div>
      <div className="card db-chartbox">
        <div className="db-h"><span className="h3">Cost per {d.result_name}, each day</span><span className="db-cap">Lower is better</span></div>
        <Chart d={d} />
      </div>
    </>
  );
}

function Did({ d }: { d: Ready }) {
  const list = group(d.actions).reverse().slice(0, 4);
  return (
    <div className="card db-did">
      <div className="db-h"><span className="h3">What the team did</span><a className="db-cap" href="#/room">Full log in the Team Room</a></div>
      {list.length === 0 && <div className="db-cap" style={{ padding: "10px 0 12px" }}>Nothing flagged or changed in these days.</div>}
      {list.map((g) => (
        <div className="db-did-i" key={g.a.n}>
          <Av who={g.a.who} />
          <span className="db-did-t"><b>{g.a.who.name}</b> {g.a.verb && g.a.verb + (/^[A-Z]/.test(g.a.text) ? ": " : " ")}{g.a.text}
            {g.a.link && <> <a href={`#/deliverables/${g.a.link.key}`}>{g.a.link.title}</a></>}</span>
          <span className="db-did-d">{days(g)}</span>
        </div>
      ))}
      {d.after && <div className="db-cap db-after">{d.after}</div>}
    </div>
  );
}

function Campaigns({ d, onMore }: { d: Ready; onMore: () => void }) {
  const top = d.target * 1.25;
  const rows = [...d.campaigns].sort((a, b) => (a.cpr ?? Infinity) - (b.cpr ?? Infinity));
  const sub = (c: CampaignRow) => {
    const [, ...rest] = c.name.split(" – ");
    return c.test_id ? <span className="tag b">Testing</span> : <span className="db-cap">{rest.join(" – ") || c.status_label}</span>;
  };
  return (
    <div className="card db-camp">
      <div className="db-h"><span className="h3">Campaigns</span>
        <button className="db-link" onClick={onMore}>Numbers behind this</button></div>
      <table>
        <thead><tr><th>Campaign</th><th className="r">Spent</th><th className="r">Cost per {d.result_name}</th></tr></thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td><div className="nm">{c.name.split(" – ")[0]}</div>{sub(c)}</td>
              <td className="r num">{usd0(c.spend)}</td>
              <td className="r num">{usd(c.cpr)}
                <span className="db-meter" title={`${usd(c.cpr)} against your ${usd0(d.target)} goal`}>
                  <i className={c.cpr != null && c.cpr > d.target ? "over" : ""}
                     style={{ width: `${Math.min(100, ((c.cpr ?? 0) / top) * 100)}%` }} />
                  <b style={{ left: `${(d.target / top) * 100}%` }} /></span></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="db-cap" style={{ margin: "8px 0 4px" }}>The black tick on each bar is your {usd0(d.target)} goal.</div>
    </div>
  );
}

export default function Dashboard({ tick, route, sidebar, items }:
  { tick: number; route: Route; sidebar: ReactNode; items: Deliverable[] }) {
  const range = [7, 14, 28].includes(Number(route.q.get("days"))) ? Number(route.q.get("days")) : 7;
  const { data, error } = useData(() => api.dashboard(range), [tick, range]);
  const [tip, setTip] = useState<string | null>(null);
  const [how, setHow] = useState(false);
  const more = useRef<HTMLDivElement>(null);
  const main = useRef<HTMLElement>(null);
  const d = data && data.ready && !(data.real && data.campaigns.length === 0) ? data : null;
  const waiting = items.find((i) => i.status === "wait");
  useEffect(() => { main.current?.scrollTo({ top: 0 }); }, [range]);
  return (
    <div className="app list">
      {sidebar}
      <main ref={main} className="col scroll top db" style={{ minWidth: 0 }}>
        <div className="db-head">
          <div>
            <div className="eyebrow">{data?.account_name ?? "Sandbox"}{d ? ` · ${onDays(d.start, d.end)}` : ""}</div>
            <div className="h1" style={{ marginTop: 4 }}>Dashboard</div>
          </div>
          <div className="row" style={{ gap: 12 }}>
            {d?.pulled && <span className="mono muted" style={{ fontSize: 11 }}>Pulled by Analyst · {d.pulled.day ? onDay(d.pulled.day) : date(d.pulled.ts)}</span>}
            <div className="seg" role="tablist">
              {[7, 14, 28].map((n) => (
                <button key={n} className={n === range ? "on" : ""} onClick={() => go("dashboard", { days: n === 7 ? null : n })}>{n} days</button>
              ))}
            </div>
          </div>
        </div>
        {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
        {data?.real && !d && <div className="empty">Nothing is live on this account, so there are no numbers yet. Daily numbers appear once an ad set runs.</div>}
        {data && !data.ready && !data.real && (data.waiting
          ? <div className="empty">No day has run yet. Run the first day from Settings, Simulation, and the numbers start here.</div>
          : <div className="empty">The team is setting up the demo week.</div>)}
        {d && (
          <>
            {d.short && <div className="db-cap">
              Only {d.end} {d.end === 1 ? "day has" : "days have"} run, so this shows {onDays(d.start, d.end)}.</div>}
            <Briefing d={d} waiting={waiting} />
            <div className="db-low">
              <Did d={d} />
              <Campaigns d={d} onMore={() => more.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
            </div>
          </>
        )}
        {data && (data.real || data.ready || data.waiting) && (
          <div ref={more} className="db-more">
            <div className="db-h"><span className="h3">Numbers behind this</span>
              <span className="db-cap">The rows the Analyst reads, and the drafts waiting in Ads Manager</span></div>
            {data.real ? (
              <div className="synth">
                <span className="k">Real account</span>
                <span><span className="mono ink">{data.account}</span> · read through Meta's Ads MCP · stays on this computer</span>
              </div>
            ) : (
              <div className="synth">
                <span className="k">Synthetic</span>
                <span>Sandbox account <span className="mono ink">{data.account ?? "act_sandbox_0001"}</span> · Marketing API Insights shape · no real money or people</span>
                <div className="grow" />
                <button className="btn ghost xs" onClick={() => setHow(!how)} aria-expanded={how}>How this is generated</button>
                {how && (
                  <div style={{ flexBasis: "100%", fontSize: 12, lineHeight: 1.5, color: "var(--text)" }}>
                    A seeded simulator plays out each ad's delivery one day at a time: impressions, clicks,
                    {" "}{d?.result_name ?? "result"}s and spend, with fatigue as frequency rises. Each row has the fields
                    {" "}of a Marketing API Insights call at ad level with time_increment=1, and an evidence id the
                    {" "}team cites. The Analyst reads these same rows; the Operator's approved changes feed back into
                    {" "}the next day's delivery.
                  </div>
                )}
              </div>
            )}
            {d && (
              <>
                <div className="kpis">
                  {d.kpis.map((k) => <Tile key={k.key} k={k} d={d} open={tip === k.key} onOpen={setTip} />)}
                </div>
                <div className="card" style={{ padding: "12px 14px 4px", overflowX: "auto" }}>
                  <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                    <span className="h3">Campaigns and ads</span>
                    <span className="mono muted" style={{ fontSize: 10.5 }}>
                      Daily total {usd(d.daily_total, 0)} of the {usd(d.daily_cap, 0)} house cap</span>
                  </div>
                  <Table d={d} />
                </div>
              </>
            )}
            <Drafts tick={tick} />
          </div>
        )}
      </main>
    </div>
  );
}
