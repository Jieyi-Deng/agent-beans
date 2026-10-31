// Types and calls for the PGT web API (src/pgt/api). The server computes every status;
// the client only shows what it is given.
import { useEffect, useState } from "react";

export type Who = { actor: string; code: string | null; name: string; persona: string };
export type Status = "draft" | "review" | "wait" | "ok" | "applied" | "stale";

export type Deliverable = {
  key: string;
  artifact_id: string;
  kind: string;
  tab: string;
  title: string;
  sub: string;
  from: Who;
  status: Status;
  status_label: string;
  updated: string;
  sim_day: number | null;
  discussed: { event_id: number; sim_day: number | null; ts: string };
};

export type Card = Deliverable & { note: string; approved_ts?: string };
export type Reply = { id: number; ts: string; who: Who; text: string };
export type Line = { id: number; ts: string; sim_day: number | null; type: string; who: Who; text: string };

export type Msg = {
  kind: "msg"; id: number; ts: string; sim_day: number | null; who: Who; text: string;
  mention: boolean; card: Card | null; replies: Reply[]; models?: string[]; cost?: number | null;
};
export type Fold = {
  kind: "fold"; id: number; ids: number[]; actors: Who[]; names: string[]; tail: string;
  ts_from: string; ts_to: string; sim_day: number | null; items: Line[];
  models?: string[]; cost?: number | null;
};
export type DayDiv = { kind: "day"; id: number; label: string; sim_day: number | null };
export type RoomItem = Msg | Fold | DayDiv;

export type Check = { passed: boolean; text: string; rule: string; evidence?: string[] };
export type Evidence = { id: string; note: string; values: Record<string, number> };
export type Image = { ratio: string; width: number; height: number; url: string | null; variant?: number };
export type Video = { width: number; height: number; seconds: number; url: string | null };
export type Scene = { angle: string; prompt: string };
export type Storyboard = { hook: string; benefit: string; proof: string; cta: string };
export type AdCopy = { name: string; headline: string; primary_text: string; description: string;
                       scenes?: Scene[]; video?: Storyboard };
export type Creative = { key: string; title: string; test_id: string; ad_copy: AdCopy; images: Image[];
                         video?: Video | null };

export type Detail = Deliverable & {
  headline: string;
  content: Record<string, unknown>;
  content_hash: string;
  checks: Check[];
  evidence: Evidence[];
  can_approve: boolean;
  approve_blocked: string[];
  made_with: Tally | null;
  discussion: { anchor_id: number | null; anchor_ts: string | null; anchor_who: Who | null;
                root: Msg | null; lines: Line[] };
  ads?: { ad: string; name: string; test_id: string; campaign: string; campaign_id: string;
          budget: number | null }[];
  creatives?: Creative[];
  changes?: { kind: string; target_id: string; reason: string; campaign: string; allowed: boolean;
              new_daily_budget?: number }[];
  creative?: Creative;
  review?: { verdict: string; issues: string[] } | null;
  tests?: { test_id: string; name: string; hypothesis: string; campaign: string;
            campaign_id: string; weekly_budget: number; stop_rule: string;
            brief?: { angle: string; hook: string } }[];
};

/** Dashboard: Insights-shaped rows from the sandbox, plus what the team did. */
export type Link = { key: string; title: string };
export type Kpi = { key: string; label: string; value: number | null; fmt: "money" | "int" | "pct" | "ratio";
                    sub: string; tone: "" | "good" | "bad"; formula: string; hero?: boolean };
export type Metrics = { spend: number; impressions: number; clicks: number; results: number;
                        ctr: number | null; cpr: number | null; tone: "" | "under" | "over";
                        revenue: number | null; roas: number | null };
export type AdRow = Metrics & { id: string; name: string; status: string; status_label: string;
                                waits: boolean; evidence: string | null };
export type CampaignRow = Metrics & { id: string; name: string; status: string; status_label: string;
  in_learning: boolean; budget: number; test_id: string | null; rows: number; ads: AdRow[] };
export type Action = { n: number; day: number; who: Who; verb: string; text: string;
                       evidence: string[]; link: Link | null };
