// Memory: what the team keeps from week to week. The account memory is shared; each role's
// lessons are private to it. The owner's edits win and lock the line, and a role can only propose
// a change to a locked line. Every line cites its sources; code checks each one before it is kept.
import { useState, type ReactNode } from "react";
import {
  api, onDay, READ_ONLY, REPLAY, useData, type FactProposal, type JournalEntry, type MemLine,
  type MemoryData, type MemoryEdit, type MemStore, type Note,
} from "./api";
import { roleWho } from "./Harness";
import { go, type Route } from "./route";
import { Av, OWNER } from "./ui";

const ROLES = ["growth_lead", "analyst", "media_buyer", "producer", "reviewer", "strategist"];
const TABS = [
  ["account", "Account memory"], ["lessons", "Lessons"], ["weeks", "Week records"],
  ["notes", "Owner notes"], ["proposals", "Proposals"], ["checks", "Checks"],
] as const;
const STATUS_LABEL: Record<MemLine["status"], [string, string]> = {
  active: ["lbl ok", "Active"], watch: ["lbl review", "On watch"],
  inactive: ["lbl draft", "Expired"], struck: ["lbl stale", "Struck"],
};

type Act = (fn: () => Promise<unknown>) => Promise<boolean>;

function Sources({ ids }: { ids: string[] }) {
  return <>{ids.map((s) => <span key={s} className="chip" title={s}>{s}</span>)}</>;
}

