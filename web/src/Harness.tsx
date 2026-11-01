// Harness: what each role's model calls cost and saw. Every request is stored by its context
// hash, so any text call opens here as it was sent: sections and budgets, instructions, prompt,
// tools, output schema and the turns after the prompt.
import { useState, type ReactNode } from "react";
import { api, clock, costLabel, onDay, useData, type CallRow, type HarnessData, type Mode, type RoleRow, type SeenCall } from "./api";
import { ADDRESSABLE } from "./avatars";
import { go, type Route } from "./route";
import { Av } from "./ui";

export function roleWho(role: string) {
  const r = ADDRESSABLE.find((a) => a.actor === role);
  return { code: r?.code ?? null, name: r?.name ?? role.replace(/_/g, " ") };
}

const int = (n: number | null | undefined) => (n ?? 0).toLocaleString("en-US");
const pct = (n: number | null | undefined) => `${Math.round((n ?? 0) * 100)}%`;
const usd = (n: number | null | undefined) => `$${(n ?? 0).toFixed(4)}`;

function Tiles({ t, stored }: { t: HarnessData["totals"]; stored: HarnessData["stored"] }) {
  const tiles: [string, string, string][] = [
    ["Model calls", int(t.calls), `${int(stored.requests)} requests stored`],
    ["Input tokens", int(t.input_tokens), `${int(t.cached_tokens)} read from cache`],
    ["Cached share", pct(t.cached_share), "of input tokens"],
    ["Output tokens", int(t.output_tokens), ""],
    ["Cost", usd(t.cost), "at list price"],
    ["Failed calls", int(t.failed), "timeout, schema, provider, cap"],
    ["Trimmed contexts", int(t.trimmed), "sections cut to budget"],
  ];
  return (
    <div className="kpis">
      {tiles.map(([k, v, d]) => (
        <div className="kpi" key={k} style={{ cursor: "default" }}>
          <span className="k">{k}</span><span className="v" style={{ textDecoration: "none" }}>{v}</span>
          <span className="d">{d}</span>
        </div>
      ))}
    </div>
  );
}

function Roles({ rows }: { rows: RoleRow[] }) {
  return (
    <div className="tbl"><table className="use">
      <thead><tr><th>Role</th><th className="n">Calls</th><th className="n">Input</th><th className="n">Cached</th>
        <th className="n">Output</th><th className="n">Cost</th><th className="n">Failed</th>
        <th className="n">Largest inputs</th><th className="n">Trimmed</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.role}>
          <td><div className="row" style={{ gap: 8 }}><Av who={roleWho(r.role)} size="sm" /><span>{roleWho(r.role).name}</span></div></td>
          <td className="n">{r.calls}{r.searches ? <div className="sub">{r.searches} searches</div> : null}</td>
          <td className="n">{int(r.input_tokens)}</td>
          <td className="n">{pct(r.cached_share)}<div className="sub">{int(r.cached_tokens)}</div></td>
          <td className="n">{int(r.output_tokens)}</td>
          <td className="n">{usd(r.cost)}</td>
          <td className="n">{r.failed}{r.failed ? <div className="sub">{Object.entries(r.failures).map(([k, v]) => `${v} ${k}`).join(", ")}</div> : null}</td>
          <td className="n">{int(r.largest_inputs)}<div className="sub">of {int(r.inputs_budget)}</div></td>
          <td className="n">{pct(r.trimmed_share)}{r.flag_larger_budget
            ? <div className="sub" style={{ color: "var(--signal-ink)" }}>needs a larger budget</div>
            : r.trimmed ? <div className="sub">{Object.keys(r.trimmed_sections).join(", ")}</div> : null}</td>
        </tr>
      ))}</tbody>
    </table></div>
  );
}

const BUDGET_COLS: [string, string][] = [
  ["inputs", "Inputs"], ["account_memory", "Account memory"], ["week_stubs", "Week stubs"],
  ["lessons", "Lessons"], ["owner_notes", "Owner notes"], ["journal", "Journal"],
  ["must_keep", "Must keep"], ["total", "Total"],
];

function Budgets({ rows }: { rows: HarnessData["budgets"] }) {
  return (
    <div className="tbl"><table className="use">
      <thead><tr><th>Role</th>{BUDGET_COLS.map(([k, l]) => <th className="n" key={k}>{l}</th>)}</tr></thead>
      <tbody>{rows.map((b) => (
        <tr key={String(b.role)}>
          <td>{roleWho(String(b.role)).name}</td>
          {BUDGET_COLS.map(([k]) => <td className="n" key={k}>{int(b[k] as number)}</td>)}
        </tr>
      ))}</tbody>
    </table></div>
  );
}

