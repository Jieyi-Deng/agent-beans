// Hash routes: #/room, #/deliverables, #/deliverables/<key>, #/dashboard, #/assets, #/memory, #/harness,
// #/settings, with ?query.
import { useEffect, useState } from "react";

export type Route = { page: "room" | "deliverables" | "detail" | "dashboard" | "assets" | "memory" | "harness"
                              | "settings"; key?: string;
                      q: URLSearchParams };

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const q = new URLSearchParams(query);
  if (path.startsWith("deliverables/")) return { page: "detail", key: path.slice(13), q };
  if (path === "deliverables") return { page: "deliverables", q };
  if (path === "dashboard") return { page: "dashboard", q };
  if (path === "assets") return { page: "assets", q };
  if (path === "memory") return { page: "memory", q };
  if (path === "harness") return { page: "harness", q };
  if (path === "settings") return { page: "settings", q };
  return { page: "room", q };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function go(path: string, params?: Record<string, string | number | null | undefined>) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v != null && v !== "") q.set(k, String(v));
  const s = q.toString();
  window.location.hash = "/" + path + (s ? "?" + s : "");
}
