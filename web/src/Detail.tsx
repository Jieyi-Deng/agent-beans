// 4c A deliverable under Deliverables. The breadcrumb says where it lives; the right column
// carries its discussion and links back to the room thread. Approving binds to this version.
import { useState, type ReactNode } from "react";
import { api, clock, modelLabel, money, when, type Detail as D, type Evidence, type Image, type Mode } from "./api";
import Composer from "./Composer";
import { TABS } from "./Deliverables";
import { go } from "./route";
import { Av, Lbl, Text } from "./ui";

const WORDS = ["no", "one", "two", "three", "four", "five", "six"];
const count = (n: number) => WORDS[n] ?? String(n);

function headline(d: D): string {
  if (d.ads?.length) {
    const camps = [...new Set(d.ads.map((a) => a.campaign))];
    return `Ship ${count(d.ads.length)} new ${d.ads.length === 1 ? "ad" : "ads"} to ${camps.join(" and ")}`;
  }
  return d.headline;
}

function note(d: D): string {
  // The approval wording only applies while this version waits on the owner.
  const bind = d.status === "wait"
    ? " Approving binds to this exact version; any edit after approval voids it." : "";
  if (d.ads?.length) return `${d.ads.length === 2 ? "Both are" : "All are"} created paused.${bind}`;
  if (d.changes) return `Only the changes the house rules allow are applied.${bind}`;
  if (d.kind === "TestPlan" && bind) return `Approving starts creative production for these tests.${bind}`;
  return d.sub;
}

function Chip({ id, evidence }: { id: string; evidence: Evidence[] }) {
  const ev = evidence.find((e) => e.id === id);
  const tip = ev ? ev.note + "\n" + Object.entries(ev.values).map(([k, v]) => `${k}: ${v}`).join("\n") : id;
  return <span className="chip" title={tip}>{id}</span>;
}

function Images({ images, caption }: { images: Image[]; caption: (i: Image) => string }) {
  const shown = images.filter((i) => i.url);
  if (!shown.length) return null;
  return (
    <div className="imgs">
      {shown.map((i) => (
        <figure key={i.url!}>
          <img src={i.url!} alt={caption(i)} />
          <figcaption>{caption(i)}</figcaption>
        </figure>
      ))}
    </div>
  );
}