function LineRow({ line, store, role, act, busy }: {
  line: MemLine; store: "account" | "lessons"; role: string | null; act: Act; busy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(line.text);
  const [cls, label] = STATUS_LABEL[line.status];
  const edit = (body: Partial<MemoryEdit>) => act(() => api.editMemory({ store, role, line_id: line.id, ...body } as MemoryEdit));
  const off = line.status === "struck" || line.status === "inactive";
  return (
    <div className={off ? "mline off" : "mline"}>
      <div className="col" style={{ gap: 5, minWidth: 0 }}>
        {editing ? (
          <div className="note-box">
            <textarea autoFocus value={text} onChange={(e) => setText(e.target.value)} />
            <div className="row" style={{ gap: 8 }}>
              <button className="btn ink xs" disabled={busy || !text.trim()}
                      onClick={async () => { if (await edit({ action: "edit", text })) setEditing(false); }}>Save</button>
              <button className="btn ghost xs" onClick={() => { setEditing(false); setText(line.text); }}>Cancel</button>
              <span className="muted" style={{ fontSize: 12 }}>Your version wins and locks the line.</span>
            </div>
          </div>
        ) : <div className="mline-t">{line.text}</div>}
        <div className="mline-m">
          <span className="mono faint">{line.id}</span>
          <span className={cls}>{label}</span>
          {line.locked && <span className="lbl wait" title="Your line: a role can only propose a change to it">{OWNER}'s</span>}
          {!line.locked && line.author !== "system" && <span className="faint">by {roleWho(line.author).name}</span>}
          {line.created_week != null && <span className="faint">week {line.created_week}{line.last_confirmed_week && line.last_confirmed_week !== line.created_week ? `, confirmed week ${line.last_confirmed_week}` : ""}</span>}
          <Sources ids={line.sources} />
        </div>
        {line.flag && <div className="mline-flag">Flag: {line.flag}</div>}
      </div>
      {!REPLAY && !editing && (
        <div className="row" style={{ gap: 6, alignSelf: "flex-start" }}>
          {line.status !== "struck" && <button className="btn ghost xs" disabled={busy} onClick={() => setEditing(true)}>Edit</button>}
          {line.status === "struck"
            ? <button className="btn ghost xs" disabled={busy} onClick={() => edit({ action: "restore" })}>Restore</button>
            : <button className="btn ghost xs" disabled={busy} onClick={() => edit({ action: "strike" })}>Strike</button>}
        </div>
      )}
    </div>
  );
}

function AddLine({ store, role, act, busy, hint }: { store: "account" | "lessons"; role: string | null; act: Act; busy: boolean; hint: string }) {
  const [text, setText] = useState("");
  if (REPLAY) return null;
  return (
    <div className="note-box">
      <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder={hint} />
      <div className="row" style={{ gap: 8 }}>
        <button className="btn ink xs" disabled={busy || !text.trim()}
                onClick={async () => { if (await act(() => api.editMemory({ store, role, action: "add", text }))) setText(""); }}>Add line</button>
        <span className="muted" style={{ fontSize: 12 }}>Your lines are kept as written and only you can change them.</span>
      </div>
    </div>
  );
}

function Versions({ store }: { store: MemStore }) {
  if (!store.history.length) return null;
  return (
    <div className="section">
      <div className="eyebrow">Versions</div>
      {[...store.history].reverse().slice(0, 8).map((v) => (
        <div key={v.version} className="ver">
          <span className="mono ink">v{v.version}</span>
          <span className="mono faint">{v.week ? `week ${v.week}` : ""}</span>
          <span>{v.reason}</span>
          <span className="faint">{v.lines} line{v.lines === 1 ? "" : "s"}</span>
        </div>
      ))}
    </div>
  );
}

function Store({ store, kind, role, act, busy, hint }: {
  store: MemStore; kind: "account" | "lessons"; role: string | null; act: Act; busy: boolean; hint: string;
}) {
  const order = { active: 0, watch: 1, inactive: 2, struck: 3 };
  const lines = [...store.lines].sort((a, b) => order[a.status] - order[b.status]);
  return (
    <div className="col" style={{ gap: 12 }}>
      {lines.length ? <div className="mlines">{lines.map((l) => <LineRow key={l.id} line={l} store={kind} role={role} act={act} busy={busy} />)}</div>
        : <div className="empty">Nothing kept yet. Lines appear when a week closes.</div>}
      <AddLine store={kind} role={role} act={act} busy={busy} hint={hint} />
      <Versions store={store} />
    </div>
  );
}

function Journal({ role, tick }: { role: string; tick: number }) {
  const { data } = useData(() => api.journal(role), [role, tick]);
  const entries: JournalEntry[] = [...(data?.entries ?? [])].reverse().slice(0, 12);
  if (!entries.length) return null;
  return (
    <div className="section">
      <div className="eyebrow">{roleWho(role).name}'s journal · latest {entries.length}</div>
      <div className="tbl"><table className="use">
        <thead><tr><th>When</th><th>Work</th><th>Decided</th><th>Outcomes</th></tr></thead>
        <tbody>{entries.map((e) => (
          <tr key={e.id}>
            <td className="mono" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>{e.day ? onDay(e.day) : ""}<div className="sub">{e.id}</div></td>
            <td className="mono" style={{ fontSize: 11.5 }}>{e.topic}</td>
            <td>{e.decided}</td>
            <td>{e.outcomes.map((o) => (
              <span key={o.id} className={o.good === false ? "pill bad" : o.good ? "pill good" : "pill"}
                    title={o.detail}>{o.outcome.replace(/_/g, " ")}{o.simulated ? " (sim)" : ""}</span>
            ))}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </div>
  );
}

function Weeks({ m }: { m: MemoryData }) {
  const [open, setOpen] = useState<number | null>(null);
  const record = useData(() => (open ? api.week(open) : Promise.resolve(null)), [open]);
  if (!m.weeks.length) return <div className="empty">No week has closed yet. A week closes when the next weekly review starts.</div>;
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="muted" style={{ fontSize: 12.5 }}>Frozen when the week closes and never edited. Roles read the one-line stub; the full record is a tool call away.</div>
      {[...m.weeks].reverse().map((w) => (
        <div key={w.week} className="card" style={{ padding: "10px 12px" }}>
          <button className="sec-h" onClick={() => setOpen(open === w.week ? null : w.week)}>
            <span><b className="ink">Week {w.week}</b> <span className="mono faint" style={{ fontSize: 11 }}>wk-{w.week}</span></span>
            <span className="faint mono" style={{ fontSize: 11 }}>{open === w.week ? "Hide" : "Open"}</span>
          </button>
          <div style={{ fontSize: 12.5, marginTop: 4 }}>{w.stub}</div>
          {open === w.week && record.data && record.data.week === w.week && (
            <div className="col" style={{ gap: 8, marginTop: 10 }}>
              {record.data.readouts.length > 0 && (
                <div className="tbl"><table className="use">
                  <thead><tr><th>Test</th><th>Angle</th><th>Read out</th><th>Why</th></tr></thead>
                  <tbody>{record.data.readouts.map((r) => (
                    <tr key={r.test_id}><td><span className="mono">{r.test_id}</span><div className="sub">{r.name}</div></td>
                      <td>{r.angle}{r.exploratory ? <div className="sub">exploratory</div> : null}</td>
                      <td className="mono">{r.recommendation}</td><td>{r.rationale}</td></tr>
                  ))}</tbody>
                </table></div>
              )}
              <div className="muted" style={{ fontSize: 12 }}>
                {record.data.changes.reduce((n, c) => n + c.applied.length, 0)} change(s) applied in {record.data.changes.length} change set(s)
                {" · "}owner approved {record.data.owner.approved}, sent back {record.data.owner.sent_back}
                {record.data.owner.simulated ? ` (plus ${record.data.owner.simulated} simulated)` : ""}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function route(n: Note): string {
  const hops = n.routed.map((r) => roleWho(r.to).name);
  return [OWNER, ...hops].join(" → ");
}

function Notes({ notes }: { notes: Note[] }) {
  if (!notes.length) return <div className="empty">No owner notes yet. A message in the Team Room with @Role goes to that role; without one it goes to the Growth Lead, who hands it on or answers.</div>;
  return (
    <div className="tbl"><table className="use">
      <thead><tr><th>Note</th><th>Went to</th><th>Seen by</th><th>Status</th></tr></thead>
      <tbody>{[...notes].reverse().map((n) => (
        <tr key={n.id}>
          <td>{n.text}<div className="sub">{n.id} · {n.kind === "send_back" ? "send-back" : "message"}{n.day ? ` · ${onDay(n.day)}` : ""}</div></td>
          <td style={{ whiteSpace: "nowrap" }}>{route(n)}</td>
          <td>{n.seen_by.map((r) => roleWho(r).name).join(", ")}</td>
          <td>{n.done ? <span className="lbl ok" title={n.done.how}>Done</span> : <span className="lbl wait">Open</span>}
            {n.done && <div className="sub">{n.done.how}</div>}</td>
        </tr>
      ))}</tbody>
    </table></div>
  );
}

function factValue(v: unknown): string {
  if (Array.isArray(v)) return v.join("; ");
  if (v === "" || v == null) return "(empty)";
  return String(v);
}

function Proposals({ m, act, busy }: { m: MemoryData; act: Act; busy: boolean }) {
  const facts = [...m.fact_proposals].reverse();
  const open = facts.filter((f) => !f.resolved);
  const decided = facts.filter((f) => f.resolved);
  const decide = (fn: () => Promise<unknown>) => act(fn);
  const Buttons = ({ onYes, onNo }: { onYes: () => void; onNo: () => void }) => REPLAY ? null : (
    <div className="row" style={{ gap: 6 }}>
      <button className="btn pri xs" disabled={busy} onClick={onYes}>Accept</button>
      <button className="btn ghost xs" disabled={busy} onClick={onNo}>Reject</button>
    </div>
  );
  return (
    <div className="col" style={{ gap: 16 }}>
      <section className="section">
        <div className="h2">Changes to your lines</div>
        <div className="muted" style={{ fontSize: 12.5 }}>A role cannot change a line you wrote or edited. When its weekly pass would, it asks here.</div>
        {m.proposals.length ? m.proposals.map((p) => (
          <div key={p.id} className="prop">
            <div className="col" style={{ gap: 4, minWidth: 0 }}>
              <div className="mono faint" style={{ fontSize: 11 }}>{p.store === "account" ? "Account memory" : `${roleWho(p.role ?? "").name}'s lessons`} · {p.line_id} · from {roleWho(p.by).name}</div>
              <div className="was">{p.was}</div>
              <div>{p.text ? p.text : <i className="muted">Strike this line.</i>}</div>
              {p.sources.length > 0 && <div><Sources ids={p.sources} /></div>}
            </div>
            <Buttons onYes={() => decide(() => api.decideMemory(p.id, true))} onNo={() => decide(() => api.decideMemory(p.id, false))} />
          </div>
        )) : <div className="empty" style={{ padding: "8px 0" }}>None waiting.</div>}
      </section>
      <section className="section">
        <div className="h2">Changes to the business facts</div>
        <div className="muted" style={{ fontSize: 12.5 }}>The Growth Lead and the Analyst may propose a change to a fact, with a reason and evidence. Only you can make it.</div>
        {open.length ? open.map((f) => <FactRow key={f.id} f={f} buttons={
          <Buttons onYes={() => decide(() => api.decideFact(f.id, true))} onNo={() => decide(() => api.decideFact(f.id, false))} />} />)
          : <div className="empty" style={{ padding: "8px 0" }}>None waiting.</div>}
        {decided.length > 0 && <div className="eyebrow" style={{ marginTop: 8 }}>Decided</div>}
        {decided.map((f) => <FactRow key={f.id} f={f} buttons={
          <span className={f.resolved?.accepted ? "lbl ok" : "lbl draft"}>{f.resolved?.accepted ? "Accepted" : "Rejected"}</span>} />)}
      </section>
    </div>
  );
}

function FactRow({ f, buttons }: { f: FactProposal; buttons: ReactNode }) {
  return (
    <div className="prop">
      <div className="col" style={{ gap: 4, minWidth: 0 }}>
        <div className="mono faint" style={{ fontSize: 11 }}>e-{f.id} · {roleWho(f.role ?? f.by ?? "").name}{f.sim_day ? ` · ${onDay(f.sim_day)}` : ""}</div>
        <div><b className="ink">{f.key.replace(/_/g, " ")}</b>: <span className="was">{factValue(f.was)}</span> → {factValue(f.value)}</div>
        <div className="muted" style={{ fontSize: 12.5 }}>{f.reason}</div>
        {f.evidence_ids && f.evidence_ids.length > 0 && <div><Sources ids={f.evidence_ids} /></div>}
      </div>
      {buttons}
    </div>
  );
}

function Checks({ m }: { m: MemoryData }) {
  const rejected = [...m.rejected].reverse();
  return (
    <div className="col" style={{ gap: 16 }}>
      <section className="section">
        <div className="h2">Lines not kept</div>
        <div className="muted" style={{ fontSize: 12.5 }}>
          Every proposed line is checked in code (sources exist and belong to the role, enough outcomes behind it, nothing that loosens a rule or reads like an instruction), then by the Reviewer.
        </div>
        {rejected.length ? (
          <div className="tbl"><table className="use">
            <thead><tr><th>Line</th><th>Whose</th><th>Why not</th><th>By</th></tr></thead>
            <tbody>{rejected.map((r) => (
              <tr key={r.id}>
                <td>{r.text}<div className="sub">week {r.week} · <Sources ids={r.sources.slice(0, 4)} /></div></td>
                <td style={{ whiteSpace: "nowrap" }}>{r.store === "account" ? "Account" : roleWho(r.role ?? "").name}</td>
                <td>{r.problems.join("; ")}</td>
                <td className="mono">{r.by}</td>
              </tr>
            ))}</tbody>
          </table></div>
        ) : <div className="empty" style={{ padding: "8px 0" }}>Every proposed line was kept.</div>}
      </section>
      <section className="section">
        <div className="h2">Rollbacks</div>
        <div className="muted" style={{ fontSize: 12.5 }}>A lessons version is judged on the outcomes after it goes live, and rolled back if quality falls.</div>
        {m.reverted.length ? m.reverted.map((r) => (
          <div key={r.id} className="ver"><span className="ink">{roleWho(r.role).name}</span>
            <span className="mono faint">v{r.from_version} → v{r.to_version}</span><span>{r.reason}</span></div>
        )) : <div className="empty" style={{ padding: "8px 0" }}>None.</div>}
      </section>
      {(m.collapse.length > 0 || m.failed.length > 0) && (
        <section className="section">
          <div className="h2">Warnings</div>
          {m.collapse.map((c) => <div key={c.id} className="ver"><span className="ink">{roleWho(c.role).name}</span><span>{c.reason}</span></div>)}
          {m.failed.map((f) => <div key={f.id} className="ver"><span className="ink">Week {f.week}</span><span>The {f.pass} pass failed: {f.error}</span></div>)}
        </section>
      )}
    </div>
  );
}

export default function Memory({ tick, busy, route: r, sidebar }: { tick: number; busy: string | null; route: Route; sidebar: ReactNode }) {
  const [bump, setBump] = useState(0);
  const { data, error } = useData(api.memory, [tick, bump]);
  const [working, setWorking] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const tab = r.q.get("tab") ?? "account";
  const role = r.q.get("role") ?? "media_buyer";
  const act: Act = async (fn) => {
    setWorking(true); setFailure(null);
    try { await fn(); setBump((b) => b + 1); return true; }
    catch (e) { setFailure((e as Error).message); return false; }
    finally { setWorking(false); }
  };
  const blocked = working || !!busy;
  const waiting = data ? data.proposals.length + data.fact_proposals.filter((f) => !f.resolved).length : 0;
  let body: ReactNode = null;
  if (data) {
    if (tab === "account") body = (
      <>
        <div className="muted" style={{ fontSize: 12.5 }}>Shared by every role. The Growth Lead merges each closed week into it; every line cites evidence, the week record or your note.</div>
        <Store store={data.account} kind="account" role={null} act={act} busy={blocked}
               hint="Add a line every role should know, for example a season, a product launch or what the audience responds to." />
      </>
    );
    else if (tab === "lessons") {
      const store = data.lessons[role];
      body = (
        <>
          <div className="seg" style={{ alignSelf: "flex-start" }}>
            {ROLES.map((x) => {
              const n = data.lessons[x]?.lines.filter((l) => l.status === "active" || l.status === "watch").length ?? 0;
              return <button key={x} className={x === role ? "on" : ""} onClick={() => go("memory", { tab: "lessons", role: x })}>
                {roleWho(x).name}{n ? ` · ${n}` : ""}</button>;
            })}
          </div>
          <div className="row" style={{ gap: 10 }}>
            <Av who={roleWho(role)} size="sm" />
            <div className="muted" style={{ fontSize: 12.5 }}>
              Private to the {roleWho(role).name}. A lesson needs two independent outcomes to go active (one puts it on watch), expires after three weeks unconfirmed, and is rolled back if quality falls.
            </div>
          </div>
          {store && <Store store={store} kind="lessons" role={role} act={act} busy={blocked}
                           hint={`Add a lesson for the ${roleWho(role).name}. Only you can say "never".`} />}
          <Journal role={role} tick={tick + bump} />
        </>
      );
    } else if (tab === "weeks") body = <Weeks m={data} />;
    else if (tab === "notes") body = <Notes notes={data.notes} />;
    else if (tab === "proposals") body = <Proposals m={data} act={act} busy={blocked} />;
    else body = <Checks m={data} />;
  }
  return (
    <div className="app list">
      {sidebar}
      <main className="col scroll top memh" style={{ padding: "18px 28px", gap: 14, minWidth: 0 }}>
        <div>
          <div className="eyebrow">What the team keeps{data?.closed.length ? ` · ${data.closed.length} week${data.closed.length === 1 ? "" : "s"} closed` : ""}</div>
          <div className="h1" style={{ marginTop: 4 }}>Memory</div>
        </div>
        <div className="tabs rule-b">
          {TABS.map(([k, label]) => (
            <a key={k} className={tab === k ? "tab on" : "tab"} href={`#/memory?tab=${k}`} style={{ textDecoration: "none" }}>
              {label}{k === "proposals" && waiting > 0 && <span className="count">{waiting}</span>}
            </a>
          ))}
        </div>
        {REPLAY && <div className="note">{READ_ONLY}</div>}
        {(failure || error) && <div className="err" style={{ fontSize: 12.5 }}>{failure ?? error}</div>}
        {data && data.closed.length === 0 && tab !== "notes" && tab !== "proposals" && (
          <div className="muted" style={{ fontSize: 12.5 }}>No week has closed yet{data.weeks.length ? "" : `; the first closes when the next weekly review starts`}.</div>
        )}
        {body}
        {data && data.charter && tab === "account" && (
          <div className="faint" style={{ fontSize: 12 }}>The charter every role reads first is the business facts, version {data.charter_version}. <a href="#/settings?s=facts">Edit it in Settings</a>.</div>
        )}
      </main>
    </div>
  );
}