function Calls({ rows }: { rows: CallRow[] }) {
  if (!rows.length) return <div className="empty">No model calls yet.</div>;
  return (
    <div className="tbl"><table className="use calls">
      <thead><tr><th>When</th><th>Role</th><th>Topic</th><th className="n">Input</th><th className="n">Cached</th>
        <th className="n">Output</th><th className="n">Cost</th><th /></tr></thead>
      <tbody>{[...rows].reverse().map((c) => (
        <tr key={c.event_id} className={c.inspectable ? "click" : ""}
            onClick={() => c.inspectable && go("harness", { call: c.event_id })}>
          <td className="mono" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>{c.sim_day ? onDay(c.sim_day) : ""} {clock(c.ts)}</td>
          <td>{roleWho(c.role).name}</td>
          <td><span className="mono" style={{ fontSize: 11.5 }}>{c.topic ?? c.model}</span>
            {c.failure && <span className="lbl wait" style={{ marginLeft: 6 }}>{c.failure}</span>}
            {(c.attempt ?? 1) > 1 && <span className="sub"> attempt {c.attempt}</span>}</td>
          <td className="n">{int(c.input_tokens)}</td>
          <td className="n">{int(c.cached_tokens)}</td>
          <td className="n">{int(c.output_tokens)}</td>
          <td className="n">{costLabel(c.cost)}</td>
          <td className="n">{c.inspectable ? <a href={`#/harness?call=${c.event_id}`} onClick={(e) => e.stopPropagation()}>Open</a> : <span className="faint">image</span>}</td>
        </tr>
      ))}</tbody>
    </table></div>
  );
}

function Block({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  const [shown, setShown] = useState(open);
  return (
    <div className="section">
      <button className="sec-h" onClick={() => setShown(!shown)} aria-expanded={shown}>
        <span className="eyebrow">{title}</span><span className="faint mono" style={{ fontSize: 11 }}>{shown ? "Hide" : "Show"}</span>
      </button>
      {shown && children}
    </div>
  );
}

function partText(part: Record<string, unknown>): string {
  const body = part.content ?? part.args ?? "";
  return typeof body === "string" ? body : JSON.stringify(body, null, 1);
}

function Seen({ s, mode }: { s: SeenCall; mode?: Mode }) {
  const c = s.call;
  const output = s.output?.object?.name ?? (s.output as { tools?: { name: string }[] } | null)?.tools?.[0]?.name;
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <Av who={roleWho(s.role)} size="sm" />
        <b className="ink">{roleWho(s.role).name}</b>
        <span className="mono muted" style={{ fontSize: 11.5 }}>{s.model}{mode === "offline" ? ", offline stand-in" : s.provider ? ` on ${s.provider}` : ""}
          {s.sim_day ? ` · ${onDay(s.sim_day)}` : ""} · {clock(s.ts)} · attempt {c.attempt ?? 1}</span>
        {c.failure && <span className="lbl wait">{String(c.failure)}</span>}
      </div>
      <dl className="kv seen-kv">
        <dt>Input tokens</dt><dd>{int(c.input_tokens)} ({int(c.cached_tokens)} cached)</dd>
        <dt>Output tokens</dt><dd>{int(c.output_tokens)}</dd>
        <dt>Cost</dt><dd>{usd(c.cost)}</dd>
        <dt>Deadline</dt><dd>{c.deadline_s ? `${c.deadline_s} s` : "none"}</dd>
        <dt>Output</dt><dd>{output ?? "text"}{s.output?.mode ? ` (${s.output.mode})` : ""}</dd>
        <dt>Context hash</dt><dd title={s.context_hash ?? ""}>{s.context_hash?.slice(0, 16)}</dd>
      </dl>
      <div className="section">
        <div className="eyebrow">Context sections</div>
        <div className="tbl"><table className="use">
          <thead><tr><th>Section</th><th>Where</th><th className="n">Tokens</th><th className="n">Budget</th><th style={{ paddingLeft: 16 }}>Trimmed</th></tr></thead>
          <tbody>{s.sections.map((x) => (
            <tr key={x.name}>
              <td className="mono" style={{ fontSize: 12 }}>{x.name.replace(/_/g, " ")}</td>
              <td className="muted">{x.place}{x.must_keep ? ", must keep" : ""}</td>
              <td className="n">{int(x.tokens)}</td>
              <td className="n">{x.budget ? int(x.budget) : ""}</td>
              <td className="muted" style={{ fontSize: 12, paddingLeft: 16 }}>{x.dropped
                ? `${x.dropped} item(s) cut from ${int(x.before_tokens)} tokens${x.left_out.length ? `: ${x.left_out.slice(0, 4).join(", ")}` : ""}`
                : ""}</td>
            </tr>
          ))}</tbody>
        </table></div>
      </div>
      <Block title="Instructions"><pre className="pre">{s.instructions}</pre></Block>
      <Block title="Prompt" open><pre className="pre">{s.prompt}</pre></Block>
      {(s.tools.length > 0 || s.native_tools.length > 0) && (
        <Block title={`Tools (${s.tools.length + s.native_tools.length})`}>
          <div className="col" style={{ gap: 6 }}>
            {s.tools.map((t) => <div key={t.name} style={{ fontSize: 12.5 }}><span className="mono ink">{t.name}</span> <span className="muted">{t.description}</span></div>)}
            {s.native_tools.map((t, i) => <div key={i} className="mono muted" style={{ fontSize: 12 }}>hosted {JSON.stringify(t)}</div>)}
          </div>
        </Block>
      )}
      {s.attachments.length > 0 && (
        <div className="muted" style={{ fontSize: 12 }}>{s.attachments.length} image attachment(s): {s.attachments.map((a) => `${a.media_type}, ${int(a.bytes)} bytes`).join("; ")}</div>
      )}
      {s.turns.length > 0 && (
        <Block title={`Turns after the prompt (${s.turns.length})`} open>
          <div className="col" style={{ gap: 6 }}>
            {s.turns.flatMap((t, i) => t.parts.map((p, j) => (
              <div key={`${i}-${j}`} className="turn">
                <span className="mono faint">{t.kind} · {String(p.part_kind ?? "")}{p.tool_name ? ` · ${String(p.tool_name)}` : ""}</span>
                <pre className="pre sm">{partText(p).slice(0, 4000)}</pre>
              </div>
            )))}
          </div>
        </Block>
      )}
    </div>
  );
}

