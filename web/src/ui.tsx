// Small pieces shared by the pages.
import { Fragment, type ReactNode } from "react";
import { avatar } from "./avatars";
import type { Card, Status, Who } from "./api";
import { go } from "./route";

let ownerName = "Owner";
let ownerPicture: string | null = null;

/** Sets the owner's name and picture from the status, like setDayOne. */
export function setOwner(name: string | undefined, picture: string | null | undefined) {
  if (name) ownerName = name;
  ownerPicture = picture ?? null;
}

/** The name the owner gave, "Owner" until they give one. */
export function owner(): string {
  return ownerName;
}

/** The owner's saved picture, or null when the app draws their initials. */
export function ownerAvatar(): string | null {
  return ownerPicture;
}

/** "Rosa Martinez" -> "RM", "Jieyi" -> "J". */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  const first = Array.from(words[0])[0];
  const last = words.length > 1 ? Array.from(words[words.length - 1])[0] : "";
  return (first + last).toUpperCase();
}

type Person = Who | { code: string | null; name: string };

export function Av({ who, size }: { who: Person; size?: "sm" | "lg" }) {
  const cls = size ? "av " + size : "av";
  if (who.code === "YOU") return <OwnerAv size={size} />;
  return <img className={cls} src={avatar(who.code)} alt={who.name} />;
}

/** The owner's picture, or their initials when they have none. `picture` and `name` override the
 *  saved ones, for a preview before saving. */
export function OwnerAv({ size, name, picture }: { size?: "sm" | "lg"; name?: string; picture?: string | null }) {
  const cls = size ? "av " + size : "av";
  const who = name ?? ownerName;
  const src = picture === undefined ? ownerPicture : picture;
  if (src) return <img className={cls} src={src} alt={who} />;
  return <span className={cls + " ini"} role="img" aria-label={who}>{initials(who || "?")}</span>;
}

export function Stack({ people }: { people: { code: string | null; name: string }[] }) {
  return (
    <span className="av-stack">
      {people.slice(0, 3).map((p, i) => <Av key={i} who={p} size="sm" />)}
    </span>
  );
}

const STATUS_CLS: Record<Status, string> = {
  draft: "lbl draft", review: "lbl review", wait: "lbl wait", ok: "lbl ok",
  applied: "lbl applied", stale: "lbl stale",
};

export function Lbl({ status, label }: { status: Status; label: string }) {
  return <span className={STATUS_CLS[status]}>{label}</span>;
}

/** Message text with leading @mentions marked; the owner's in the warm colour. */
export function Text({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const names = [ownerName, "Growth Lead", "Analyst", "Creative Strategist", "Creative Producer",
                 "Reviewer", "Media Buyer", "Operator"].map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`@(${names.join("|")})(?![\\p{L}\\p{N}_])`, "gu");
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    parts.push(<span key={m.index} className={m[1] === ownerName ? "mention you" : "mention"}>@{m[1]}</span>);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts.map((p, i) => <Fragment key={i}>{p}</Fragment>)}</>;
}

export function ArtCard({ card }: { card: Card }) {
  const need = card.status === "wait";
  return (
    <div className={need ? "art need" : "art"}>
      <div>
        <div className="art-k">{card.title} · {card.status_label}</div>
        <div className="art-t">{card.sub}</div>
        {card.note && <div className="art-s">{card.note}</div>}
        {need && !card.note && <div className="art-s">Nothing happens until you approve it.</div>}
      </div>
      <button className={need ? "btn pri xs" : "btn ghost xs"} onClick={(e) => {
        e.stopPropagation();
        go("deliverables/" + card.key);
      }}>{need ? "Open in Deliverables" : "Open"}</button>
    </div>
  );
}

export const Chevron = () => (
  <svg width="8" height="5" viewBox="0 0 8 5" aria-hidden="true"><path d="M1 1l3 3l3-3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" /></svg>
);
