"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { demoIncidentFetch } from "./fall-incidents-demo";

// 사건 screens, built from the reviewed mockup (Main.dc.html / Incident.dc.html):
// a list page and a detail page in one column. The automatic judgment (robot /
// Cloud AI) is shown but never edited here.

export type IncidentFilter = "all" | "check" | "closed" | "normal" | "report";
type OpinionLabel = "fall" | "suspected_fall" | "normal";
type SceneState = "preparing" | "available" | "partial" | "unavailable" | "expired";

type IncidentSummary = {
  incidentId: string; origin: "robot" | "user_report"; category: "check" | "normal" | "report";
  state: string | null; fallSeen: boolean; assessment: string | null; answer: string | null;
  notificationRank: number; occurredAt: string; updatedAt: string; reviewState: "open" | "closed";
  closedAt: string | null; closedBy: string | null; reopenedAt: string | null;
  needsCheck: boolean; aiFailed: boolean; unacknowledged: boolean; reviewPending: boolean;
  foundDown: boolean; reportedBy: string | null; reportedMomentAt: string | null;
  opinionCounts: Partial<Record<OpinionLabel, number>>;
  sceneState?: SceneState | null; linkedCount?: number;
  notification?: { level: string; sent: number; total: number } | null;
};
type Clip = {
  segmentIndex: number; startAt: string; endAt: string; anchorKinds: string[]; foundDown: boolean;
  clockStepped: boolean; playbackState: SceneState; hasPeople?: boolean;
};
/** [ms from the clip start, left, top, right, bottom] in 1/1000 of the frame. */
type PeopleSample = [number, number, number, number, number];
type ScenePeople = {
  people: Array<{ label: string; target: boolean; samples: PeopleSample[] }>;
  cloud: PeopleSample[];
};
type IncidentDetail = IncidentSummary & {
  viewerEmail: string;
  clips: Clip[];
  robotEvents: Array<{ sequence: number; eventKind: string; occurredAt: string; assessment: string | null;
    answer: string | null; reason: string | null; notificationLevel: string | null }>;
  notifications: Array<{ kind: "first" | "resend" | "reopen"; round: number; level: string; reason: string;
    status: string; createdAt: string; acceptedAt: string | null }>;
  opinions: Array<{ userEmail: string; role: "owner" | "family" | null; label: OpinionLabel; memo: string | null;
    updatedAt: string }>;
  linkedIncidentIds: string[];
  aiReviews: Array<{ reviewId: string; requestedBy: string; momentAt: string; status: string;
    assessment: string | null; explanation: string | null; errorCode: string | null; frameCount: number | null;
    createdAt: string; completedAt: string | null;
    questions: Array<{ askedBy: string; question: string; status: string; answer: string | null; createdAt: string }> }>;
};

const FILTERS: Array<[IncidentFilter, string]> = [
  ["all", "전체"], ["check", "확인 필요"], ["closed", "처리 완료"], ["normal", "정상으로 확인됨"], ["report", "사용자 신고"],
];
const OPINIONS: Array<[OpinionLabel, string]> = [["fall", "낙상"], ["suspected_fall", "낙상 의심"], ["normal", "정상"]];
const OPINION_LABEL: Record<string, string> = Object.fromEntries(OPINIONS);
const ASSESSMENT: Record<string, [string, string]> = {
  observed_fall: ["낙상", "is-fall"], suspected_fall: ["낙상 의심", "is-suspected"],
  normal_activity: ["정상", "is-normal"], unobservable: ["판단 불가", "is-neutral"],
};
const LEVEL_LABEL: Record<string, string> = { urgent: "긴급", check: "확인 필요", info: "일반" };
const EVENT_LABEL: Record<string, string> = {
  incident_opened: "자세 분석: 넘어짐 의심", incident_updated: "새 근거로 사건 갱신",
  question_requested: "로봇이 \"괜찮으세요?\" 질문", voice_result: "질문에 대한 답",
  decision_required: "추가 판단 필요", notification_requested: "알림 요청",
  agent_check_failed: "로봇 질문 실패", analysis_completed: "클라우드 AI",
  analysis_unavailable: "클라우드 AI 분석 실패", stale_analysis_result: "늦게 도착한 분석 결과",
  recheck_unavailable: "재확인 실패", incident_resolved: "로봇이 사건 종료", confirmation_completed: "상황 확인 완료",
};
const ANSWER_LABEL: Record<string, string> = {
  help_request: "본인이 도움을 요청함", okay: "괜찮다고 답함", unclear: "답이 불분명", no_response: "무응답", failed: "질문 실패",
};
const SCENE: Record<SceneState, [string, string]> = {
  preparing: ["장면 영상 준비 중", "is-warn"], available: ["장면 영상 재생 가능", "is-ok"],
  partial: ["장면 영상 일부 누락", "is-warn"], unavailable: ["장면 영상 없음", "is-off"],
  expired: ["보관 기간 만료 (7일)", "is-off"],
};
const AI_ERROR_LABEL: Record<string, string> = {
  cloud_consent_off: "클라우드 분석 동의가 꺼져 있음", key_missing: "클라우드 AI 키 없음", model_unknown: "로봇 모델 정보 없음",
  frames_unavailable: "녹화 사진을 충분히 찾지 못함", cloud_quota_exhausted: "AI 사용 한도 초과",
  cloud_auth_required: "AI 키 인증 실패", cloud_timeout: "AI 응답 시간 초과", worker_lost: "처리 중단",
};

