"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { demoIncidentFetch } from "./fall-incidents-demo";
import { fallHistoryLines, plainLabels } from "../fall-event-label";
import type { FallHistoryEvent } from "../fall-event-label";
import { clock, dayLabel, incidentBadges, incidentExtras, incidentSubtitle, incidentTitle, when } from "../fall-incident-view";
import type { BadgeTone, IncidentSummary, OpinionLabel, SceneState } from "../fall-incident-view";

// 사건 screens, built from the reviewed mockup (Main.dc.html / Incident.dc.html):
// a list page and a detail page in one column. The automatic judgment (robot /
// Cloud AI) is shown but never edited here.

export type IncidentFilter = "all" | "check" | "closed" | "normal" | "report";
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
  viewerUserId: string;
  clips: Clip[];
  robotEvents: Array<FallHistoryEvent & { sequence: number; occurredAt: string;
    notificationLevel: string | null }>;
  notifications: Array<{ kind: "first" | "resend" | "reopen"; round: number; level: string; reason: string;
    status: string; createdAt: string; acceptedAt: string | null; checkRound?: number }>;
  opinions: Array<{ userId: string; userName: string; role: "owner" | "family" | null; label: OpinionLabel; memo: string | null;
    updatedAt: string }>;
  linkedIncidentIds: string[];
  aiReviews: Array<{ reviewId: string; requestedBy: string; momentAt: string; status: string;
    assessment: string | null; explanation: string | null; errorCode: string | null; frameCount: number | null;
    createdAt: string; completedAt: string | null;
    questions: Array<{ askedBy: string; question: string; status: string; answer: string | null; createdAt: string }> }>;
};

const FILTERS: Array<[IncidentFilter, string]> = [
  ["all", "전체"], ["check", "우선 확인"], ["closed", "처리 완료"], ["normal", "정상"], ["report", "직접 신고"],
];
const OPINIONS: Array<[OpinionLabel, string]> = [["fall", "낙상"], ["suspected_fall", "낙상 의심"], ["normal", "정상"]];
const OPINION_LABEL: Record<string, string> = Object.fromEntries(OPINIONS);
const ASSESSMENT: Record<string, [string, string]> = {
  observed_fall: ["낙상", "is-fall"], suspected_fall: ["낙상 의심", "is-suspected"],
  normal_activity: ["정상", "is-normal"], unobservable: ["판단 불가", "is-neutral"],
};
const LEVEL_LABEL: Record<string, string> = { urgent: "긴급", check: "확인 필요", info: "일반" };
const SCENE: Record<SceneState, [string, string]> = {
  preparing: ["영상 준비 중", "is-warn"], available: ["영상 재생 가능", "is-ok"],
  partial: ["영상 일부 누락", "is-warn"], unavailable: ["영상 없음", "is-off"],
  expired: ["보관 기간 만료 (7일)", "is-off"],
};
const AI_ERROR_LABEL: Record<string, string> = {
  cloud_consent_off: "클라우드 AI 확인 동의가 꺼져 있음", key_missing: "클라우드 AI 키 없음", model_unknown: "말벗 모델 정보 없음",
  frames_unavailable: "녹화 사진을 충분히 찾지 못함", cloud_quota_exhausted: "AI 사용 한도 초과",
  cloud_auth_required: "AI 키 인증 실패", cloud_timeout: "AI 응답 시간 초과", worker_lost: "처리 중단",
  frames_unavailable_yet: "녹화 사진이 아직 준비되지 않음", kvs_unavailable: "녹화 사진을 가져오지 못함",
  kvs_request_invalid: "녹화 사진 요청이 거절됨", cloud_transport_error: "AI 연결 오류",
  cloud_http_error: "AI 서버 오류",
};

/** 로봇이 스스로 닫은 사건: 사람 아님, 바닥에 있지 않음. */
const robotChecked = (i: IncidentSummary) => i.category === "not_person" || i.category === "off_floor";

/** 로봇이 스스로 닫은 시각: 마지막 종료 기록. */
function robotClosedAt(detail: IncidentDetail) {
  const closed = detail.robotEvents.filter((e) => e.eventKind === "incident_resolved").at(-1);
  return closed?.occurredAt ?? detail.updatedAt;
}

function Badges({ items }: { items: Array<[BadgeTone, string]> }) {
  return <div className="fall-badges">{items.map(([tone, text]) => <span key={text} className={tone}>{text}</span>)}</div>;
}

async function json(response: Response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : "요청을 처리하지 못했어요.");
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

