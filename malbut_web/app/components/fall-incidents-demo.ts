// Local UI demo (NEXT_PUBLIC_HOMECAM_UI_DEMO=1) for the 사건 screen: an
// in-memory stand-in for the incident API. Never used with a real device.

const ME = "나@example.com";
const minutes = (n: number) => new Date(Date.now() - n * 60_000).toISOString();
const range = (n: number) => ({ startAt: minutes(n + 0.17), endAt: minutes(n - 0.33) });

type Row = Record<string, unknown> & { incidentId: string };

function summary(id: string, change: Record<string, unknown>): Row {
  return {
    incidentId: id, origin: "robot", category: "check", state: "help_required", fallSeen: false,
    assessment: "suspected_fall", answer: null, notificationRank: 2, occurredAt: minutes(18),
    updatedAt: minutes(5), reviewState: "open", closedAt: null, closedBy: null, reopenedAt: null,
    needsCheck: true, aiFailed: false, unacknowledged: false, reviewPending: false, foundDown: false,
    reportedBy: null, reportedMomentAt: null, opinionCounts: {}, ...change,
  };
}

const incidents: Row[] = [
  summary("demo-1", { fallSeen: true, assessment: "observed_fall", answer: "help_request", notificationRank: 3,
    unacknowledged: true, occurredAt: minutes(18), sceneState: "available", linkedCount: 1,
    notification: { level: "urgent", sent: 3, total: 3 } }),
  summary("demo-2", { answer: "no_response", occurredAt: minutes(18), opinionCounts: { fall: 1, normal: 1 },
    sceneState: "preparing", linkedCount: 1, notification: { level: "check", sent: 2, total: 2 } }),
  summary("demo-3", { aiFailed: true, assessment: "unobservable", occurredAt: minutes(160), sceneState: "partial",
    linkedCount: 0, notification: null }),
  summary("demo-4", { category: "normal", state: "resolved", assessment: "normal_activity", answer: "okay",
    needsCheck: false, reviewPending: true, notificationRank: 0, occurredAt: minutes(60 * 20), sceneState: "available",
    linkedCount: 0, notification: null }),
  summary("demo-5", { origin: "user_report", category: "report", state: null, assessment: null, needsCheck: false,
    notificationRank: 0, reportedBy: "가족@example.com", reportedMomentAt: minutes(60 * 30), occurredAt: minutes(60 * 30),
    sceneState: "expired", linkedCount: 0, notification: null }),
];

const details: Record<string, Record<string, unknown>> = {
  "demo-1": {
    clips: [{ segmentIndex: 0, ...range(18), anchorKinds: ["pose_motion"], foundDown: false, clockStepped: false,
      playbackState: "available" }],
    robotEvents: [
      { sequence: 1, eventKind: "incident_opened", occurredAt: minutes(18), assessment: null, answer: null },
      { sequence: 2, eventKind: "analysis_completed", occurredAt: minutes(17.9), assessment: "observed_fall", answer: null },
      { sequence: 3, eventKind: "voice_result", occurredAt: minutes(17.7), assessment: null, answer: "help_request" },
      { sequence: 4, eventKind: "notification_requested", occurredAt: minutes(17.7), assessment: null, answer: "help_request" },
    ],
    notifications: [
      { kind: "first", round: 1, level: "urgent", reason: "help_requested", status: "accepted", createdAt: minutes(17.7), acceptedAt: minutes(17.7) },
      { kind: "resend", round: 2, level: "urgent", reason: "help_requested", status: "accepted", createdAt: minutes(15.5), acceptedAt: minutes(15.5) },
      { kind: "resend", round: 3, level: "urgent", reason: "help_requested", status: "accepted", createdAt: minutes(13.5), acceptedAt: minutes(13.5) },
    ],
    opinions: [], linkedIncidentIds: ["demo-2"], aiReviews: [],
  },
  "demo-2": {
    clips: [{ segmentIndex: 0, ...range(18), anchorKinds: ["pose_motion", "cloud_window"], foundDown: false,
      clockStepped: false, playbackState: "preparing" }],
    robotEvents: [
      { sequence: 1, eventKind: "incident_opened", occurredAt: minutes(18), assessment: null, answer: null },
      { sequence: 2, eventKind: "voice_result", occurredAt: minutes(17.8), assessment: null, answer: "no_response" },
      { sequence: 3, eventKind: "analysis_completed", occurredAt: minutes(17.7), assessment: "suspected_fall", answer: null },
    ],
    notifications: [
      { kind: "first", round: 1, level: "check", reason: "person_no_response", status: "accepted", createdAt: minutes(17.5), acceptedAt: minutes(17.5) },
    ],
    opinions: [
      { userEmail: "소유자@example.com", role: "owner", label: "fall", memo: null, updatedAt: minutes(16) },
      { userEmail: "가족@example.com", role: "family", label: "normal", memo: "의자에 앉으시다 미끄러지셨는데 다치진 않으셨어요", updatedAt: minutes(14) },
    ],
    linkedIncidentIds: ["demo-1"],
    aiReviews: [{ reviewId: "r1", requestedBy: "가족@example.com", momentAt: minutes(18), status: "completed",
      assessment: "suspected_fall", explanation: "바닥에 앉아 있는 모습이 보이나 넘어지는 과정은 분명하지 않습니다.",
      errorCode: null, frameCount: 12, createdAt: minutes(12), completedAt: minutes(11.9),
      questions: [{ askedBy: "가족@example.com", question: "손으로 짚었나요?", status: "completed",
        answer: "세 번째 사진부터 오른손으로 바닥을 짚는 모습이 보입니다.", createdAt: minutes(11) }] }],
  },
  "demo-3": {
    clips: [{ segmentIndex: 0, ...range(160), anchorKinds: ["cloud_window"], foundDown: true, clockStepped: false,
      playbackState: "partial" }],
    robotEvents: [
      { sequence: 1, eventKind: "incident_opened", occurredAt: minutes(160), assessment: null, answer: null },
      { sequence: 2, eventKind: "analysis_unavailable", occurredAt: minutes(159.7), assessment: null, answer: null },
    ],
    notifications: [], opinions: [], linkedIncidentIds: [], aiReviews: [],
  },
  "demo-4": {
    clips: [{ segmentIndex: 0, ...range(60 * 20), anchorKinds: ["pose_motion"], foundDown: false, clockStepped: false,
      playbackState: "available" }],
    robotEvents: [
      { sequence: 1, eventKind: "incident_opened", occurredAt: minutes(1200), assessment: null, answer: null },
      { sequence: 2, eventKind: "incident_resolved", occurredAt: minutes(1199), assessment: "normal_activity", answer: "okay" },
    ],
    notifications: [], opinions: [], linkedIncidentIds: [], aiReviews: [],
  },
  "demo-5": {
    clips: [{ segmentIndex: 0, ...range(60 * 30), anchorKinds: ["user_report"], foundDown: false, clockStepped: false,
      playbackState: "expired" }],
    robotEvents: [], notifications: [], opinions: [], linkedIncidentIds: [], aiReviews: [],
  },
};