const clock = (value: string, seconds = false) => new Date(value).toLocaleTimeString("ko-KR",
  { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false });
function dayLabel(value: string) {
  const date = new Date(value), today = new Date();
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return "오늘";
  if (date.toDateString() === yesterday.toDateString()) return "어제";
  return date.toLocaleDateString("ko-KR", { month: "long", day: "numeric" });
}
const when = (value: string) => `${dayLabel(value)} ${clock(value)}`;

function title(i: IncidentSummary) {
  if (i.origin === "user_report") return "놓친 넘어짐 신고";
  return i.fallSeen || i.assessment === "observed_fall" ? "낙상" : "낙상 의심";
}

function subtitle(i: IncidentSummary) {
  if (i.origin === "user_report") {
    return `${when(i.reportedMomentAt ?? i.occurredAt)} 구간 · ${i.reportedBy ?? "사용자"} 신고`;
  }
  const reason = i.aiFailed ? "AI가 시간 안에 답하지 못함"
    : i.answer ? ANSWER_LABEL[i.answer] ?? null : i.foundDown ? "이미 쓰러진 모습 발견" : null;
  return [when(i.occurredAt), reason].filter(Boolean).join(" · ");
}

function badges(i: IncidentSummary): Array<[string, string]> {
  const list: Array<[string, string]> = [];
  if (i.unacknowledged) {
    list.push(["is-alert", "아무도 확인하지 않음"]);
    if (i.notification) list.push(["is-alert-soft", `알림: ${LEVEL_LABEL[i.notification.level]} · ${i.notification.sent}/${i.notification.total}회 발송`]);
  } else if (i.reviewState === "open" && i.needsCheck) list.push(["is-check", "확인 필요"]);
  if (i.category === "normal") list.push(["is-ok", "정상으로 확인됨"]);
  if (i.category === "report") list.push(["is-report", "사용자 신고"], ["is-neutral", "자동 감지 아님"]);
  if (i.reviewState === "closed") list.push(["is-neutral", "처리 완료"]);
  else if (i.reviewPending) list.push(["is-neutral", "검수 전"]);
  if (i.reopenedAt && i.reviewState === "open") list.push(["is-neutral", "다시 열림"]);
  if (i.aiFailed) list.push(["is-neutral", "AI 검증 실패"]);
  if (Object.keys(i.opinionCounts).length > 1) list.push(["is-neutral", "의견이 엇갈림"]);
  if (!i.unacknowledged && i.reviewState === "open" && i.notification && i.notification.sent > 1) {
    // Same count as 알림 이력: this send / all sends of the level.
    list.push(["is-neutral", `[재발신] ${i.notification.sent}/${i.notification.total}회`]);
  }
  return list;
}

function Badges({ items }: { items: Array<[string, string]> }) {
  return <div className="fall-badges">{items.map(([tone, text]) => <span key={text} className={tone}>{text}</span>)}</div>;
}

async function json(response: Response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : "요청을 처리하지 못했습니다.");
  return body;
}

type Request = (url: string, init?: RequestInit) => Promise<Response>;

const OVERLAY_KEY = "malbut.fall.personOverlay";
const POSE_GAP_MS = 600;
const CLOUD_SHOW_MS = 500;

type Box = [number, number, number, number];

/** Robot Pose: interpolated between samples; hidden across a gap in tracking. */
function poseBoxAt(samples: PeopleSample[], now: number): Box | null {
  let i = 0;
  while (i < samples.length && samples[i][0] <= now) i += 1;
  const before = samples[i - 1], after = samples[i];
  if (before && after && after[0] - before[0] <= POSE_GAP_MS) {
    const k = (now - before[0]) / (after[0] - before[0]);
    return [1, 2, 3, 4].map((j) => before[j] + (after[j] - before[j]) * k) as Box;
  }
  if (before && now - before[0] <= POSE_GAP_MS / 2) return before.slice(1) as Box;
  return null;
}