function useScenePlayer({ deviceId, incidentId, clip, request, scene, demo, playable, onStart }: {
  deviceId: string; incidentId: string; clip: Clip; request: Request;
  /** Boxes to draw, or null when 사람 표시 is off or has nothing. */
  scene: ScenePeople | null; demo: boolean;
  /** Whether the scene video can be played; the whole scene is its play button. */
  playable: boolean; onStart: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  // 정지 사진 of the suspected moment, before playing. The demo has no archive: its drawn still.
  const stillUrl = playable && !demo
    ? `/api/devices/${encodeURIComponent(deviceId)}/fall-incidents/${encodeURIComponent(incidentId)}` +
      `/clips/${clip.segmentIndex}/still`
    : null;
  const [still, setStill] = useState<"loading" | "shown" | "none">(demo && playable ? "shown" : stillUrl ? "loading" : "none");
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
        if (response.status !== 425) throw new Error(body.error ?? "장면 영상을 불러오지 못했어요.");
        setMessage("장면 영상을 저장하고 있어요…");
        await new Promise((resolve) => setTimeout(resolve, 5_000));
      }
      throw new Error("장면 영상 준비 시간이 지났어요.");
    };
    setMessage("장면 영상을 불러오는 중이에요…");
    void run().then(async (playbackUrl) => {
      if (video.canPlayType("application/vnd.apple.mpegurl")) { video.src = playbackUrl; video.load(); return; }
      const { default: Hls } = await import("hls.js");
      if (!Hls.isSupported()) throw new Error("이 브라우저에서는 장면 영상을 재생할 수 없어요.");
      const player = new Hls({ enableWorker: true });
      player.on(Hls.Events.ERROR, (_name, data) => { if (data.fatal) setMessage("장면 영상을 재생하지 못했어요."); });
      player.loadSource(playbackUrl);
      player.attachMedia(video);
      dispose = () => player.destroy();
    }).catch((error) => {
      if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "장면 영상을 불러오지 못했어요.");
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
      {!playing && (
        <button type="button" className={`fall-scene-start ${still === "shown" ? "has-photo" : ""}`}
          aria-label="장면 영상 보기" disabled={!playable} onClick={onStart}>
          {demo && still === "shown" && <span className="fall-scene-still" aria-hidden="true" />}
          {stillUrl && still !== "none" && (
            // eslint-disable-next-line @next/next/no-img-element -- a private, per-user API image
            <img className="fall-scene-photo" src={stillUrl} alt=""
              onLoad={() => setStill("shown")} onError={() => setStill("none")} />
          )}
          {playable && still === "none" && <span className="fall-scene-hint">누르면 이 구간을 재생해요</span>}
          {playable && (
            <span className="fall-scene-play" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5l11 7-11 7z" /></svg>
            </span>
          )}
          {/* 재생 중에는 없다: 영상 아래쪽의 재생 위치·소리·확대 버튼을 가리지 않게. */}
          <span className="fall-scene-bar">
            <span>{clock(clip.startAt, true)} – {clock(clip.endAt, true)}</span>
            <span>{SCENE[clip.playbackState][0]}</span>
          </span>
        </button>
      )}
      {message && <span className="fall-scene-message" role="status">{message}</span>}
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
  const playable = ["available", "partial", "preparing"].includes(clip.playbackState);
  const player = useScenePlayer({ deviceId, incidentId, clip, request, scene, demo, playable,
    onStart: () => { setWanted(true); player.setPlaying(true); } });
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
      {clip.foundDown && <p className="fall-hint">이미 쓰러진 모습을 본 때부터라, 넘어지는 순간은 없을 수 있어요.</p>}
      {clip.clockStepped && <p className="fall-hint">{clip.hasPeople && showPeople
        ? "말벗 시계가 바뀌어 시각과 사람 표시가 조금 어긋날 수 있어요."
        : "말벗 시계가 바뀌어 시각이 정확하지 않을 수 있어요."}</p>}
      {/* 장면 영상은 위 영상 칸을 눌러 본다(정지 사진·재생 표시). */}
      <button type="button" className="fall-button fall-wide-button" disabled={!onOpenLive} onClick={onOpenLive}>
        지금 실시간으로 보기
      </button>
    </>
  );
}