type Which = { account: string; account_name: string; real: boolean };
export type DashboardData = (Which & { ready: false; waiting?: boolean }) | (Which & {
  ready: true; currency: string; days: number; start: number; end: number;
  short: boolean; pulled: { day: number | null; ts: string } | null; target: number;
  result_name: string; kpis: Kpi[];
  evidence: { rows: number; first: string | null; last: string | null; fields: string;
              level: string; used_in: Link | null };
  series: { day: number; spend: number; results: number; cpr: number | null }[];
  actions: Action[]; after: string | null; campaigns: CampaignRow[];
  daily_total: number; daily_cap: number;
});

/** Ads Manager drafts on a real account, read only. */
export type DraftNode = { id: string; name: string; level: "campaign" | "ad set" | "ad"; status: string | null;
                          details: Record<string, string | number | boolean>; nested: string[]; children: DraftNode[] };
export type Drafts = { available: boolean; reason: string | null; drafts: DraftNode[];
                       counts?: Record<string, number> };

/** Creative Assets: every image the team made, with how it was made. */
export type StepModel = { model: string; provider: string; calls: number | null; cost: number | null };
export type Step = { title: string; sub: string; model: StepModel | null; final: boolean; by?: string };
export type Asset = {
  id: string; kind: "made" | "source" | "upload"; title: string; hook: string;
  test_id: string | null; week: string | null; status: string; status_label: string;
  version: number; updated: string | null; models: string[]; after: string; bad: boolean;
  image: string | null; images: (Image & { path: string })[]; source: string | null;
  video?: string | null; scenes?: string[];
  steps: Step[]; facts: [string, string][]; used_in: Link | null; deliverable: string | null;
};

export type Mode = "offline" | "live" | "mixed";
export type AppStatus = { busy: string | null; error: string | null; where: string;
                          day: number | null; last_event_id: number; owner: string; mode: Mode };

/** Model calls for one role or one deliverable, at list price. */
export type Tally = {
  models: { model: string; provider: string; calls: number }[];
  calls: number; failed: number; tokens: number; searches: number; cost: number; seconds: number;
};
export type SettingsData = {
  facts: Record<string, unknown>; fact_version: number; model_calls: number; tokens: number;
  cost: number; offline: boolean; mode: Mode; usage: (Tally & { who: Who })[];
  cap: number | null; remaining: number | null; day: number | null; now: string;
};

/** Which ad account the team works on, how much it may do, and the house rules. */
export type Account = { kind: "sandbox" | "meta"; account_id: string; name: string; currency: string;
                        timezone: string; blurb?: string };
export type Rules = { preset: string; daily_budget_cap: number; max_budget_change_pct: number;
  min_days_between_budget_changes: number; min_spend_multiple_before_pause: number;
  protect_learning_phase: boolean; check_time: string };
export type LevelInfo = { level: number; code: string; name: string; text: string; scopes: string[] };
export type AccessData = {
  connection: (Account & { ts: string; by: string }) | null; level: number; levels: LevelInfo[];
  sandbox: Account; public: boolean; meta_login: string | null; meta_accounts: Account[];
  rules: Rules; presets: Record<string, Partial<Rules>>; weekly_budget: number;
  weekly_test_budget: number; constraints: string[]; fact_version: number;
  reads: Reads | null;
};
/** Which source answered each read on a real account: the Meta Ads MCP or the Graph API. */
export type Reads = { mcp: boolean; reads: Record<string, "mcp" | "graph">; notes: Record<string, string> };
export type FactsPatch = { weekly_budget?: number; test_budget_share?: number; target_cost_per_result?: number };
export type RulesPatch = Partial<Omit<Rules, "check_time">>;

