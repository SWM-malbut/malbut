// Local UI demo (NEXT_PUBLIC_HOMECAM_UI_DEMO=1) for the 사건 screen: an
// in-memory stand-in for the incident API. Never used with a real device.

const ME = "demo-user-me";
const ME_NAME = "서연";
const minutes = (n: number) => new Date(Date.now() - n * 60_000).toISOString();
const range = (n: number) => ({ startAt: minutes(n + 0.17), endAt: minutes(n - 0.33) });

type Row = Record<string, unknown> & { incidentId: string };
type Box = [number, number, number, number]; // left, top, width, height (fractions)
type Sample = [number, number, number, number, number]; // same as the people API

// Demo person boxes in the people API format: Pose tracks at 5 per second,
// Cloud AI boxes at a few analyzed frames. `at(t)` is a box `t` s into the clip.
const lerp = (a: Box, b: Box, k: number) => a.map((v, i) => v + (b[i] - v) * Math.min(1, Math.max(0, k))) as Box;
const sample = (t: number, [l, top, w, h]: Box): Sample =>
  [Math.round(t * 1000), Math.round(l * 1000), Math.round(top * 1000), Math.round((l + w) * 1000), Math.round((top + h) * 1000)];
function track(label: string, target: boolean, seconds: number, at: (t: number) => Box) {
  const samples: Sample[] = [];
  for (let n = 0; n * 0.2 <= seconds; n += 1) samples.push(sample(n * 0.2, at(n * 0.2)));
  return { label, target, samples };
}
const STANDING: Box = [0.15, 0.26, 0.12, 0.52];
const LYING: Box = [0.33, 0.62, 0.3, 0.17];
const people: Record<string, { people: ReturnType<typeof track>[]; cloud: Sample[] }> = {
  "demo-1": {
    people: [
      track("사람 1", true, 30, (t) => t < 9
        ? lerp(STANDING, [0.36, 0.26, 0.12, 0.52], t / 9)
        : lerp([0.36, 0.26, 0.12, 0.52], LYING, (t - 9) / 1.5)),
      track("사람 2", false, 30, (t) => t < 14 ? [0.74, 0.3, 0.11, 0.48]
        : t < 20 ? lerp([0.74, 0.3, 0.11, 0.48], [0.6, 0.3, 0.11, 0.48], (t - 14) / 6)
        : lerp([0.6, 0.3, 0.11, 0.48], [0.58, 0.46, 0.14, 0.33], (t - 20) / 2)),
    ],
    cloud: [sample(11, [0.31, 0.6, 0.33, 0.2]), sample(14, [0.32, 0.61, 0.32, 0.19]), sample(17, [0.31, 0.6, 0.34, 0.2])],
  },
  "demo-3": {
    people: [track("사람 1", true, 30, () => LYING)],
    cloud: [sample(10, [0.31, 0.6, 0.33, 0.2]), sample(13, [0.32, 0.61, 0.32, 0.19])],
  },
};

function summary(id: string, change: Record<string, unknown>): Row {
  return {
    incidentId: id, origin: "robot", category: "check", state: "help_required", fallSeen: false,
    assessment: "suspected_fall", answer: null, notificationRank: 2, occurredAt: minutes(18),
    updatedAt: minutes(5), reviewState: "open", closedAt: null, closedBy: null, closedByName: null, reopenedAt: null,
    needsCheck: true, aiFailed: false, unacknowledged: false, reviewPending: false, foundDown: false,
    reportedBy: null, reportedByName: null, reportedMomentAt: null, opinionCounts: {}, ...change,
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
    notificationRank: 0, reportedBy: "demo-user-family", reportedByName: "지민", reportedMomentAt: minutes(60 * 30), occurredAt: minutes(60 * 30),
    sceneState: "expired", linkedCount: 0, notification: null }),
];