const FILTER: Record<string, (row: Row) => boolean> = {
  all: () => true,
  check: (r) => r.reviewState === "open" && r.category === "check",
  closed: (r) => r.reviewState === "closed",
  normal: (r) => r.category === "normal",
  report: (r) => r.category === "report",
};

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function counts(opinions: Array<{ label: string }>) {
  const result: Record<string, number> = {};
  for (const o of opinions) result[o.label] = (result[o.label] ?? 0) + 1;
  return result;
}

export async function demoIncidentFetch(url: string, init?: RequestInit): Promise<Response> {
  await new Promise((resolve) => setTimeout(resolve, 150));
  const path = new URL(url, "http://demo").pathname;
  const list = /\/fall-incidents$/.test(path);
  if (list) {
    const filter = new URL(url, "http://demo").searchParams.get("filter") ?? "all";
    const rows = incidents.filter(FILTER[filter] ?? FILTER.all)
      .sort((a, b) => Number(b.unacknowledged) - Number(a.unacknowledged) ||
        Date.parse(String(b.occurredAt)) - Date.parse(String(a.occurredAt)));
    return reply({ filter, incidents: rows });
  }
  const match = /\/fall-incidents\/([^/]+)(\/[a-z/0-9-]+)?$/.exec(path);
  const row = match && incidents.find((i) => i.incidentId === match[1]);
  if (!row || !match) return reply({ error: "사건을 찾을 수 없습니다." }, 404);
  const detail = details[row.incidentId] as { opinions: Array<{ userEmail: string; role: string; label: string; memo: string | null; updatedAt: string }> };
  const action = match[2] ?? "";
  if (action === "/opinion") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    detail.opinions = detail.opinions.filter((o) => o.userEmail !== ME);
    let reopened = false;
    if (body.label) {
      detail.opinions.push({ userEmail: ME, role: "family", label: body.label, memo: body.memo || null, updatedAt: new Date().toISOString() });
      row.unacknowledged = false;
      const closedLabels = (row.closedLabels as string[] | undefined) ?? [];
      if (row.reviewState === "closed" && !closedLabels.includes(body.label)) {
        Object.assign(row, { reviewState: "open", closedAt: null, closedBy: null, reopenedAt: new Date().toISOString(),
          needsCheck: true, category: row.origin === "user_report" ? "report" : "check" });
        reopened = true;
      }
    }
    row.opinionCounts = counts(detail.opinions);
    return reply({ saved: true, reopened });
  }
  if (action === "/close") {
    if (!detail.opinions.length) {
      return reply({ error: "의견을 먼저 남겨 주세요. 누구든 의견이 하나 있어야 처리 완료할 수 있어요.", reason: "needs_opinion" }, 409);
    }
    Object.assign(row, { reviewState: "closed", closedAt: new Date().toISOString(), closedBy: ME, needsCheck: false,
      unacknowledged: false, reviewPending: false, closedLabels: detail.opinions.map((o) => o.label) });
    return reply({ closed: true, changed: true });
  }
  if (action.endsWith("/playback")) {
    return reply({ error: "로컬 데모에는 녹화 영상이 없습니다." }, 404);
  }
  return reply({ incident: { ...row, ...detail, viewerEmail: ME, activity: [] } });
}
