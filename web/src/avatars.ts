// Bean crew v3: one picture per role, drawn for this project. The owner's picture, or their
// initials, comes from the workspace (see Av in ui.tsx).
const CODES = ["AN", "GL", "CS", "CP", "RV", "MB", "OP"] as const;
export const TEAM: { code: string; name: string }[] = [
  { code: "AN", name: "Analyst" },
  { code: "GL", name: "Growth Lead" },
  { code: "CS", name: "Creative Strategist" },
  { code: "CP", name: "Creative Producer" },
  { code: "RV", name: "Reviewer" },
  { code: "MB", name: "Media Buyer" },
  { code: "OP", name: "Operator" },
];

/** Roles the owner can address with @, with the actor id the API expects. */
export const ADDRESSABLE: { actor: string; code: string; name: string; does: string }[] = [
  { actor: "growth_lead", code: "GL", name: "Growth Lead", does: "Plans the week, hands out work" },
  { actor: "analyst", code: "AN", name: "Analyst", does: "Reads the numbers, writes findings" },
  { actor: "strategist", code: "CS", name: "Creative Strategist", does: "Researches the market" },
  { actor: "producer", code: "CP", name: "Creative Producer", does: "Writes copy, makes images" },
  { actor: "reviewer", code: "RV", name: "Reviewer", does: "Checks claims against evidence" },
  { actor: "media_buyer", code: "MB", name: "Media Buyer", does: "Proposes budget and ad changes" },
  { actor: "operator", code: "OP", name: "Operator", does: "Writes approved changes to the sandbox" },
];

export function avatar(code: string | null | undefined): string {
  if (code && (CODES as readonly string[]).includes(code)) return `avatars/${code}.svg`;
  return "pallas-logo.png"; // System and house rules speak as the team itself.
}