function Body({ d }: { d: D }) {
  const parts: ReactNode[] = [];
  if (d.ads) {
    parts.push(
      <table className="tbl" key="ads">
        <thead><tr><th>Ad</th><th>Test</th><th>Campaign</th><th className="num">Week budget</th></tr></thead>
        <tbody>{d.ads.map((a) => (
          <tr key={a.test_id}><td>{a.ad}</td><td className="mono">{a.test_id}</td>
            <td><span title={a.campaign}>{a.campaign_id}</span></td><td className="num">{money(a.budget)}</td></tr>
        ))}</tbody>
      </table>,
      <Images key="img" images={(d.creatives ?? []).map((c) => ({ ...c.images[0], ratio: c.test_id }))}
              caption={(i) => `${i.ratio} creative ${i.width} x ${i.height}`} />,
    );
  }
  if (d.changes) {
    parts.push(
      <table className="tbl" key="changes">
        <thead><tr><th>Change</th><th>Target</th><th>Why</th><th className="num">House rules</th></tr></thead>
        <tbody>{d.changes.map((c, i) => (
          <tr key={i}>
            <td className="mono">{c.kind}{c.new_daily_budget != null ? ` to ${money(c.new_daily_budget)}/day` : ""}</td>
            <td><span className="mono">{c.target_id}</span>{c.campaign && <div className="muted" style={{ fontSize: 11.5 }}>{c.campaign}</div>}</td>
            <td>{c.reason}</td>
            <td className="num">{c.allowed ? "allowed" : "blocked"}</td>
          </tr>
        ))}</tbody>
      </table>,
    );
  }
  if (d.tests) {
    parts.push(
      <table className="tbl" key="tests">
        <thead><tr><th>Test</th><th>Hypothesis</th><th>Campaign</th><th className="num">Week budget</th></tr></thead>
        <tbody>{d.tests.map((t) => (
          <tr key={t.test_id}>
            <td><span className="mono">{t.test_id}</span><div className="muted" style={{ fontSize: 11.5 }}>{t.name}</div></td>
            <td>{t.hypothesis}{t.brief?.hook && <div className="muted" style={{ fontSize: 11.5 }}>Hook: "{t.brief.hook}"</div>}</td>
            <td><span className="mono" title={t.campaign}>{t.campaign_id}</span></td>
            <td className="num">{money(t.weekly_budget)}</td>
          </tr>
        ))}</tbody>
      </table>,
    );
  }
  if (d.creative) {
    const c = d.creative.ad_copy;
    const video = d.creative.video;
    const scenes = d.creative.images.filter((i) => i.ratio === "1:1");
    const angle = (i: Image) => c.scenes?.[(i.variant ?? 1) - 1]?.angle;
    parts.push(
      <div key="copy" className="col" style={{ gap: 10 }}>
        <div className="copy"><div className="k">Headline</div>{c.headline}</div>
        <div className="copy"><div className="k">Primary text</div>{c.primary_text}</div>
        <div className="copy"><div className="k">Description</div>{c.description}</div>
        {c.video && c.video.hook && (
          <div className="copy"><div className="k">Video storyboard</div>
            <ol className="story">
              <li><span className="t">0-3 s</span>{c.video.hook}</li>
              <li><span className="t">3-6 s</span>{c.video.benefit}</li>
              <li><span className="t">6-8.5 s</span>{c.video.proof}</li>
              <li><span className="t">8.5-10 s</span>End card: {c.video.cta}</li>
            </ol>
          </div>
        )}
      </div>,
      <Images key="img" images={scenes.length > 1 ? scenes : d.creative.images}
              caption={(i) => scenes.length > 1
                ? `Scene ${i.variant ?? 1}${angle(i) ? ` · ${angle(i)}` : ""}`
                : `${i.ratio} · ${i.width} x ${i.height}`} />,
    );
    if (video?.url) {
      parts.push(
        <figure key="video" className="vid">
          <video src={video.url} controls muted playsInline preload="metadata" />
          <figcaption>10-second 9:16 video, assembled in code from the three scenes. Meta's model API has no video model.</figcaption>
        </figure>,
      );
    }
  }
  const content = d.content as Record<string, any>;
  if (Array.isArray(content.findings)) {
    parts.push(
      <table className="tbl" key="findings">
        <thead><tr><th>Finding</th><th>Subject</th><th className="num">Severity</th></tr></thead>
        <tbody>{content.findings.map((f: any, i: number) => (
          <tr key={i}><td>{f.claim} {(f.evidence_ids ?? []).map((e: string) => <Chip key={e} id={e} evidence={d.evidence} />)}</td>
            <td className="mono">{f.subject_id}</td><td className="num">{f.severity}</td></tr>
        ))}</tbody>
      </table>,
    );
  }
  if (Array.isArray(content.insights)) {
    parts.push(
      <table className="tbl" key="insights">
        <thead><tr><th>Insight</th><th>Angle</th><th>Source</th></tr></thead>
        <tbody>{content.insights.map((s: any, i: number) => (
          <tr key={i}><td>{s.claim}<div className="muted" style={{ fontSize: 11.5 }}>Hook: "{s.hook}"</div></td>
            <td>{s.angle}</td><td>{(s.sources ?? []).map((x: any) => x.title).join("; ")}</td></tr>
        ))}</tbody>
      </table>,
    );
  }
  return <>{parts}</>;
}

function Footer({ d, onDone }: { d: D; onDone: () => void }) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sha = d.content_hash ? `sha ${d.content_hash.slice(0, 4)}..${d.content_hash.slice(-4)}` : "";
  const type = d.title.split(" · ")[0].toLowerCase();

  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await fn(); setNoteOpen(false); setText(""); onDone(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  if (d.status !== "wait") {
    return (
      <div className="row rule-t" style={{ gap: 8, paddingTop: 14 }}>
        <Lbl status={d.status} label={d.status_label} />
        <span className="muted" style={{ fontSize: 12.5 }}>Nothing waits on you here.</span>
        <div className="grow" /><span className="mono muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>{sha}</span>
      </div>
    );
  }
  return (
    <div className="col rule-t" style={{ gap: 10, paddingTop: 14 }}>
      {noteOpen && (
        <div className="note-box">
          <textarea autoFocus value={text} onChange={(e) => setText(e.target.value)}
                    placeholder="What should change? The team redoes this version with your note, and it comes back to you." />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn ink xs" disabled={busy} onClick={() => act(() => api.sendBack(d.key, text))}>Send back</button>
            <button className="btn ghost xs" onClick={() => setNoteOpen(false)}>Cancel</button>
          </div>
        </div>
      )}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn pri" disabled={busy || !d.can_approve}
                title={d.approve_blocked.join("\n") || undefined}
                onClick={() => act(() => api.approve(d.key))}>Approve {type}</button>
        <button className="btn ghost" disabled={busy} onClick={() => setNoteOpen(!noteOpen)}>Send back with a note</button>
        {error && <span className="err" style={{ fontSize: 12 }}>{error}</span>}
        <div className="grow" /><span className="mono muted" style={{ fontSize: 11, whiteSpace: "nowrap" }}>{sha}</span>
      </div>
    </div>
  );
}