export function FallIncidentsPanel({ deviceId, initialIncidentId, onIncidentChange, onOpenLive, onOpenTimeline,
  header, demo = false }: {
  deviceId: string;
  initialIncidentId?: string;
  onIncidentChange?: (incidentId: string | null) => void;
  onOpenLive?: () => void;
  /** 연속 녹화 화면; with an incident it opens "AI에게 다시 확인 받기" pre-filled. */
  onOpenTimeline?: (incident?: { incidentId: string; momentAt: string; title: string;
    rangeStart: string; rangeEnd: string }) => void;
  /** The tab top shared by every tab (화면 이름 · 연결 상태 · 연결된 말벗); shown above the list only. */
  header?: ReactNode;
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
      setError(reason instanceof Error ? reason.message : "사건 목록을 불러오지 못했어요.");
    } finally { setLoading(false); }
  }, [base, deviceId, filter, request]);

  const loadDetail = useCallback(async (incidentId: string, keepDraft = false) => {
    if (!incidentId) { setDetail(null); return; }
    try {
      const body = await json(await request(`${base}/fall-incidents/${encodeURIComponent(incidentId)}`, { cache: "no-store" }));
      const incident = body.incident as IncidentDetail;
      setDetail(incident);
      if (!keepDraft) {
        const mine = incident.opinions.find((o) => o.userId === incident.viewerUserId);
        setDraftLabel(mine?.label ?? null);
        setDraftMemo(mine?.memo ?? "");
      }
    } catch (reason) {
      setDetail(null);
      setNotice(reason instanceof Error ? reason.message : "사건을 불러오지 못했어요.");
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
      setNotice(reason instanceof Error ? reason.message : "요청을 처리하지 못했어요.");
    } finally { setBusy(""); }
  };
  const post = (body: unknown, method = "POST"): RequestInit =>
    ({ method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

  if (selectedId) {
    if (!detail) {
      return (
        <div className="fall-page">
          <div className="fall-topbar"><button type="button" className="fall-link" onClick={back}>‹ 사건 목록</button></div>
          <p className="fall-empty" role="status">{notice || "사건을 불러오는 중이에요…"}</p>
        </div>
      );
    }
    const incidentPath = `${base}/fall-incidents/${encodeURIComponent(detail.incidentId)}`;
    const mine = detail.opinions.find((o) => o.userId === detail.viewerUserId) ?? null;
    const changed = (mine?.label ?? null) !== draftLabel || (draftLabel !== null && (mine?.memo ?? "") !== draftMemo);
    const latest = detail.aiReviews.at(-1) ?? null;
    const clip = detail.clips[Math.min(segment, Math.max(0, detail.clips.length - 1))];
    const moment = detail.origin === "user_report" ? detail.reportedMomentAt ?? detail.occurredAt : detail.occurredAt;
    const who = (o: IncidentDetail["opinions"][number]) =>
      `${o.role === "owner" ? "소유자" : o.role === "family" ? "보호자" : o.userName}${o.userId === detail.viewerUserId ? " (나)" : ""}`;
    return (
      <div className="fall-page">
        <div className="fall-topbar"><button type="button" className="fall-link" onClick={back}>‹ 사건 목록</button></div>
        <header className="fall-head">
          <Badges items={incidentBadges(detail)} />
          <h1>{incidentTitle(detail)}</h1>
          <div className="fall-sub">{[`${when(moment)}${detail.reportedBy ? ` · ${detail.reportedByName} 신고` : ""}`,
            ...incidentExtras(detail)].join(" · ")}</div>
          {(detail.mergedIntoIncidentIds ?? []).map((id) => (
            <button type="button" key={`merged-${id}`} className="fall-linked" onClick={() => open(id)}>
              사람 연결을 확인했어요. 이어서 처리하는 사건 보기 ›
            </button>
          ))}
          {detail.linkedIncidentIds.filter((id) => !detail.mergedIntoIncidentIds?.includes(id)).map((id) => (
            <button type="button" key={id} className="fall-linked" onClick={() => open(id)}>
              같은 시간대의 관련 사건 보기 ›
            </button>
          ))}
          {/* 결과 상자는 흰 머리 칸 안, 관련 사건 링크 바로 밑(목업 46번). */}
          {detail.checkResult && (
            <section className="fall-robot-check">
              <span>말벗이 확인한 결과</span>
              <strong>{detail.checkResult.title}</strong>
              {detail.checkResult.note && <p>{detail.checkResult.note}</p>}
            </section>
          )}
          {!detail.checkResult && detail.robotCheck && (
            <section className="fall-robot-check is-alert">
              <span>말벗이 직접 확인하지 못한 이유</span>
              <strong>{detail.robotCheck.reason}</strong>
            </section>
          )}
          {!detail.checkResult && detail.robotCheck?.revisit && (
            <section className="fall-robot-check">
              <span>말벗이 다시 가서 본 결과</span>
              <strong>{detail.robotCheck.revisit.title}</strong>
              {detail.robotCheck.revisit.note && <p>{detail.robotCheck.revisit.note}</p>}
            </section>
          )}
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
          <p className="fall-hint">의견을 남겨도 사건은 닫히지 않아요. &quot;처리 완료&quot;를 눌러야 닫혀요.</p>
          {detail.opinions.map((o) => (
            <div className="fall-opinion" key={o.userId}>
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
            <p className="fall-hint">{draftLabel ? "선택됨 · \"의견 남기기\"를 누르면 기록돼요" : "하나를 눌러 골라 주세요. 다시 누르면 풀려요."}</p>
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
          <p className="fall-hint">의견을 남기면 모두에게 가던 재발신이 멈춰요.</p>
        </section>

        <section className="fall-card">
          <h2>AI 확인</h2>
          {latest ? (
            <div className="fall-ai-result">
              {latest.status === "completed" && latest.assessment
                ? <span className={`fall-pill ${ASSESSMENT[latest.assessment]?.[1] ?? "is-neutral"}`}>{ASSESSMENT[latest.assessment]?.[0] ?? latest.assessment}</span>
                : <span className="fall-pill is-neutral">{latest.status === "failed"
                  ? `확인 실패 · ${AI_ERROR_LABEL[latest.errorCode ?? ""] ?? latest.errorCode}`
                  // A queued review keeps its last failure while it waits to try again.
                  : latest.errorCode
                    ? `다시 시도 중 · ${AI_ERROR_LABEL[latest.errorCode] ?? latest.errorCode}` : "AI가 확인하는 중"}</span>}
              <span className="fall-sub">사진만으로 받은 판정 · {clock(latest.completedAt ?? latest.createdAt, true)}</span>
              {latest.explanation && <p>{plainLabels(latest.explanation)}</p>}
            </div>
          ) : <p className="fall-hint">아직 AI 확인을 받지 않았어요.</p>}
          {onOpenTimeline && (
            <>
              <button type="button" className="fall-button"
                onClick={() => onOpenTimeline({
                  incidentId: detail.incidentId, momentAt: moment,
                  title: `${incidentTitle(detail)} · ${when(moment)}`,
                  // The original scene range: inside it, the result attaches to this incident.
                  rangeStart: detail.clips[0]?.startAt ?? new Date(Date.parse(moment) - 10_000).toISOString(),
                  rangeEnd: detail.clips.at(-1)?.endAt ?? new Date(Date.parse(moment) + 20_000).toISOString(),
                })}>AI에게 다시 확인 받기 ›</button>
              <p className="fall-hint">연속 녹화에서 이 사건의 순간을 다시 골라 AI에게 보내요.</p>
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
          <p className="fall-hint">말벗과 AI의 판정이에요. 사용자 의견으로 바뀌지 않아요.</p>
          <p className="fall-hint">괄호 안 이유는 AI가 쓴 설명이라, 실제와 다를 수 있어요.</p>
          <div className="fall-log">
            {fallHistoryLines(detail.robotEvents).map(({ event: e, label, divider }, index) => (
              <Fragment key={`${e.sequence}-${index}`}>
                {divider && <div className="fall-log-round"><span /><span>— {divider} —</span></div>}
                <div>
                  <span>{clock(e.occurredAt, true)}</span>
                  <span title={e.analysis?.purpose === "crosscheck" ? "장면 전체 분석 이유" : undefined}>
                    {label}</span>
                </div>
              </Fragment>
            ))}
            {detail.robotEvents.length === 0 && <div><span /><span>자동 판정 기록이 없어요 (사용자 신고).</span></div>}
          </div>
        </details>
        <details className="fall-card fall-fold">
          <summary>알림 이력</summary>
          <div className="fall-log">
            {detail.notifications.map((n) => {
              const total = n.level === "urgent" ? 3 : n.level === "check" ? 2 : 1;
              // 목업 31번: the robot's check round, once the incident has more than one.
              const rounds = detail.notifications.some((x) => (x.checkRound ?? 1) > 1);
              return (
                <div key={`${n.kind}-${n.createdAt}-${n.round}`}>
                  <span>{clock(n.createdAt, true)}</span>
                  <span>{n.kind === "resend" ? `[재발신] ${LEVEL_LABEL[n.level] ?? n.level} · ${n.round}/${total}회`
                    : n.kind === "reopen" ? "다시 열림 · 확인 필요 알림 · 모든 사용자"
                      : `${LEVEL_LABEL[n.level] ?? n.level} 알림 · 모든 사용자`}
                    {rounds && n.kind !== "reopen" ? ` · ${n.checkRound ?? 1}회차` : ""}
                    {n.status === "canceled" ? " (취소됨)" : n.status === "pending" ? " (보내는 중)" : ""}</span>
                </div>
              );
            })}
            {detail.notifications.length === 0 && <div><span /><span>보낸 알림이 없어요.</span></div>}
          </div>
        </details>

        <div className="fall-bottom">
          {detail.reviewState === "closed" ? (
            <p className="fall-closed">처리 완료됨 · {detail.closedBy === detail.viewerUserId ? "나" : detail.closedByName} · {when(detail.closedAt ?? detail.updatedAt)}</p>
          ) : robotChecked(detail) ? (
            <p className="fall-closed">처리 완료됨 · 말벗 · {when(robotClosedAt(detail))}</p>
          ) : (
            <button type="button" className="fall-button is-blue is-large"
              disabled={busy === "close" || detail.opinions.length === 0}
              onClick={() => void act("close", `${incidentPath}/close`, post({}), () => "처리 완료했어요.")}>
              {busy === "close" ? "처리 중…" : "처리 완료"}
            </button>
          )}
          {detail.category === "not_person" && (
            <p>알림은 보내지 않았어요. 사람이었다고 생각하면 낙상이나 낙상 의심으로 의견을 남겨 주세요. 사건이 다시 열리고 모두에게 알려요.</p>
          )}
          {detail.category === "off_floor" && (
            <p>묻지도, 알리지도 않았어요. 쓰러졌던 거라고 생각하면 낙상이나 낙상 의심으로 의견을 남겨 주세요. 사건이 다시 열리고 모두에게 알려요.</p>
          )}
          {detail.reviewState === "open" && detail.robotCheck && (
            <p>말벗은 이 사건을 스스로 닫지 않아요. 상태를 확인한 뒤 처리 완료를 눌러 주세요.</p>
          )}
          {detail.reviewState === "open" && !robotChecked(detail) && detail.opinions.length === 0 && (
            <p className="fall-needs-opinion">의견을 먼저 남겨 주세요. 누구든 의견이 하나 있어야 처리 완료할 수 있어요.</p>
          )}
          {!robotChecked(detail) && <p>누구나 누를 수 있어요. 닫힌 뒤 다른 의견이 달리면 다시 열리고 모두에게 알려요.</p>}
          <p>녹화 영상은 7일이 지나면 자동으로 지워져요. 그 뒤에도 사건 기록은 남아요.</p>
        </div>
      </div>
    );
  }

  // List page: "응답 없음", then 오늘 / 어제 / 지난 기록.
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
      <Badges items={incidentBadges(i)} />
      <strong>{incidentTitle(i)}</strong>
      <span className="fall-sub">{incidentSubtitle(i)}</span>
      {/* 아래 줄: 영상 상태 + 덧붙는 것(알림 횟수 · 다시 열림 · AI 판정 실패, 목업 36번). */}
      {(i.sceneState || incidentExtras(i).length > 0) && (
        <span className={`fall-scene-state ${i.sceneState ? SCENE[i.sceneState][1] : "is-off"}`}>
          {i.sceneState && <i />}{[i.sceneState ? SCENE[i.sceneState][0] : null, ...incidentExtras(i)].filter(Boolean).join(" · ")}
        </span>
      )}
      {(i.linkedCount ?? 0) > 0 && <span className="fall-card-link">같은 시간에 다른 사람 사건 {i.linkedCount}건</span>}
    </button>
  );
  return (
    <>
      {header}
      <div className="fall-page">
        <div className="fall-list-tools">
          <div className="fall-chips" role="group" aria-label="사건 필터">
            {FILTERS.map(([key, label]) => (
              <button type="button" key={key} aria-pressed={filter === key} className={filter === key ? "is-on" : ""}
                onClick={() => setFilter(key)}>{label}</button>
            ))}
            {/* 마지막 필터 줄의 오른쪽 끝(남는 자리가 없으면 다음 줄 끝). 모양은 글자 링크라 필터와 구분된다. */}
            {onOpenTimeline && <button type="button" className="fall-link" onClick={() => onOpenTimeline()}>연속 녹화 보기 ›</button>}
          </div>
        </div>
        <div className="fall-list">
          {error && <p className="fall-notice" role="alert">{error}</p>}
          {loading && incidents.length === 0 && <p className="fall-empty" role="status">사건을 불러오는 중이에요…</p>}
          {!loading && !error && incidents.length === 0 && (
            <p className="fall-empty">표시할 사건이 없어요. 낙상이 의심되면 사건이 생기고, 그 장면을 연속 녹화에서 바로 볼 수 있어요.</p>
          )}
          {first.length > 0 && <div className="fall-group">응답 없음</div>}
          {first.map(card)}
          {groups.map(([label, items]) => (
            <div className="fall-group-block" key={label}>
              <div className="fall-group">{label}</div>
              {items.map(card)}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
