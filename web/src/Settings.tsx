// Settings: the Meta connection and how much the team may do, the house rules, the business facts
// the team works from, what the models cost, and the controls that move the simulated week forward.
import { Fragment, useState, type ReactNode } from "react";
import { Connection, FactsForm, HouseRulesForm } from "./Access";
import { ProfileForm } from "./Profile";
import { api, money, onDay, useData, type AccessData, type SettingsData } from "./api";
import type { Route } from "./route";
import { Av } from "./ui";

function rows(obj: Record<string, unknown>): [string, string][] {
  return Object.entries(obj).flatMap(([k, v]): [string, string][] => {
    const label = k.replace(/_/g, " ");
    if (v && typeof v === "object" && !Array.isArray(v)) return rows(v as Record<string, unknown>);
    if (Array.isArray(v)) return [[label, v.join("; ")]];
    if (k.endsWith("budget") || k.startsWith("target_cost") || k.endsWith("_cap")) return [[label, money(v as number)]];
    if (k.endsWith("_pct") || k.endsWith("_share")) return [[label, `${Math.round((v as number) * 100)}%`]];
    return [[label, String(v)]];
  });
}

// Edited in the form above the list.
const NUMBERS = new Set(["weekly budget", "test budget share", "target cost per result"]);

const SECTIONS = [
  { key: "connection", label: "Meta connection" },
  { key: "rules", label: "House rules" },
  { key: "facts", label: "Business facts" },
  { key: "models", label: "Models and spend" },
  { key: "simulation", label: "Simulation" },
  { key: "profile", label: "Your profile" },
] as const;

export default function Settings({ tick, busy, sidebar, route, access, onAccess, named }: {
  tick: number; busy: string | null; sidebar: ReactNode; route: Route;
  access: AccessData | null; onAccess: () => void; named: boolean;
}) {
  const { data } = useData(api.settings, [tick]);
  const [error, setError] = useState<string | null>(null);
  const run = (fn: () => Promise<unknown>) => { setError(null); fn().catch((e: Error) => setError(e.message)); };
  const s = route.q.get("s") ?? "connection";
  const meta = route.q.get("meta");
  const where = access?.connection?.name ?? "No account connected";
  return (
    <div className="app list">
      {sidebar}
      <main className="col scroll top" style={{ padding: "18px 28px", gap: 16, minWidth: 0 }}>
        <div>
          <div className="eyebrow">{where}{data?.day ? ` · ${onDay(data.day)}` : ""}</div>
          <div className="h1" style={{ marginTop: 4 }}>Settings</div>
        </div>
        {meta === "cancelled" && <div className="note">Facebook Login was cancelled. Nothing was connected.</div>}
        {meta === "failed" && <div className="note err">Meta did not return an ad account. Nothing was connected; try again.</div>}
        <div className="set-wrap">
          <nav className="snav" aria-label="Settings">
            {SECTIONS.map((x) => (
              <a key={x.key} className={s === x.key ? "on" : ""} href={`#/settings?s=${x.key}`}>{x.label}</a>
            ))}
          </nav>
          <div className="col grow" style={{ gap: 16, minWidth: 0, maxWidth: 860 }}>
            {s === "connection" && access && <Connection a={access} onChange={onAccess} />}
            {s === "profile" && <ProfileForm named={named} />}
            {s === "rules" && access && (
              <section className="section">
                <div className="h2">House rules and budget caps</div>
                <HouseRulesForm a={access} onChange={onAccess} />
              </section>
            )}
            {s === "simulation" && (
              <section className="section" style={{ maxWidth: 640 }}>
                <div className="h2">Simulation</div>
                <div className="muted" style={{ fontSize: 12.5 }}>
                  The account is a synthetic sandbox. {data?.offline
                    ? "Each step runs the team on an offline stand-in that answers in the same shape as Muse, at no cost."
                    : `Each step calls the real models and is charged at list price, up to the $${data?.cap ?? ""} cap.`} Nothing reaches a real ad account.
                </div>
                <div className="row" style={{ gap: 8, marginTop: 4, flexWrap: "wrap" }}>
                  <button className="btn ink" disabled={!!busy} onClick={() => run(api.runDay)}>Run the next day</button>
                  <button className="btn ghost" disabled={!!busy} onClick={() => run(api.runWeek)}>Start the weekly review</button>
                  {busy && <span className="status-bar"><span className="dot work" />{busy}</span>}
                  {error && <span className="err" style={{ fontSize: 12 }}>{error}</span>}
                </div>
              </section>
            )}
            {s === "models" && data && <Usage data={data} />}
            {s === "facts" && data && (
              <section className="section">
                <div className="h2">Business facts · version {data.fact_version}</div>
                <FactsForm facts={data.facts as unknown as Parameters<typeof FactsForm>[0]["facts"]}
                           version={data.fact_version} onChange={onAccess} />
                <dl className="kv facts">
                  {rows(data.facts).filter(([k]) => !NUMBERS.has(k))
                    .map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}
                </dl>
              </section>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

const MODE_TEXT = {
  offline: "Offline stand-in. No model is called; costs are what the same tokens would cost at list price.",
  live: "Live on the Meta API: Muse Spark for every text role, Muse Image and SAM 3.1 for creative.",
  mixed: "Live: every role on Muse Spark Open, the open-weights model, on Baseten. Web search, Muse Image and SAM 3.1 on the Meta API. Built with Muse Spark Open.",
};

function Usage({ data }: { data: SettingsData }) {
  const used = data.cap ? Math.min(data.cost / data.cap, 1) : 0;
  return (
    <section className="section">
      <div className="h2">Models and spend</div>
      <div className="muted" style={{ fontSize: 12.5 }}>{MODE_TEXT[data.mode]}</div>
      {data.cap != null && (
        <div style={{ maxWidth: 420 }}>
          <div className="row mono" style={{ justifyContent: "space-between", fontSize: 11.5 }}>
            <span>${data.cost.toFixed(2)} of ${data.cap} spend cap</span>
            <span className="faint">${(data.remaining ?? 0).toFixed(2)} left</span>
          </div>
          <div className="capbar"><i style={{ width: `${used * 100}%` }} /></div>
        </div>
      )}
      <table className="use">
        <thead><tr><th>Role</th><th>Model</th><th className="n">Calls</th><th className="n">Tokens</th>
          <th className="n">Time</th><th className="n">Cost</th></tr></thead>
        <tbody>
          {data.usage.map((u) => (
            <tr key={u.who.actor}>
              <td><div className="row" style={{ gap: 8 }}><Av who={u.who} size="sm" /><span>{u.who.name}</span></div></td>
              <td>{u.models.map((m) => (
                <div key={m.model}><span className="mono" style={{ fontSize: 12 }}>{m.model.replace(/^meta-models\//, "")}</span>
                  <div className="sub">{data.offline ? "offline stand-in" : m.provider} · {m.calls} {m.calls === 1 ? "call" : "calls"}</div></div>
              ))}</td>
              <td className="n">{u.calls}{u.failed ? <div className="sub">{u.failed} failed</div> : null}</td>
              <td className="n">{u.tokens.toLocaleString("en-US")}{u.searches ? <div className="sub">{u.searches} searches</div> : null}</td>
              <td className="n">{u.seconds}s</td>
              <td className="n">${u.cost.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot><tr><td>All roles</td><td /><td className="n">{data.model_calls}</td>
          <td className="n">{data.tokens.toLocaleString("en-US")}</td><td />
          <td className="n">${data.cost.toFixed(4)}</td></tr></tfoot>
      </table>
    </section>
  );
}