/** Cloud AI: only around the analyzed photo it came from. */
function cloudBoxesAt(samples: PeopleSample[], now: number): Box[] {
  return samples.filter((s) => Math.abs(s[0] - now) <= CLOUD_SHOW_MS).map((s) => s.slice(1) as Box);
}

function PersonBox({ box, kind, label }: { box: Box; kind: string; label: string }) {
  return (
    <div className={`fall-person ${kind}`} style={{
      left: `${box[0] / 10}%`, top: `${box[1] / 10}%`,
      width: `${(box[2] - box[0]) / 10}%`, height: `${(box[3] - box[1]) / 10}%`,
    }}><span>{label}</span></div>
  );
}

function PeopleOverlay({ scene, now }: { scene: ScenePeople; now: number }) {
  return (
    <div className="fall-people" aria-hidden>
      {scene.people.map((person) => {
        const box = poseBoxAt(person.samples, now);
        return box && <PersonBox key={person.label} box={box} label={person.label}
          kind={person.target ? "is-target" : "is-other"} />;
      })}
      {cloudBoxesAt(scene.cloud, now).map((box, i) => <PersonBox key={`ai-${i}`} box={box} kind="is-cloud" label="AI 추정" />)}
    </div>
  );
}

function useScenePlayer({ deviceId, incidentId, clip, request, scene, demo }: {
  deviceId: string; incidentId: string; clip: Clip; request: Request;
  /** Boxes to draw, or null when 사람 표시 is off or has nothing. */
  scene: ScenePeople | null; demo: boolean;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [message, setMessage] = useState("");
  // Milliseconds from the clip start of the frame on screen.
  const [now, setNow] = useState<number | null>(null);
  const offsetRef = useRef(0);

  useEffect(() => {
    if (!playing || !scene) return;
    const clipLength = Date.parse(clip.endAt) - Date.parse(clip.startAt);
    const startedAt = performance.now();
    let frame = 0;
    const tick = () => {
      const video = videoRef.current;
      // Local demo has no video: loop the clip's time on a still image.
      if (demo) setNow((performance.now() - startedAt) % clipLength);
      else if (video && video.readyState >= 2) setNow((video.currentTime - offsetRef.current) * 1000);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, scene, demo, clip.startAt, clip.endAt]);

  useEffect(() => {
    if (!playing || demo) return;
    const video = videoRef.current;
    const controller = new AbortController();
    let dispose = () => undefined as void;
    let start = 0, end = Number.POSITIVE_INFINITY;
    if (!video) return;
    const stopAtEnd = () => { if (video.currentTime >= end) video.pause(); };
    const ready = () => { video.currentTime = start; setMessage(""); void video.play().catch(() => undefined); };
    video.addEventListener("timeupdate", stopAtEnd);
    video.addEventListener("loadedmetadata", ready);
    const run = async () => {
      const url = `/api/devices/${encodeURIComponent(deviceId)}/fall-incidents/${encodeURIComponent(incidentId)}` +
        `/clips/${clip.segmentIndex}/playback`;
      for (let attempt = 0; attempt < 10; attempt += 1) {
        const response = await request(url, { method: "POST", signal: controller.signal });
        const body = await response.json().catch(() => ({}));
        if (response.ok && typeof body.playbackUrl === "string") {
          start = Number(body.seekAdjustmentSeconds) || 0;
          offsetRef.current = start;
          end = start + (Number(body.durationSeconds) || Number.POSITIVE_INFINITY);
          return body.playbackUrl as string;
        }
        if (response.status !== 425) throw new Error(body.error ?? "장면 영상을 불러오지 못했습니다.");
        setMessage("장면 영상을 저장하고 있습니다…");
        await new Promise((resolve) => setTimeout(resolve, 5_000));
      }
      throw new Error("장면 영상 준비 시간이 초과되었습니다.");
    };
    setMessage("장면 영상을 불러오는 중입니다…");
    void run().then(async (playbackUrl) => {
      if (video.canPlayType("application/vnd.apple.mpegurl")) { video.src = playbackUrl; video.load(); return; }
      const { default: Hls } = await import("hls.js");
      if (!Hls.isSupported()) throw new Error("이 브라우저는 HLS 재생을 지원하지 않습니다.");
      const player = new Hls({ enableWorker: true });
      player.on(Hls.Events.ERROR, (_name, data) => { if (data.fatal) setMessage("장면 영상을 재생하지 못했습니다."); });
      player.loadSource(playbackUrl);
      player.attachMedia(video);
      dispose = () => player.destroy();
    }).catch((error) => {
      if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "장면 영상을 불러오지 못했습니다.");
    });
    return () => {
      controller.abort();
      dispose();
      video.removeEventListener("timeupdate", stopAtEnd);
      video.removeEventListener("loadedmetadata", ready);
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [deviceId, incidentId, clip.segmentIndex, playing, request, demo]);

  return { playing, setPlaying, view: (
    <div className="fall-scene">
      <video ref={videoRef} controls={playing} playsInline hidden={!playing || demo} />
      {playing && demo && <div className="fall-scene-still" />}
      {playing && scene && now !== null && <PeopleOverlay scene={scene} now={now} />}
      {!playing && <span className="fall-scene-hint">당시 영상 보기를 누르면 이 구간을 재생해요</span>}
      {message && <span className="fall-scene-message" role="status">{message}</span>}
      <div className="fall-scene-bar">
        <span>{clock(clip.startAt, true)} – {clock(clip.endAt, true)}</span>
        <span>{SCENE[clip.playbackState][0]}</span>
      </div>
    </div>
  ) };
}

function readOverlayPreference() {
  try { return window.localStorage.getItem(OVERLAY_KEY) !== "off"; } catch { return true; }
}

function Scene({ deviceId, incidentId, clip, request, demo, onOpenLive }: {
  deviceId: string; incidentId: string; clip: Clip; request: Request; demo: boolean;
  onOpenLive?: () => void;
}) {
  // Scene mounts only after the detail loads on the client, so storage is readable here.
  const [showPeople, setShowPeople] = useState(readOverlayPreference);
  const [loaded, setLoaded] = useState<ScenePeople | null>(null);
  const [wanted, setWanted] = useState(false);
  const toggle = (on: boolean) => {
    setShowPeople(on);
    try { window.localStorage.setItem(OVERLAY_KEY, on ? "on" : "off"); } catch { /* preference only */ }
  };
  // Boxes are fetched once, when the scene is first played with 사람 표시 on.
  useEffect(() => {
    if (!wanted || !showPeople || !clip.hasPeople || loaded) return;
    const controller = new AbortController();
    const url = `/api/devices/${encodeURIComponent(deviceId)}/fall-incidents/${encodeURIComponent(incidentId)}` +
      `/clips/${clip.segmentIndex}/people`;
    void request(url, { signal: controller.signal }).then(async (response) => {
      if (response.ok) setLoaded(await response.json() as ScenePeople);
    }).catch(() => undefined); // The video plays without boxes.
    return () => controller.abort();
  }, [wanted, showPeople, clip.hasPeople, clip.segmentIndex, loaded, deviceId, incidentId, request]);
  const scene = showPeople ? loaded : null;
  const player = useScenePlayer({ deviceId, incidentId, clip, request, scene, demo });
  const playable = ["available", "partial", "preparing"].includes(clip.playbackState);
  return (
    <>
      {player.view}
      <div className="fall-scene-note">
        <span>{clip.anchorKinds.includes("user_report") ? "신고한 순간 10초 전 ~ 20초 후" : "의심 시점 10초 전 ~ 20초 후"}</span>
        {clip.hasPeople && (
          <label className="fall-people-toggle">
            <input type="checkbox" checked={showPeople} onChange={(e) => toggle(e.target.checked)} /> 사람 표시
          </label>
        )}
      </div>
      {scene && (
        <div className="fall-people-legend">
          {scene.people.some((p) => p.target) && <span><i className="is-target" />이 사건의 사람</span>}
          {scene.people.some((p) => !p.target) && <span><i className="is-other" />다른 사람</span>}
          {scene.cloud.length > 0 && <span><i className="is-cloud" />클라우드 AI 추정 위치</span>}
        </div>
      )}
      {clip.foundDown && <p className="fall-hint">넘어진 순간은 녹화되지 않았을 수 있음 · 이미 쓰러진 모습을 발견한 시점 기준이에요.</p>}
      {clip.clockStepped && <p className="fall-hint">{clip.hasPeople && showPeople
        ? "로봇 시계가 바뀌어 시각과 사람 표시가 조금 어긋날 수 있어요."
        : "로봇 시계가 바뀌어 시각이 정확하지 않을 수 있어요."}</p>}
      <div className="fall-two-buttons">
        <button type="button" className="fall-button is-dark" disabled={!playable}
          onClick={() => { setWanted(true); player.setPlaying(true); }}>당시 영상 보기</button>
        <button type="button" className="fall-button" disabled={!onOpenLive} onClick={onOpenLive}>지금 실시간으로 보기</button>
      </div>
    </>
  );
}

export function FallIncidentsPanel({ deviceId, initialIncidentId, onIncidentChange, onOpenLive, onOpenTimeline,
  demo = false }: {
  deviceId: string;
  initialIncidentId?: string;
  onIncidentChange?: (incidentId: string | null) => void;
  onOpenLive?: () => void;
  /** 연속 녹화 화면; with an incident it opens "AI에게 다시 검토 받기" pre-filled. */
  onOpenTimeline?: (incident?: { incidentId: string; momentAt: string; title: string;
    rangeStart: string; rangeEnd: string }) => void;
  /** Local UI demo: an in-memory API instead of the server. */
  demo?: boolean;
}) {
  const [filter, setFilter] = useState<IncidentFilter>("all");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState(initialIncidentId ?? "");
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [draftLabel, setDraftLabel] = useState<OpinionLabel | null>(null);
  const [draftMemo, setDraftMemo] = useState("");
  const [question, setQuestion] = useState("");
  const [withContext, setWithContext] = useState(true);
  const [segment, setSegment] = useState(0);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const base = `/api/devices/${encodeURIComponent(deviceId)}`;
  const request = useCallback<Request>((url, init) =>
    demo ? demoIncidentFetch(url, init) : fetch(url, init), [demo]);

  const loadList = useCallback(async () => {
    if (!deviceId) return;
    setLoading(true);
    try {
      const body = await json(await request(`${base}/fall-incidents?filter=${filter}`, { cache: "no-store" }));
      setIncidents(Array.isArray(body.incidents) ? body.incidents : []);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "사건 목록을 불러오지 못했습니다.");
    } finally { setLoading(false); }
  }, [base, deviceId, filter, request]);

  const loadDetail = useCallback(async (incidentId: string, keepDraft = false) => {
    if (!incidentId) { setDetail(null); return; }
    try {
      const body = await json(await request(`${base}/fall-incidents/${encodeURIComponent(incidentId)}`, { cache: "no-store" }));
      const incident = body.incident as IncidentDetail;
      setDetail(incident);
      if (!keepDraft) {
        const mine = incident.opinions.find((o) => o.userEmail === incident.viewerEmail);
        setDraftLabel(mine?.label ?? null);
        setDraftMemo(mine?.memo ?? "");
      }
    } catch (reason) {
      setDetail(null);
      setNotice(reason instanceof Error ? reason.message : "사건을 불러오지 못했습니다.");
    }
  }, [base, request]);

  useEffect(() => { window.queueMicrotask(() => void loadList()); }, [loadList]);
  useEffect(() => { window.queueMicrotask(() => void loadDetail(selectedId)); }, [loadDetail, selectedId]);
  useEffect(() => {
    // Reviews and reminders change on the server; refresh while the screen is open.
    const timer = window.setInterval(() => {
      if (selectedId) void loadDetail(selectedId, true); else void loadList();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadDetail, loadList, selectedId]);

  const open = (incidentId: string) => {
    setSelectedId(incidentId);
    setSegment(0);
    setNotice("");
    setQuestion("");
    onIncidentChange?.(incidentId);
    window.scrollTo?.({ top: 0 });
  };
  const back = () => {
    setSelectedId("");
    setDetail(null);
    setNotice("");
    onIncidentChange?.(null);
    void loadList();
  };

  const act = async (name: string, url: string, init: RequestInit, done: (body: Record<string, unknown>) => string) => {
    if (!detail) return;
    setBusy(name);
    try {
      const body = await json(await request(url, init));
      setNotice(done(body));
      await loadDetail(detail.incidentId, name === "question");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "요청을 처리하지 못했습니다.");
    } finally { setBusy(""); }
  };
  const post = (body: unknown, method = "POST"): RequestInit =>
    ({ method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

  if (selectedId) {
    if (!detail) {
      return (
        <div className="fall-page">
          <div className="fall-topbar"><button type="button" className="fall-link" onClick={back}>‹ 사건 목록</button></div>
          <p className="fall-empty" role="status">{notice || "사건을 불러오는 중입니다…"}</p>
        </div>
      );
    }
    const incidentPath = `${base}/fall-incidents/${encodeURIComponent(detail.incidentId)}`;
    const mine = detail.opinions.find((o) => o.userEmail === detail.viewerEmail) ?? null;
    const changed = (mine?.label ?? null) !== draftLabel || (draftLabel !== null && (mine?.memo ?? "") !== draftMemo);
    const latest = detail.aiReviews.at(-1) ?? null;
    const clip = detail.clips[Math.min(segment, Math.max(0, detail.clips.length - 1))];
    const moment = detail.origin === "user_report" ? detail.reportedMomentAt ?? detail.occurredAt : detail.occurredAt;
    const who = (o: IncidentDetail["opinions"][number]) =>
      `${o.role === "owner" ? "소유자" : o.role === "family" ? "공유 사용자" : o.userEmail}${o.userEmail === detail.viewerEmail ? " (나)" : ""}`;
    return (
      <div className="fall-page">
        <div className="fall-topbar"><button type="button" className="fall-link" onClick={back}>‹ 사건 목록</button></div>
        <header className="fall-head">
          <Badges items={badges(detail)} />
          <h1>{title(detail)}</h1>
          <div className="fall-sub">{when(moment)}{detail.reportedBy ? ` · ${detail.reportedBy} 신고` : ""}</div>
          {detail.linkedIncidentIds.map((id) => (
            <button type="button" key={id} className="fall-linked" onClick={() => open(id)}>
              같은 시간에 다른 사람의 사건도 있어요 ›
            </button>
          ))}
        </header>
        {notice && <p className="fall-notice" role="status">{notice}</p>}

        <section className="fall-scene-section">
          {detail.clips.length > 1 && (
            <div className="fall-segments" role="group" aria-label="장면 구간">
              {detail.clips.map((c, index) => (
                <button type="button" key={c.segmentIndex} aria-pressed={index === segment}
                  className={index === segment ? "is-on" : ""} onClick={() => setSegment(index)}>
                  {clock(c.startAt, true)}
                </button>
              ))}
            </div>
          )}
          {clip ? (
            <Scene key={`${detail.incidentId}-${clip.segmentIndex}`} deviceId={deviceId} incidentId={detail.incidentId}
              clip={clip} request={request} demo={demo} onOpenLive={onOpenLive} />
          ) : <p className="fall-hint">아직 장면 구간이 도착하지 않았어요.</p>}
        </section>

        <section className="fall-card">
          <h2>사용자 의견</h2>
          <p className="fall-hint">의견은 기록만 하며, 사건은 &quot;처리 완료&quot;를 눌러야 닫혀요.</p>
          {detail.opinions.map((o) => (
            <div className="fall-opinion" key={o.userEmail}>
              <div><strong>{who(o)} · {clock(o.updatedAt)}</strong>
                <span className={`fall-pill is-${o.label}`}>{OPINION_LABEL[o.label]}</span></div>
              {o.memo && <p>메모: {o.memo}</p>}
            </div>
          ))}
          <div className="fall-opinion-pick">
            <div id="fall-opinion-label" className="fall-label">내 의견 고르기</div>
            <div role="group" aria-labelledby="fall-opinion-label" className="fall-toggle">
              {OPINIONS.map(([key, label]) => (
                <button type="button" key={key} aria-pressed={draftLabel === key}
                  className={`is-${key} ${draftLabel === key ? "is-on" : ""}`}
                  onClick={() => setDraftLabel(draftLabel === key ? null : key)}>{label}</button>
              ))}
            </div>
            <p className="fall-hint">{draftLabel ? "선택됨 · \"의견 남기기\"를 누르면 기록돼요" : "하나를 눌러 고르세요. 다시 누르면 선택이 풀려요"}</p>
          </div>
          <label className="fall-field">메모 (선택)
            <input type="text" maxLength={500} placeholder="상황을 적어 주세요" value={draftMemo} disabled={!draftLabel}
              onChange={(e) => setDraftMemo(e.target.value)} />
          </label>
          <button type="button" className="fall-button" disabled={!changed || busy === "opinion"}
            onClick={() => void act("opinion", `${incidentPath}/opinion`,
              post({ label: draftLabel, memo: draftLabel ? draftMemo : null }, "PUT"),
              (body) => body.reopened ? "다른 의견이라 사건을 다시 열고 모두에게 알렸어요." : draftLabel ? "의견을 남겼어요." : "의견을 지웠어요.")}>
            {busy === "opinion" ? "남기는 중…" : draftLabel === null && mine ? "내 의견 지우기" : "의견 남기기"}
          </button>
          <p className="fall-hint">의견을 남기면 모든 사용자에게 가는 [재발신]이 멈춰요.</p>
        </section>

        <section className="fall-card">
          <h2>AI 검토</h2>
          {latest ? (
            <div className="fall-ai-result">
              {latest.status === "completed" && latest.assessment
                ? <span className={`fall-pill ${ASSESSMENT[latest.assessment]?.[1] ?? "is-neutral"}`}>{ASSESSMENT[latest.assessment]?.[0] ?? latest.assessment}</span>
                : <span className="fall-pill is-neutral">{latest.status === "failed"
                  ? `검토 실패 · ${AI_ERROR_LABEL[latest.errorCode ?? ""] ?? latest.errorCode}` : "검토 진행 중"}</span>}
              <span className="fall-sub">사진만으로 받은 판정 · {clock(latest.completedAt ?? latest.createdAt, true)}</span>
              {latest.explanation && <p>{latest.explanation}</p>}
            </div>
          ) : <p className="fall-hint">아직 AI 검토를 받지 않았어요.</p>}
          {onOpenTimeline && (
            <>
              <button type="button" className="fall-button"
                onClick={() => onOpenTimeline({
                  incidentId: detail.incidentId, momentAt: moment,
                  title: `${title(detail)} · ${when(moment)}`,
                  // The original scene range: inside it, the result attaches to this incident.
                  rangeStart: detail.clips[0]?.startAt ?? new Date(Date.parse(moment) - 10_000).toISOString(),
                  rangeEnd: detail.clips.at(-1)?.endAt ?? new Date(Date.parse(moment) + 20_000).toISOString(),
                })}>AI에게 다시 검토 받기 ›</button>
              <p className="fall-hint">연속 녹화에서 이 사건의 순간을 확인하고 고친 뒤 보내요.</p>
            </>
          )}
          {latest?.status === "completed" && (
            <div className="fall-ask">
              <label className="fall-field is-strong">이 사진에 대해 물어보기
                <input type="text" maxLength={500} placeholder="예: 머리를 부딪혔는지 보이나요?" value={question}
                  onChange={(e) => setQuestion(e.target.value)} />
              </label>
              <div className="fall-switch-row">
                <span>내 메모와 이전 판정 기록 함께 보내기</span>
                <button type="button" role="switch" aria-checked={withContext} aria-label="내 메모와 이전 판정 기록 함께 보내기"
                  className={`fall-switch ${withContext ? "is-on" : ""}`} onClick={() => setWithContext(!withContext)}>
                  <span />
                </button>
              </div>
              <button type="button" className="fall-button is-blue"
                disabled={!question.trim() || busy === "question" || latest.questions.some((q) => q.status === "queued" || q.status === "running")}
                onClick={() => void act("question", `${incidentPath}/ai-reviews/${encodeURIComponent(latest.reviewId)}/questions`,
                  post({ question: question.trim(), includeContext: withContext }),
                  () => { setQuestion(""); return "질문을 보냈어요. 답이 오면 아래에 표시돼요."; })}>
                {busy === "question" ? "보내는 중…" : "질문하기"}
              </button>
              {latest.questions.map((q, index) => (
                <div className="fall-answer" key={`${latest.reviewId}-${index}`}>
                  <span>참고 답변 · 판정 결과에는 반영되지 않아요</span>
                  <strong>{q.question}</strong>
                  <p>{q.status === "completed" ? q.answer : q.status === "failed" ? "답을 받지 못했어요." : "답을 기다리는 중…"}</p>
                </div>
              ))}
            </div>
          )}
        </section>

        <details className="fall-card fall-fold">
          <summary>자동 판정 기록</summary>
          <p className="fall-hint">로봇과 AI의 판단이에요. 사용자 의견으로 바뀌지 않아요.</p>
          <div className="fall-log">
            {detail.robotEvents.map((e) => (
              <div key={e.sequence}>
                <span>{clock(e.occurredAt, true)}</span>
                <span>{EVENT_LABEL[e.eventKind] ?? e.eventKind}
                  {e.assessment ? `: ${ASSESSMENT[e.assessment]?.[0] ?? e.assessment}` : ""}
                  {e.answer ? ` · ${ANSWER_LABEL[e.answer] ?? e.answer}` : ""}</span>
              </div>
            ))}
            {detail.robotEvents.length === 0 && <div><span /><span>자동 판정 기록이 없어요 (사용자 신고).</span></div>}
          </div>
        </details>
        <details className="fall-card fall-fold">
          <summary>알림 이력</summary>
          <div className="fall-log">
            {detail.notifications.map((n) => {
              const total = n.level === "urgent" ? 3 : n.level === "check" ? 2 : 1;
              return (
                <div key={`${n.kind}-${n.createdAt}-${n.round}`}>
                  <span>{clock(n.createdAt, true)}</span>
                  <span>{n.kind === "resend" ? `[재발신] ${LEVEL_LABEL[n.level] ?? n.level} · ${n.round}/${total}회`
                    : n.kind === "reopen" ? "다시 열림 · 확인 필요 알림 · 모든 사용자"
                      : `${LEVEL_LABEL[n.level] ?? n.level} 알림 · 모든 사용자`}
                    {n.status === "canceled" ? " (취소됨)" : n.status === "pending" ? " (보내는 중)" : ""}</span>
                </div>
              );
            })}
            {detail.notifications.length === 0 && <div><span /><span>보낸 알림이 없어요.</span></div>}
          </div>
        </details>

        <div className="fall-bottom">
          {detail.reviewState === "closed" ? (
            <p className="fall-closed">처리 완료됨 · {detail.closedBy === detail.viewerEmail ? "나" : detail.closedBy} · {when(detail.closedAt ?? detail.updatedAt)}</p>
          ) : (
            <button type="button" className="fall-button is-blue is-large"
              disabled={busy === "close" || detail.opinions.length === 0}
              onClick={() => void act("close", `${incidentPath}/close`, post({}), () => "처리 완료했어요.")}>
              {busy === "close" ? "처리 중…" : "처리 완료"}
            </button>
          )}
          {detail.reviewState === "open" && detail.opinions.length === 0 && (
            <p className="fall-needs-opinion">의견을 먼저 남겨 주세요. 누구든 의견이 하나 있어야 처리 완료할 수 있어요.</p>
          )}
          <p>누구나 누를 수 있어요. 닫힌 뒤 다른 의견이 달리면 다시 열리고 모두에게 알려요.</p>
          <p>녹화 영상은 7일이 지나면 자동으로 지워져요. 그 뒤에도 사건 기록은 남아요.</p>
        </div>
      </div>
    );
  }

  // List page: "가장 먼저 확인할 사건", then 오늘 / 어제 / 지난 기록.
  const first = incidents.filter((i) => i.unacknowledged);
  const rest = incidents.filter((i) => !i.unacknowledged);
  const groups: Array<[string, IncidentSummary[]]> = [];
  for (const incident of rest) {
    const day = dayLabel(incident.origin === "user_report" ? incident.reportedMomentAt ?? incident.occurredAt : incident.occurredAt);
    const label = day === "오늘" || day === "어제" ? day : "지난 기록";
    const group = groups.find(([name]) => name === label);
    if (group) group[1].push(incident); else groups.push([label, [incident]]);
  }
  const card = (i: IncidentSummary) => (
    <button type="button" key={i.incidentId} onClick={() => open(i.incidentId)}
      className={`fall-incident-card ${i.unacknowledged ? "is-urgent" : i.origin === "user_report" ? "is-report" : ""}`}>
      <Badges items={badges(i)} />
      <strong>{title(i)}</strong>
      <span className="fall-sub">{subtitle(i)}</span>
      {i.sceneState && (
        <span className={`fall-scene-state ${SCENE[i.sceneState][1]}`}><i />{SCENE[i.sceneState][0]}</span>
      )}
      {(i.linkedCount ?? 0) > 0 && <span className="fall-card-link">같은 시간에 다른 사람 사건 {i.linkedCount}건</span>}
    </button>
  );
  return (
    <div className="fall-page">
      <div className="fall-list-head">
        <div>
          <h2>사건</h2>
          {onOpenTimeline && <button type="button" className="fall-link" onClick={() => onOpenTimeline()}>연속 녹화 보기</button>}
        </div>
        <div className="fall-chips" role="group" aria-label="사건 필터">
          {FILTERS.map(([key, label]) => (
            <button type="button" key={key} aria-pressed={filter === key} className={filter === key ? "is-on" : ""}
              onClick={() => setFilter(key)}>{label}</button>
          ))}
        </div>
      </div>
      <div className="fall-list">
        {error && <p className="fall-notice" role="alert">{error}</p>}
        {loading && incidents.length === 0 && <p className="fall-empty" role="status">사건을 불러오는 중입니다…</p>}
        {!loading && !error && incidents.length === 0 && (
          <p className="fall-empty">표시할 사건이 없어요. 넘어짐이 의심되면 사건이 생기고, 그 장면을 연속 녹화에서 바로 볼 수 있어요.</p>
        )}
        {first.length > 0 && <div className="fall-group">가장 먼저 확인할 사건</div>}
        {first.map(card)}
        {groups.map(([label, items]) => (
          <div className="fall-group-block" key={label}>
            <div className="fall-group">{label}</div>
            {items.map(card)}
          </div>
        ))}
      </div>
    </div>
  );
}