const details: Record<string, Record<string, unknown>> = {
  "demo-1": {
    clips: [{ segmentIndex: 0, ...range(18), anchorKinds: ["pose_motion"], foundDown: false, clockStepped: false,
      playbackState: "available", hasPeople: true }],
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
      { userId: "demo-user-owner", userName: "민준", role: "owner", label: "fall", memo: null, updatedAt: minutes(16) },
      { userId: "demo-user-family", userName: "지민", role: "family", label: "normal", memo: "의자에 앉으시다 미끄러지셨는데 다치진 않으셨어요", updatedAt: minutes(14) },
    ],
    linkedIncidentIds: ["demo-1"],
    aiReviews: [{ reviewId: "r1", requestedBy: "demo-user-family", momentAt: minutes(18), status: "completed",
      assessment: "suspected_fall", explanation: "바닥에 앉아 있는 모습이 보이나 넘어지는 과정은 분명하지 않습니다.",
      errorCode: null, frameCount: 12, createdAt: minutes(12), completedAt: minutes(11.9),
      questions: [{ askedBy: "demo-user-family", question: "손으로 짚었나요?", status: "completed",
        answer: "세 번째 사진부터 오른손으로 바닥을 짚는 모습이 보입니다.", createdAt: minutes(11) }] }],
  },
  "demo-3": {
    clips: [{ segmentIndex: 0, ...range(160), anchorKinds: ["cloud_window"], foundDown: true, clockStepped: true,
      playbackState: "partial", hasPeople: true }],
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

function queuedReview(momentAt: string) {
  return { reviewId: `demo-review-${Date.now()}`, requestedBy: ME, momentAt, status: "queued", assessment: null,
    explanation: null, errorCode: null, frameCount: null, createdAt: new Date().toISOString(), completedAt: null,
    questions: [] };
}

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

let fallSettings = { settingsRevision: "3", enabled: true, cameraEnabled: true, cloudConsent: true };
let cloudKey = { configured: true, last4: "7f3a", keyVersion: 2, updatedAt: minutes(60 * 24), robotModel: "gemma4:31b",
  robotHasCurrent: true };

function demoTimeline(url: URL) {
  const from = Date.parse(url.searchParams.get("from") ?? ""), to = Date.parse(url.searchParams.get("to") ?? "");
  const end = Math.min(to, Date.now());
  // Recorded all day except a 90-minute gap ending at 03:00, or an hour ago today, so "now" is always recorded.
  const gapEnd = Math.min(from + 180 * 60_000, Date.now() - 60 * 60_000), gapStart = gapEnd - 90 * 60_000;
  const recordings = [[from, Math.min(gapStart, end)], [Math.max(gapEnd, from), end]].filter(([a, b]) => b > a)
    .map(([a, b]) => ({ startAt: new Date(a).toISOString(), endAt: new Date(b).toISOString() }));
  const marks = incidents.map((i) => ({ incidentId: i.incidentId,
    at: String(i.origin === "user_report" ? i.reportedMomentAt : i.occurredAt),
    kind: i.origin === "user_report" ? "report" : i.fallSeen ? "fall" : "suspected" }))
    .filter((m) => Date.parse(m.at) >= from && Date.parse(m.at) < to);
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString(), recordings, incidents: marks };
}