function Discussion({ d }: { d: D }) {
  const { root, lines, anchor_who, anchor_ts, anchor_id } = d.discussion;
  const replies = root?.replies ?? [];
  const n = lines.length + (root ? 1 : 0) + replies.length;
  const toRoom = () => go("room", root ? { thread: root.id } : { focus: anchor_id });
  return (
    <aside className="col rule-l" style={{ padding: "20px 18px 16px" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <span className="h2">Discussion</span><span className="mono muted" style={{ fontSize: 11 }}>{n}</span>
      </div>
      {anchor_who && (
        <div className="back-link">
          <Av who={anchor_who} size="sm" />
          <span>Posted in Team Room, {when(anchor_ts, d.discussed.sim_day)}</span>
          <a href="#" onClick={(e) => { e.preventDefault(); toRoom(); }}>Open in room</a>
        </div>
      )}
      <div className="grow scroll bottom" style={{ marginTop: 6 }}>
        <div className="scroll-in">
          {lines.map((l) => (
            <div className="th-rep" key={l.id}><Av who={l.who} size="sm" />
              <div><div className="mh"><b>{l.who.name}</b><span className="t">{clock(l.ts)}</span></div>
                <div className="mb">{l.text}</div></div></div>
          ))}
          {root && (
            <div className="th-rep"><Av who={root.who} size="sm" />
              <div><div className="mh"><b>{root.who.name}</b><span className="t">{clock(root.ts)}</span></div>
                <div className="mb"><Text text={root.text} /></div></div></div>
          )}
          {replies.map((r) => (
            <div className="th-rep" key={r.id}><Av who={r.who} size="sm" />
              <div><div className="mh"><b>{r.who.name}</b><span className="t">{clock(r.ts)}</span></div>
                <div className="mb"><Text text={r.text} /></div></div></div>
          ))}
          {n === 0 && <div className="empty" style={{ padding: "16px 0" }}>No discussion yet.</div>}
        </div>
      </div>
      <Composer small placeholder="Reply here; it posts to the room thread" thread={root?.id ?? null} />
    </aside>
  );
}

function madeWith(d: D, mode?: Mode): string {
  const t = d.made_with;
  if (!t) return "";
  const names = modelLabel(t.models.map((m) => m.model), mode);
  const calls = `${t.calls} ${t.calls === 1 ? "call" : "calls"}`;
  return `made with ${names} · ${calls} · $${t.cost.toFixed(3)}`;
}

export default function Detail({ d, error, sidebar, onChange, mode }: {
  d: D | null; error: string | null; sidebar: ReactNode; onChange: () => void; mode?: Mode;
}) {
  if (!d) {
    return (
      <div className="app list">{sidebar}
        <main className="col" style={{ padding: "18px 30px" }}>
          <div className="empty">{error ?? "Loading"}</div>
        </main>
      </div>
    );
  }
  const tabLabel = TABS.find(([k]) => k === d.tab)?.[1] ?? "All";
  const checks = d.checks;
  return (
    <div className="app detail">
      {sidebar}
      <main className="col scroll top" style={{ padding: "18px 30px 20px", gap: 16, background: "var(--surface)", minWidth: 0 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="crumb">
            <a href="#/deliverables">Deliverables</a><span>/</span>
            <a href={`#/deliverables?tab=${d.tab}`}>{tabLabel}</a><span>/</span><b>{d.title}</b>
          </div>
          <button className="btn ghost xs" onClick={() => go("deliverables")}>Close</button>
        </div>
        <div className="col" style={{ gap: 6 }}>
          <div className="row" style={{ gap: "4px 8px", flexWrap: "wrap" }}>
            <Lbl status={d.status} label={d.status_label} />
            <span className="mono muted" style={{ fontSize: 11 }}>from {d.from.name} · {when(d.updated, d.sim_day)}</span>
            {d.made_with && <span className="mono faint" style={{ fontSize: 11 }}
              title={d.made_with.models.map((m) => `${m.model} on ${mode === "offline" ? "the offline stand-in" : m.provider}: ${m.calls}`).join("\n")}>{madeWith(d, mode)}</span>}
            {d.made_with && <a className="mono" style={{ fontSize: 11, whiteSpace: "nowrap" }} href={`#/harness?topic=${d.key}`}
               title="The stored request: every section, the prompt, tools and output schema">What the model saw</a>}
          </div>
          <div className="h1">{headline(d)}</div>
          <div className="muted" style={{ fontSize: 13 }}>{note(d)}</div>
        </div>
        <Body d={d} />
        {checks.length > 0 && (
          <div className="section">
            <div className="eyebrow">Checked in code</div>
            {checks.map((c, i) => (
              <div className="gate" key={i}>
                <span className="k">{c.passed ? "PASS" : "FAIL"}</span>
                <span>{c.text} {(c.evidence ?? []).map((e) => <Chip key={e} id={e} evidence={d.evidence} />)}</span>
              </div>
            ))}
          </div>
        )}
        <div className="grow" />
        <Footer d={d} onDone={onChange} />
      </main>
      <Discussion d={d} />
    </div>
  );
}
