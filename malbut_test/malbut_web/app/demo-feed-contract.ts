export type DemoLine = {
  id: string;
  speaker: "user" | "robot";
  text: string;
  confirmation: boolean;
  at: number;
};
export type DemoBadge =
  | "낙상 의심"
  | "음성 확인 중"
  | "도움 요청 없음"
  | "도움 요청"
  | "응답 없음";
export type DemoView = {
  session: string;
  lines: DemoLine[];
  count: number | null;
  badge: DemoBadge | null;
};
export type DemoReport = {
  session: string;
  lines: DemoLine[];
  count: number | null;
  suspect: boolean;
};
export const sessionId = (v: unknown): v is string =>
  typeof v === "string" &&
  /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(v);

export function parseReport(raw: unknown): DemoReport | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Partial<DemoReport>;
  if (
    !sessionId(r.session) ||
    !Array.isArray(r.lines) ||
    r.lines.length > 12 ||
    !(
      r.count === null ||
      (Number.isInteger(r.count) &&
        Number(r.count) >= 0 &&
        Number(r.count) <= 100)
    ) ||
    typeof r.suspect !== "boolean"
  )
    return null;
  for (const line of r.lines) {
    if (
      !line ||
      typeof line.id !== "string" ||
      !/^[a-zA-Z0-9:_.-]{1,240}$/.test(line.id) ||
      !["user", "robot"].includes(line.speaker) ||
      typeof line.text !== "string" ||
      !line.text.trim() ||
      line.text.length > 4096 ||
      typeof line.confirmation !== "boolean" ||
      !Number.isSafeInteger(line.at) ||
      line.at <= 0 ||
      line.at > 8640000000000000
    )
      return null;
  }
  return {
    session: r.session,
    lines: r.lines,
    count: r.count!,
    suspect: r.suspect,
  };
}

type Incident = {
  state: string;
  answer: string | null;
  opened: number;
  updated: number;
};
export function fallBadge(
  incident: Incident | undefined,
  lines: DemoLine[],
  suspect: boolean,
  now: number,
): DemoBadge | null {
  if (incident && now - incident.updated <= 600_000) {
    if (
      incident.answer === "help_request" ||
      incident.state === "help_required"
    )
      return "도움 요청";
    if (incident.answer === "okay")
      return now - incident.updated <= 10_000 ? "도움 요청 없음" : null;
    if (incident.answer === "no_response") return "응답 없음";
    if (incident.state === "resolved") return null;
    if (
      lines.some(
        (l) =>
          l.confirmation &&
          l.speaker === "robot" &&
          l.at >= incident.opened - 1000,
      )
    )
      return "음성 확인 중";
    return "낙상 의심";
  }
  if (
    suspect &&
    lines.some(
      (l) =>
        l.confirmation &&
        l.speaker === "robot" &&
        Math.abs(now - l.at) < 15_000,
    )
  )
    return "음성 확인 중";
  return suspect ? "낙상 의심" : null;
}
