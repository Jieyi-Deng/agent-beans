import { useState, type ReactNode } from "react";
import { Wizard } from "./Access";
import { api, date, REPLAY, setDayOne, useData, useLive, type Mode } from "./api";
import Assets from "./Assets";
import { TEAM } from "./avatars";
import Dashboard from "./Dashboard";
import Deliverables from "./Deliverables";
import Detail from "./Detail";
import Harness from "./Harness";
import Memory from "./Memory";
import Room from "./Room";
import { go, useRoute, type Route } from "./route";
import Settings from "./Settings";
import { owner, OwnerAv, setOwner } from "./ui";

const MODE_LABEL = { offline: "Offline stand-in", live: "Live · Meta API", mixed: "Live · Muse Spark Open" };

function Sidebar({ route, waiting, mode, account }: { route: Route; waiting: number; mode?: Mode; account?: string }) {
  const page = route.page === "detail" ? "deliverables" : route.page;
  const item = (key: string, label: string, extra?: ReactNode) => (
    <a className={page === key ? "nav-i on" : "nav-i"} href={`#/${key}`} style={{ textDecoration: "none" }}>
      <span>{label}</span>{extra}
    </a>
  );
  return (
    <aside className="col rule-r" style={{ background: "var(--sunken)", padding: "18px 12px", gap: 22 }}>
      <div className="mark" style={{ padding: "0 6px" }}>
        <img className="mark-logo" src="pallas-logo.png" alt="Agent Beans" />
        <div style={{ minWidth: 0 }}><div className="mark-name">Agent Beans</div><div className="mark-sub" title={account}>{account ?? "No account connected"}</div></div>
      </div>
      {mode && <a className={"mode " + mode} href="#/settings"
                  title={REPLAY ? "A recorded live run, read-only. Models and spend are in Settings." : "Which models answer. Details in Settings."}>
        <span className="dot" />{REPLAY ? "Replay of a live run" : MODE_LABEL[mode]}</a>}
      <nav className="nav">
        {item("room", "Team Room")}
        {item("deliverables", "Deliverables", waiting > 0 &&
          <span className="nav-dot" title={`${waiting} ${waiting === 1 ? "item" : "items"} waiting on ${owner()}`} />)}
        {item("dashboard", "Dashboard")}
        {item("assets", "Creative Assets")}
        {item("memory", "Memory")}
        {item("harness", "Harness")}
        {item("settings", "Settings")}
      </nav>
      <div className="grow" />
      <div className="col" style={{ gap: 8, padding: "0 6px" }}>
        <div className="eyebrow">The team</div>
        <div className="row team-avs">
          {TEAM.map((t) => <img key={t.code} className="av sm" src={`avatars/${t.code}.svg`} alt={t.name} title={t.name} />)}
        </div>
      </div>
      <div className="row" style={{ gap: 10, padding: "0 6px" }}>
        <OwnerAv />
        <div style={{ minWidth: 0 }}><div className="ink" style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={owner()}>{owner()}</div>
          <div className="mono muted" style={{ fontSize: 11 }}>Owner · {date()}</div></div>
      </div>
    </aside>
  );
}

export default function App() {
  const route = useRoute();
  const { tick, busy } = useLive();
  const status = useData(api.status, [tick]);
  setDayOne(status.data?.day_one);
  setOwner(status.data?.owner, status.data?.owner_avatar);
  const list = useData(api.deliverables, [tick]);
  const room = useData(api.room, [tick]);
  const detail = useData(() => (route.key ? api.detail(route.key) : Promise.resolve(null)), [tick, route.key]);
  const items = list.data?.items ?? [];
  const waiting = items.filter((d) => d.status === "wait").length;
  const mode = status.data?.mode;
  const [bump, setBump] = useState(0);
  const access = useData(api.access, [tick, bump]);
  const [later, setLater] = useState(false);
  const refresh = () => setBump((b) => b + 1);
  const sidebar = <Sidebar route={route} waiting={waiting} mode={mode} 
                          account={access.data ? access.data.connection?.name ?? "No account connected" : ""} />;
  // First run, "Switch account", or back from facebook.com with accounts to pick.
  const a = access.data;
  const asked = route.page === "settings" && (route.q.get("setup") || route.q.get("meta") === "pick");
  const named = status.data?.owner_set ?? false;
  const wizard = !REPLAY && a && status.data && (asked || (!a.connection && !later)) ? (
    <Wizard key={asked ? "asked" : "first"} a={a} named={named} start={route.q.get("meta") === "pick" && a.meta_accounts.length ? 1 : 0}
            onClose={() => { setLater(true); if (asked) go("settings", { s: "connection" }); }}
            onDone={() => { setLater(true); refresh(); if (asked) go("settings", { s: "connection" }); }} />
  ) : null;

  let page: ReactNode;
  if (route.page === "deliverables") page = <Deliverables items={items} accountName={list.data?.account_name} route={route} sidebar={sidebar} />;
  else if (route.page === "detail") {
    const d = detail.data && detail.data.key === route.key ? detail.data : null;
    page = <Detail d={d} error={detail.error} sidebar={sidebar} onChange={() => undefined} mode={mode} />;
  } else if (route.page === "dashboard") page = <Dashboard tick={tick} route={route} sidebar={sidebar} items={items} />;
  else if (route.page === "assets") page = <Assets tick={tick} route={route} sidebar={sidebar} mode={mode} />;
  else if (route.page === "memory") page = <Memory tick={tick} busy={busy} route={route} sidebar={sidebar} />;
  else if (route.page === "harness") page = <Harness tick={tick} route={route} sidebar={sidebar} mode={mode} />;
  else if (route.page === "settings") page = <Settings tick={tick} busy={busy} sidebar={sidebar} route={route}
                                                       access={a ?? null} onAccess={refresh} named={named} />;
  else page = <Room items={room.data?.items ?? []} where={status.data?.where ?? ""} busy={busy}
                    error={status.data?.error ?? null} route={route} sidebar={sidebar} mode={mode} />;
  return <div className="frame">{page}{wizard}</div>;
}