export async function demoIncidentFetch(url: string, init?: RequestInit): Promise<Response> {
  await new Promise((resolve) => setTimeout(resolve, 150));
  const parsed = new URL(url, "http://demo");
  const path = parsed.pathname;
  const method = init?.method ?? "GET";
  const body = () => JSON.parse(String(init?.body ?? "{}"));
  if (path.endsWith("/fall-timeline")) return reply(demoTimeline(parsed));
  if (path.endsWith("/recording-playback")) return reply({ error: "로컬 데모에는 녹화 영상이 없습니다." }, 404);
  if (path.endsWith("/fall-settings")) {
    if (method === "PATCH") {
      const patch = body();
      fallSettings = { ...fallSettings, ...patch, settingsRevision: String(Number(fallSettings.settingsRevision) + 1) };
      delete (fallSettings as Record<string, unknown>).expectedRevision;
      return reply({ saved: true, savedRevision: fallSettings.settingsRevision });
    }
    return reply({ settings: fallSettings, receiptState: "reported", reports: [] });
  }
  if (path.endsWith("/fall-cloud-key")) {
    if (method === "PUT") cloudKey = { ...cloudKey, configured: true, last4: String(body().apiKey).slice(-4),
      keyVersion: cloudKey.keyVersion + 1, robotHasCurrent: false };
    if (method === "DELETE") cloudKey = { ...cloudKey, configured: false, last4: "", keyVersion: cloudKey.keyVersion + 1,
      robotHasCurrent: false };
    return reply({ ...cloudKey, last4: cloudKey.configured ? cloudKey.last4 : null });
  }
  if (path.endsWith("/fall-reports")) {
    const { momentAt, memo, requestAiReview } = body();
    const id = `demo-report-${incidents.length + 1}`;
    incidents.push(summary(id, { origin: "user_report", category: "report", state: null, assessment: null,
      needsCheck: false, notificationRank: 0, reportedBy: ME, reportedByName: ME_NAME, reportedMomentAt: momentAt, occurredAt: momentAt,
      sceneState: "available", linkedCount: 0, notification: null }));
    details[id] = { clips: [{ segmentIndex: 0, startAt: new Date(Date.parse(momentAt) - 10_000).toISOString(),
      endAt: new Date(Date.parse(momentAt) + 20_000).toISOString(), anchorKinds: ["user_report"], foundDown: false,
      clockStepped: false, playbackState: "available" }], robotEvents: [], notifications: [], opinions: [],
      linkedIncidentIds: [], aiReviews: requestAiReview ? [queuedReview(momentAt)] : [], reportMemo: memo };
    return reply({ incidentId: id, ...(requestAiReview ? { aiReview: { reviewId: "demo-review" } } : {}) }, 201);
  }
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
  const detail = details[row.incidentId] as { opinions: Array<{ userId: string; userName: string; role: string; label: string; memo: string | null; updatedAt: string }> };
  const action = match[2] ?? "";
  if (action === "/opinion") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    detail.opinions = detail.opinions.filter((o) => o.userId !== ME);
    let reopened = false;
    if (body.label) {
      detail.opinions.push({ userId: ME, userName: ME_NAME, role: "family", label: body.label, memo: body.memo || null, updatedAt: new Date().toISOString() });
      row.unacknowledged = false;
      const closedLabels = (row.closedLabels as string[] | undefined) ?? [];
      if (row.reviewState === "closed" && !closedLabels.includes(body.label)) {
        Object.assign(row, { reviewState: "open", closedAt: null, closedBy: null, closedByName: null, reopenedAt: new Date().toISOString(),
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
    Object.assign(row, { reviewState: "closed", closedAt: new Date().toISOString(), closedBy: ME, closedByName: ME_NAME, needsCheck: false,
      unacknowledged: false, reviewPending: false, closedLabels: detail.opinions.map((o) => o.label) });
    return reply({ closed: true, changed: true });
  }
  if (action === "/ai-reviews") {
    const { momentAt } = body();
    const range = (details[row.incidentId] as { clips: Array<{ startAt: string; endAt: string }> }).clips;
    const at = Date.parse(momentAt);
    if (!range.some((c) => at >= Date.parse(c.startAt) && at <= Date.parse(c.endAt))) {
      return reply({ error: "이 시간은 원래 사건 밖이에요. 놓친 낙상으로 새로 신고할까요?", reason: "outside_incident" }, 409);
    }
    (details[row.incidentId] as { aiReviews: unknown[] }).aiReviews.push(queuedReview(momentAt));
    return reply({ review: { status: "queued" } }, 202);
  }
  if (action.endsWith("/people")) {
    const scene = people[row.incidentId];
    return scene ? reply({ segmentIndex: 0, revision: 1, truncated: false, ...scene })
      : reply({ error: "이 장면에는 사람 표시가 없습니다." }, 404);
  }
  if (action.endsWith("/playback")) {
    return reply({ error: "로컬 데모에는 녹화 영상이 없습니다." }, 404);
  }
  return reply({ incident: { ...row, ...detail, viewerUserId: ME, activity: [] } });
}
