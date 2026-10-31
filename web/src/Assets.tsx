// 5b Creative Assets: every image the team made, how each was made (which model, how many
// calls, what the Reviewer sent back), and how it did once launched. Also the product photos the
// team starts from, and anything the owner uploads.
import { Fragment, useRef, useState, type ReactNode } from "react";
import { api, clock, REPLAY, useData, type Asset, type Mode } from "./api";
import { go, type Route } from "./route";
import { OWNER } from "./ui";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "made", label: "Made by the team" },
  { key: "source", label: "Product photos and uploads" },
] as const;

function usd(n: number | null) {
  if (n == null) return "";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

function Card({ a, on }: { a: Asset; on: boolean }) {
  return (
    <button className={"ast" + (on ? " on" : "")} onClick={() => go("assets", { asset: on ? null : a.id })}>
      {a.image ? <img className="img" src={a.image} alt={a.title} loading="lazy" />
        : <div className="img ph-img">No image</div>}
      <div className="b">
        <div className="r"><span className="t">{a.title}</span><span className={"lbl " + a.status}>{a.status_label}</span></div>
        {a.hook && <div className="h">{a.hook}</div>}
        <div className="m">{[a.test_id ?? a.week, a.models.join(" + ")].filter(Boolean).join(" · ")}</div>
        <div className={a.bad ? "met bad" : "met"}>{a.after}</div>
      </div>
    </button>
  );
}

function Drop({ onDone }: { onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [state, setState] = useState<string | null>(null);
  const send = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    setState("Uploading");
    try {
      await api.upload(file);
      setState(null);
      onDone();
    } catch (e) {
      setState((e as Error).message);
    }
  };
  return (
    <div className={"drop" + (over ? " over" : "")}
         onDragOver={(e) => { e.preventDefault(); setOver(true); }}
         onDragLeave={() => setOver(false)}
         onDrop={(e) => { e.preventDefault(); setOver(false); send(e.dataTransfer.files); }}>
      <b>Add a product photo</b>
      <span>PNG or JPEG, up to 5 MB. The team asks before using it in an ad.</span>
      <button className="btn ghost xs" style={{ marginTop: 4 }} onClick={() => input.current?.click()}>Upload</button>
      <input ref={input} type="file" accept="image/png,image/jpeg" hidden onChange={(e) => send(e.target.files)} />
      {state && <span className={state === "Uploading" ? "mono muted" : "err"} style={{ fontSize: 11 }}>{state}</span>}
    </div>
  );
}

/** The scenes of one ad and its video. A row of tiles picks what the large frame shows; the
 * size links under it open the chosen scene in each placement size. */
function Media({ a }: { a: Asset }) {
  const scenes = [...new Set(a.images.map((i) => i.variant ?? 1))].sort();
  const [pick, setPick] = useState<number | "video">(scenes[0] ?? 1);
  if (!a.images.length) return a.image ? <img className="side-img" src={a.image} alt={a.title} /> : null;
  const sizes = a.images.filter((i) => (i.variant ?? 1) === pick);
  const square = (n: number) => a.images.find((i) => (i.variant ?? 1) === n && i.ratio === "1:1")?.url;
  const tall = a.images.find((i) => (i.variant ?? 1) === scenes[0] && i.ratio === "9:16")?.url;
  const label = (n: number) => a.scenes?.[n - 1] || `Scene ${n}`;
  return (
    <>
      {pick === "video" && a.video
        ? <video className="side-img side-video" src={a.video} poster={tall ?? undefined} controls muted playsInline preload="metadata" />
        : <img className="side-img" src={square(pick as number) ?? a.image ?? ""} alt={`${a.title}, ${label(pick as number)}`} />}
      {(scenes.length > 1 || a.video) && (
        <div className="tiles">
          {scenes.map((n) => (
            <button key={n} className={"tile" + (pick === n ? " on" : "")} onClick={() => setPick(n)} title={label(n)}>
              <img src={square(n) ?? ""} alt="" />
              <span>{label(n)}</span>
            </button>
          ))}
          {a.video && (
            <button className={"tile" + (pick === "video" ? " on" : "")} onClick={() => setPick("video")} title="10-second video">
              <img src={tall ?? ""} alt="" />
              <span>Video</span>
            </button>
          )}
        </div>
      )}
      {pick !== "video" && sizes.length > 0 && (
        <div className="sizes">
          {sizes.map((i) => i.url && <a key={i.ratio} href={i.url} target="_blank" rel="noreferrer" title={`${i.width} x ${i.height}`}>{i.ratio}</a>)}
        </div>
      )}
    </>
  );
}

function Side({ a, mode }: { a: Asset; mode?: Mode }) {
  const big = a.images.find((i) => i.ratio === "1:1")?.url ?? a.image;
  const offline = mode === "offline";
  return (
    <aside className="col rule-l scroll top" style={{ padding: "18px 18px 16px", gap: 14, background: "var(--paper)" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="h2">{a.title}</span>
        <button className="btn ghost xs" onClick={() => go("assets")}>Close</button>
      </div>
      <Media key={a.id} a={a} />
      <div className="row" style={{ gap: 6 }}>
        <span className={"lbl " + a.status}>{a.status_label}</span>
        <span className="mono muted" style={{ fontSize: 11 }}>
          {a.kind === "made" ? `v${a.version}` : a.week ?? ""}{a.updated ? ` · ${clock(a.updated)}` : ""}</span>
      </div>
      {a.steps.length > 0 && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 8 }}>How it was made</div>
          <div className="pipe">
            {a.steps.map((s, i) => (
              <div className="pipe-i" key={i}>
                <span className={"o" + (s.final ? " f" : s.by ? " x" : "")} />
                <div>
                  <div className="a">{s.title}</div>
                  {s.model && (
                    <div className="mdl">
                      {s.model.model} · {s.model.provider}
                      {s.model.calls != null && ` · ${s.model.calls} ${s.model.calls === 1 ? "call" : "calls"}`}
                      {s.model.cost != null && ` · ${usd(s.model.cost)}`}
                      {offline && " · offline stand-in"}
                    </div>
                  )}
                  {s.sub && <div className="s">{s.by ? `${s.by}: ` : ""}{s.sub}</div>}
                </div>
              </div>
            ))}
          </div>
          {a.source && <div className="mono muted" style={{ fontSize: 10.5 }}>
            Starts from <a href={a.source} target="_blank" rel="noreferrer">the week's product photo</a></div>}
        </div>
      )}
      {a.kind === "upload" && <div className="muted" style={{ fontSize: 12 }}>
        Uploaded by {OWNER}. Checked as an image and saved as PNG. The team asks before putting it in an ad.</div>}
      <dl className="kv rule-t" style={{ paddingTop: 12, margin: 0 }}>
        {a.facts.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}
        {a.used_in && <><dt>Used in</dt><dd><a href={`#/deliverables/${a.used_in.key}`}>{a.used_in.title}</a></dd></>}
      </dl>
      <div className="grow" />
      <div className="row" style={{ gap: 8 }}>
        {a.deliverable && <button className="btn ghost xs" onClick={() => go("deliverables/" + a.deliverable)}>Open in Deliverables</button>}
        {big && <a className="btn ghost xs" href={big} download>Download</a>}
      </div>
    </aside>
  );
}

export default function Assets({ tick, route, sidebar, mode }: {
  tick: number; route: Route; sidebar: ReactNode; mode?: Mode;
}) {
  const [bump, setBump] = useState(0);
  const { data, error } = useData(api.assets, [tick, bump]);
  const filter = route.q.get("show") ?? "all";
  const openId = route.q.get("asset");
  const items = data?.items ?? [];
  const shown = items.filter((a) => filter === "all" || (filter === "made" ? a.kind === "made" : a.kind !== "made"));
  const open = items.find((a) => a.id === openId) ?? null;
  const made = items.filter((a) => a.kind === "made").length;
  return (
    <div className={"app " + (open ? "side" : "list")}>
      {sidebar}
      <main className="col scroll top" style={{ padding: "18px 22px 16px 26px", gap: 12, minWidth: 0 }}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div>
            <div className="eyebrow">{data?.account_name ?? "Sandbox"} · {made} {made === 1 ? "creative" : "creatives"} made</div>
            <div className="h1" style={{ marginTop: 4 }}>Creative Assets</div>
          </div>
          <div className="seg" role="tablist">
            {FILTERS.map((f) => (
              <button key={f.key} className={filter === f.key ? "on" : ""}
                      onClick={() => go("assets", { show: f.key === "all" ? null : f.key, asset: openId })}>{f.label}</button>
            ))}
          </div>
        </div>
        <div className="row" style={{ gap: 8, fontSize: 12, color: "var(--muted)", flexWrap: "wrap" }}>
          <span className="mono" style={{ fontSize: 11 }}>Newest week first</span>
          <div className="grow" />
          <span>Numbers after launch come from the same Insights rows as the Dashboard</span>
        </div>
        {error && <div className="err" style={{ fontSize: 12 }}>{error}</div>}
        {data && made === 0 && filter !== "source" && (
          <div className="muted" style={{ fontSize: 12.5 }}>
            Nothing made yet. The Creative Producer starts once you approve the week's test plan in{" "}
            <a href="#/deliverables">Deliverables</a>.
          </div>
        )}
        <div className="assets">
          {shown.map((a) => <Card key={a.id} a={a} on={a.id === openId} />)}
          {filter !== "made" && !REPLAY && <Drop onDone={() => setBump((b) => b + 1)} />}
        </div>
      </main>
      {open && <Side a={open} mode={mode} />}
    </div>
  );
}