/** "muse-spark-1.3", or "offline stand-in" when no real model answered. */
export function modelLabel(models: string[] | undefined, mode: Mode | undefined): string {
  if (!models?.length) return "";
  const names = models.map((m) => m.replace(/^meta-models\//, "")).join(" + ");
  return mode === "offline" ? `${names}, offline stand-in` : names;
}

/** Model spend for one message or fold, e.g. "$0.004". */
export function costLabel(cost: number | null | undefined): string {
  if (cost == null) return "";
  return cost > 0 && cost < 0.001 ? "<$0.001" : `$${cost.toFixed(3)}`;
}

/** The GitHub Pages build (vite build --mode replay) reads a recorded run from data/ and changes nothing.
 *  `pgt replay` writes those files, named the same way as here. */
export const REPLAY = import.meta.env.MODE === "replay";
export const READ_ONLY = "This is a recorded replay, so nothing changes here. Run PGT locally to approve, send back or talk to the team.";

function replayFile(path: string): string {
  return "data/" + path.replace(/^\/api\//, "").replace("?days=", "-") + ".json";
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  if (REPLAY) {
    if (init?.method && init.method !== "GET") throw new Error(READ_ONLY);
    path = replayFile(path);
  }
  const res = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* plain text error */ }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => call<AppStatus>("/api/status"),
  room: () => call<{ where: string; items: RoomItem[] }>("/api/room"),
  deliverables: () => call<{ items: Deliverable[]; account_name: string }>("/api/deliverables"),
  detail: (key: string) => call<Detail>(`/api/deliverables/${key}`),
  approve: (key: string) => call(`/api/approve/${key}`, { method: "POST" }),
  sendBack: (key: string, note: string) =>
    call(`/api/send-back/${key}`, { method: "POST", body: JSON.stringify({ note }) }),
  post: (text: string, to?: string | null, thread?: number | null) =>
    call<{ id: number }>("/api/messages", {
      method: "POST", body: JSON.stringify({ text, to: to ?? null, thread: thread ?? null }),
    }),
  runDay: () => call("/api/run/day", { method: "POST" }),
  runWeek: () => call("/api/run/week", { method: "POST" }),
  settings: () => call<SettingsData>("/api/settings"),
  access: () => call<AccessData>("/api/access"),
  connect: (body: { kind: "sandbox" | "meta"; account_id?: string; level: number; rules?: RulesPatch }) =>
    call("/api/access/connect", { method: "POST", body: JSON.stringify(body) }),
  setLevel: (level: number) => call("/api/access/level", { method: "POST", body: JSON.stringify({ level }) }),
  disconnect: () => call("/api/access/disconnect", { method: "POST" }),
  houseRules: (patch: RulesPatch) =>
    call<{ fact_version: number }>("/api/house-rules", { method: "POST", body: JSON.stringify(patch) }),
  setFacts: (patch: FactsPatch) =>
    call<{ fact_version: number; refresh: boolean }>("/api/facts", { method: "POST", body: JSON.stringify(patch) }),
  dashboard: (days: number) => call<DashboardData>(`/api/dashboard?days=${days}`),
  drafts: () => call<Drafts>("/api/drafts"),
  assets: () => call<{ items: Asset[] } & Which>("/api/assets"),
  upload: async (file: File) => {
    if (REPLAY) throw new Error(READ_ONLY);
    const res = await fetch(`/api/assets/upload?name=${encodeURIComponent(file.name)}`,
                            { method: "POST", body: file });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail ?? detail; } catch { /* plain text error */ }
      throw new Error(detail);
    }
    return res.json() as Promise<{ ok: boolean; id: string }>;
  },
};

/** A counter that goes up whenever the event log or the team's busy state changes. */
export function useLive(): { tick: number; busy: string | null } {
  const [state, setState] = useState({ tick: 0, busy: null as string | null });
  useEffect(() => {
    if (REPLAY) return; // a recorded run never changes
    const es = new EventSource("/api/stream");
    es.onmessage = (m) => {
      const data = JSON.parse(m.data);
      setState((s) => ({ tick: s.tick + 1, busy: data.busy }));
    };
    return () => es.close();
  }, []);
  return state;
}

/** Load `fn` now and again on every tick. Keeps the last good value while reloading. */
export function useData<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    fn().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

// Times. The demo week runs in seconds, so the simulated day carries the date and the clock
// shows when the step happened.
export function clock(ts: string | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** A date as Mon D YYYY, e.g. "Oct 31 2026"; today when there is no timestamp. */
export function date(ts?: string | null): string {
  const d = ts ? new Date(ts) : new Date();
  if (Number.isNaN(d.getTime())) return "";
  return `${d.toLocaleString("en-US", { month: "short" })} ${d.getDate()} ${d.getFullYear()}`;
}

export function when(ts: string | null | undefined, day: number | null | undefined): string {
  const t = clock(ts);
  return day ? `Day ${day} · ${t}` : t;
}

export function money(n: number | null | undefined): string {
  if (n == null) return "";
  return "$" + (Number.isInteger(n) ? n.toString() : n.toFixed(2));
}