function Inspector({ route, mode }: { route: Route; mode?: Mode }) {
  const topic = route.q.get("topic");
  const asked = Number(route.q.get("call")) || null;
  const list = useData(() => (topic ? api.calls(topic) : Promise.resolve({ items: [] as CallRow[] })), [topic]);
  const attempts = (list.data?.items ?? []).filter((c) => c.inspectable);
  const id = asked ?? attempts[attempts.length - 1]?.event_id ?? null;
  const seen = useData(() => (id ? api.seen(id) : Promise.resolve(null)), [id]);
  const s = seen.data && seen.data.event_id === id ? seen.data : null;
  const error = seen.error ?? list.error;
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="crumb">
          <a href="#/harness">Harness</a><span>/</span>
          {topic ? <a href={`#/deliverables/${topic}`}>{topic}</a> : <span>Call {id}</span>}
          <span>/</span><b>What the model saw</b>
        </div>
        <button className="btn ghost xs" onClick={() => (topic ? go("deliverables/" + topic) : go("harness"))}>Close</button>
      </div>
      {attempts.length > 1 && (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <span className="eyebrow">Calls on this deliverable</span>
          <div className="seg">
            {attempts.map((c, i) => (
              <button key={c.event_id} className={c.event_id === id ? "on" : ""}
                      onClick={() => go("harness", { topic, call: c.event_id })}>
                {i + 1}. {roleWho(c.role).name}{c.failure ? ` (${c.failure})` : ""}
              </button>
            ))}
          </div>
        </div>
      )}
      {topic && list.data && !attempts.length && <div className="empty">No stored request for this deliverable.</div>}
      {error && <div className="empty err">{error}</div>}
      {s && <Seen s={s} mode={mode} />}
      {!s && !error && id && <div className="empty">Loading</div>}
    </div>
  );
}

export default function Harness({ tick, route, sidebar, mode }: { tick: number; route: Route; sidebar: ReactNode; mode?: Mode }) {
  const inspecting = route.q.get("call") || route.q.get("topic");
  const { data, error } = useData(() => (inspecting ? Promise.resolve(null) : api.harness()), [tick, !!inspecting]);
  return (
    <div className="app list">
      {sidebar}
      <main className="col scroll top memh" style={{ padding: "18px 28px", gap: 16, minWidth: 0 }}>
        {inspecting ? <Inspector route={route} mode={mode} /> : (
          <>
            <div>
              <div className="eyebrow">Every model call, by role</div>
              <div className="h1" style={{ marginTop: 4 }}>Harness</div>
              <div className="muted" style={{ fontSize: 12.5, marginTop: 4, maxWidth: 760 }}>
                What each role's calls cost and how much of their input was read from the cache. Each role's context is
                assembled from fixed sections with token budgets; a section over budget is trimmed and the trim is logged.
                Every request is stored by its context hash, so any text call opens as it was sent.
              </div>
            </div>
            {error && <div className="empty err">{error}</div>}
            {data && (
              <>
                <Tiles t={data.totals} stored={data.stored} />
                <section className="section"><div className="h2">By role</div><Roles rows={data.roles} /></section>
                <section className="section"><div className="h2">Recent calls</div><Calls rows={data.recent} /></section>
                <section className="section">
                  <div className="h2">Context budgets</div>
                  <div className="muted" style={{ fontSize: 12.5 }}>
                    Tokens per section. A role whose contexts are trimmed more than {pct(data.trim_flag_share)} of the time is flagged for a larger budget.
                  </div>
                  <Budgets rows={data.budgets} />
                </section>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}
