// 4b Deliverables: every output in one list, one status each. What waits on the owner is
// pinned on top; headers sort and the chips filter, both inside each group.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { when, type Deliverable, type Status } from "./api";
import { go, type Route } from "./route";
import { Av, Chevron, Lbl } from "./ui";

export const TABS: [string, string][] = [
  ["all", "All"], ["plans", "Test plans"], ["changes", "Change sets"],
  ["creative", "Creative"], ["findings", "Findings"],
];
const STATUSES: [Status, string][] = [
  ["draft", "Draft"], ["review", "In review"], ["wait", "Waiting on you"],
  ["ok", "Approved"], ["applied", "Applied"], ["stale", "Stale"],
];
type SortKey = "title" | "from" | "status" | "updated";

const Arrows = ({ dir }: { dir: "asc" | "desc" | null }) => (
  <svg width="7" height="9" viewBox="0 0 7 9" aria-hidden="true">
    {dir === "desc" && <path d="M3.5 1v7M1 5.5L3.5 8L6 5.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />}
    {dir === "asc" && <path d="M3.5 8V1M1 3.5L3.5 1L6 3.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />}
    {dir === null && <path d="M1 3.5L3.5 1L6 3.5M1 5.5L3.5 8L6 5.5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />}
  </svg>
);

function Menu({ label, value, options, onPick }: {
  label: string; value: string; options: [string, ReactNode][]; onPick: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  const current = options.find(([v]) => v === value)?.[1] ?? "All";
  return (
    <span className="fchip" ref={ref} onClick={() => setOpen(!open)} role="button" aria-expanded={open}>
      <span className="k">{label}</span>{current}<Chevron />
      {open && (
        <div className="menu" onClick={(e) => e.stopPropagation()}>
          {options.map(([v, l]) => (
            <button key={v} className={v === value ? "on" : ""} onClick={() => { onPick(v); setOpen(false); }}>{l}</button>
          ))}
        </div>
      )}
    </span>
  );
}

function Row({ d }: { d: Deliverable }) {
  return (
    <div className={d.status === "wait" ? "dl-r need" : "dl-r"} onClick={() => go("deliverables/" + d.key)}
         role="link" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") go("deliverables/" + d.key); }}>
      <div><div className="t1">{d.title}</div><div className="t2">{d.sub}</div></div>
      <div className="who"><Av who={d.from} size="sm" /><span title={d.from.name}>{d.from.name}</span></div>
      <div><Lbl status={d.status} label={d.status_label} /></div>
      <div className="when">{when(d.updated, d.sim_day)}</div>
      <div><a href={`#/room?focus=${d.discussed.event_id}`} onClick={(e) => e.stopPropagation()}>
        Team Room · {d.discussed.sim_day ? `Day ${d.discussed.sim_day}` : "now"}</a></div>
    </div>
  );
}

export default function Deliverables({ items, accountName, route, sidebar }: {
  items: Deliverable[]; accountName?: string; route: Route; sidebar: ReactNode;
}) {
  const tab = route.q.get("tab") ?? "all";
  const from = route.q.get("from") ?? "all";
  const status = route.q.get("status") ?? "all";
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "updated", dir: "desc" });
  const params = { tab: tab === "all" ? null : tab, from: from === "all" ? null : from,
                   status: status === "all" ? null : status };

  const roles = [...new Map(items.map((d) => [d.from.actor, d.from])).values()];
  const order = STATUSES.map(([s]) => s);
  const shown = items
    .filter((d) => (tab === "all" || d.tab === tab) && (from === "all" || d.from.actor === from)
                   && (status === "all" || d.status === status))
    .sort((a, b) => {
      const v = sort.key === "title" ? a.title.localeCompare(b.title)
        : sort.key === "from" ? a.from.name.localeCompare(b.from.name)
        : sort.key === "status" ? order.indexOf(a.status) - order.indexOf(b.status)
        : a.updated.localeCompare(b.updated);
      return sort.dir === "asc" ? v : -v;
    });
  const need = shown.filter((d) => d.status === "wait");
  const rest = shown.filter((d) => d.status !== "wait");
  const sortLabel = { title: "Item", from: "From", status: "Status", updated: "Updated" }[sort.key];
  const dirLabel = sort.key === "updated" ? (sort.dir === "desc" ? "newest first" : "oldest first")
    : sort.dir === "asc" ? "A to Z" : "Z to A";

  const head = (key: SortKey, label: string) => (
    <button className={sort.key === key ? "sh on" : "sh"} onClick={() => setSort(
      sort.key === key ? { key, dir: sort.dir === "asc" ? "desc" : "asc" }
                       : { key, dir: key === "updated" ? "desc" : "asc" })}>
      {label}<Arrows dir={sort.key === key ? sort.dir : null} />
    </button>
  );

  return (
    <div className="app list">
      {sidebar}
      <main className="col" style={{ padding: "18px 28px 18px", minWidth: 0 }}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
          <div>
            <div className="eyebrow">{accountName ?? "Sandbox"} · {items.length} {items.length === 1 ? "item" : "items"}</div>
            <div className="h1" style={{ marginTop: 4 }}>Deliverables</div>
          </div>
          <div className="seg" role="tablist">
            {TABS.map(([k, l]) => (
              <button key={k} className={tab === k ? "on" : ""} onClick={() => go("deliverables", { ...params, tab: k === "all" ? null : k })}>{l}</button>
            ))}
          </div>
        </div>
        <div className="fbar row">
          <Menu label="From" value={from} onPick={(v) => go("deliverables", { ...params, from: v === "all" ? null : v })}
                options={[["all", "All roles"], ...roles.map((r): [string, ReactNode] => [r.actor, r.name])]} />
          <Menu label="Status" value={status} onPick={(v) => go("deliverables", { ...params, status: v === "all" ? null : v })}
                options={[["all", "All"], ...STATUSES]} />
          <div className="grow" />
          <span className="mono muted" style={{ fontSize: 11 }}>Sorted by {sortLabel}, {dirLabel} · inside each group</span>
        </div>
        <div className="grow scroll top">
          <div className="dl">
            <div className="dl-h">
              {head("title", "Item")}{head("from", "From")}{head("status", "Status")}{head("updated", "Updated")}
              <span>Discussed in</span>
            </div>
            {need.length > 0 && <div className="grp">Waiting on you</div>}
            {need.map((d) => <Row key={d.key} d={d} />)}
            {rest.length > 0 && <div className="grp">{need.length ? "Everything else" : "All items"}</div>}
            {rest.map((d) => <Row key={d.key} d={d} />)}
            {shown.length === 0 && <div className="empty">Nothing matches these filters.</div>}
          </div>
          <div className="row" style={{ gap: 14, padding: "18px 0 0 12px", flexWrap: "wrap" }}>
            <span className="eyebrow">Status key</span>
            {STATUSES.map(([s, l]) => <Lbl key={s} status={s} label={l} />)}
          </div>
        </div>
      </main>
    </div>
  );
}
