"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { demoIncidentFetch } from "./fall-incidents-demo";

// 연속 녹화 screen from the reviewed mockup (Timeline.dc.html): pick the moment
// a fall started on the continuous recording, then either re-review an
// incident with AI or file a missed-fall report (놓친 낙상 신고).

export type TimelineMode =
  | { kind: "report"; momentAt?: string }
  | { kind: "recheck"; incidentId: string; momentAt: string; title: string; rangeStart: string; rangeEnd: string };

type Timeline = {
  from: string; to: string;
  recordings: Array<{ startAt: string; endAt: string }>;
  incidents: Array<{ incidentId: string; at: string; kind: "fall" | "suspected" | "report" }>;
};
type Request = (url: string, init?: RequestInit) => Promise<Response>;

const DAYS = 7;
const PRE_MS = 10_000, POST_MS = 20_000, REVIEW_MS = 5_000;
const WINDOW_MS = 10 * 60_000;
// Zoom bar: 2 min, so the 30 s scene is a quarter of it and the 5 s sent to AI is visible.
const ZOOM_MS = 2 * 60_000;

const pad = (n: number) => String(n).padStart(2, "0");
const hms = (ms: number) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
// Clock reads live outside render; days are counted from the midnight seen at mount.
const nowMs = () => Date.now();
function todayStart() {
  const d = new Date(nowMs());
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}
function dayStart(offset: number, today: number) {
  const d = new Date(today);
  d.setDate(d.getDate() - offset);
  return d.getTime();
}
function dayLabel(offset: number, today: number) {
  const d = new Date(dayStart(offset, today));
  const date = d.toLocaleDateString("ko-KR", { month: "long", day: "numeric" });
  return offset === 0 ? `오늘 (${date})` : offset === 1 ? `어제 (${date})` : date;
}
function dayOffsetOf(ms: number, today: number) {
  for (let offset = 0; offset < DAYS; offset += 1) if (ms >= dayStart(offset, today)) return offset;
  return DAYS - 1;
}
/** "14:20:12" on the shown day; null if not a valid time of day. */
function parseClock(text: string, base: number) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(text.trim());
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59 || Number(m[3] ?? 0) > 59) return null;
  return base + ((Number(m[1]) * 60 + Number(m[2])) * 60 + Number(m[3] ?? 0)) * 1000;
}
const second = (ms: number) => Math.floor(ms / 1000) * 1000;

async function json(response: Response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.error === "string" ? body.error : "요청을 처리하지 못했습니다.");
    (error as Error & { reason?: string }).reason = body.reason;
    throw error;
  }
  return body;
}

export function FallTimelinePanel({ deviceId, mode: initialMode, onBack, onOpenIncident, demo = false }: {
  deviceId: string;
  mode: TimelineMode;
  onBack: () => void;
  onOpenIncident: (incidentId: string) => void;
  demo?: boolean;
}) {
  const base = `/api/devices/${encodeURIComponent(deviceId)}`;
  const request = useCallback<Request>((url, init) =>
    demo ? demoIncidentFetch(url, init) : fetch(url, init), [demo]);
  const [mode, setMode] = useState<TimelineMode>(initialMode);
  const [today] = useState(todayStart);
  const [initial] = useState(() => second(initialMode.momentAt ? Date.parse(initialMode.momentAt) : nowMs() - 60_000));
  const [offset, setOffset] = useState(() => dayOffsetOf(initial, today));
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [moment, setMoment] = useState(initial);
  const [momentText, setMomentText] = useState(hms(initial));
  const [windowStart, setWindowStart] = useState(initial - WINDOW_MS / 2);
  const [zoomStart, setZoomStart] = useState(initial - ZOOM_MS / 2);
  const [videoAt, setVideoAt] = useState<number | null>(null);
  const [videoMessage, setVideoMessage] = useState("");
  const [memo, setMemo] = useState("");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<{ kind: "recheck" | "report"; ai: boolean; text: string; incidentId?: string } | null>(null);
  const [error, setError] = useState("");
  const [outsideServer, setOutsideServer] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const alignedRef = useRef(0);
  const typingRef = useRef(false);

  const day0 = dayStart(offset, today);
  useEffect(() => {
    let active = true;
    const from = new Date(day0).toISOString(), to = new Date(day0 + 86_400_000).toISOString();
    void request(`${base}/fall-timeline?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, { cache: "no-store" })
      .then(json).then((body) => { if (active) setTimeline(body as Timeline); })
      .catch(() => { if (active) setTimeline({ from, to, recordings: [], incidents: [] }); });
    return () => { active = false; };
  }, [base, day0, request]);

  // Recording window around the cursor; video time 0 is the first archived fragment.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const controller = new AbortController();
    let dispose = () => undefined as void;
    const start = Math.max(windowStart, nowMs() - 7 * 86_400_000 + 60_000);
    const end = Math.min(start + WINDOW_MS, nowMs());
    const seekTo = () => {
      const target = (moment - alignedRef.current) / 1000;
      if (Number.isFinite(target) && target >= 0) video.currentTime = target;
      setVideoMessage("");
    };
    // Only a loaded recording has a wall time; an empty player has none.
    const track = () => { if (alignedRef.current > 0 && video.currentSrc) setVideoAt(alignedRef.current + video.currentTime * 1000); };
    // Pausing or scrubbing the video picks the moment, unless the user is typing it.
    const pick = () => {
      if (typingRef.current || !(alignedRef.current > 0 && video.currentSrc)) return;
      const at = second(alignedRef.current + video.currentTime * 1000);
      setMoment(at);
      setMomentText(hms(at));
      setOutsideServer(false);
    };
    video.addEventListener("loadedmetadata", seekTo);
    video.addEventListener("timeupdate", track);
    video.addEventListener("pause", pick);
    video.addEventListener("seeked", pick);
    alignedRef.current = 0;
    window.queueMicrotask(() => {
      setVideoAt(null);
      setVideoMessage(end <= start ? "이 시간의 녹화 영상이 없습니다." : "녹화를 불러오는 중입니다…");
    });
    if (end <= start) return;
    void request(`${base}/recording-playback`, {
      method: "POST", signal: controller.signal, headers: { "content-type": "application/json" },
      body: JSON.stringify({ startAt: new Date(start).toISOString(), endAt: new Date(end).toISOString() }),
    }).then(json).then(async (body) => {
      alignedRef.current = Date.parse(body.alignedStartAt);
      if (video.canPlayType("application/vnd.apple.mpegurl")) { video.src = body.playbackUrl; video.load(); return; }
      const { default: Hls } = await import("hls.js");
      if (!Hls.isSupported()) throw new Error("이 브라우저는 HLS 재생을 지원하지 않습니다.");
      const player = new Hls({ enableWorker: true });
      player.on(Hls.Events.ERROR, (_name, data) => { if (data.fatal) setVideoMessage("녹화를 재생하지 못했습니다."); });
      player.loadSource(body.playbackUrl);
      player.attachMedia(video);
      dispose = () => player.destroy();
    }).catch((reason) => {
      if (!controller.signal.aborted) setVideoMessage(reason instanceof Error ? reason.message : "녹화를 불러오지 못했습니다.");
    });
    return () => {
      controller.abort();
      dispose();
      video.removeEventListener("loadedmetadata", seekTo);
      video.removeEventListener("timeupdate", track);
      video.removeEventListener("pause", pick);
      video.removeEventListener("seeked", pick);
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
    // The window, not every moment change, decides when to reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, request, windowStart]);

  const setCursor = (at: number) => {
    const clamped = second(Math.min(nowMs(), Math.max(dayStart(DAYS - 1, today), at)));
    setMoment(clamped);
    setMomentText(hms(clamped));
    setOutsideServer(false);
    if (clamped < windowStart || clamped > windowStart + WINDOW_MS) setWindowStart(clamped - WINDOW_MS / 2);
    // Keep the zoom still while the moment stays in its middle; re-center otherwise.
    if (clamped < zoomStart + 10_000 || clamped > zoomStart + ZOOM_MS - 25_000) setZoomStart(clamped - ZOOM_MS / 2);
    else if (videoRef.current && alignedRef.current) videoRef.current.currentTime = (clamped - alignedRef.current) / 1000;
  };
  const changeDay = (next: number) => {
    setOffset(next);
    setCursor(Math.min(nowMs(), dayStart(next, today) + 12 * 3600_000));
  };

  const recheck = mode.kind === "recheck" ? mode : null;
  const rangeStart = recheck ? Date.parse(recheck.rangeStart) : moment - PRE_MS;
  const rangeEnd = recheck ? Date.parse(recheck.rangeEnd) : moment + POST_MS;
  const outside = recheck !== null && (moment < rangeStart || moment > rangeEnd || outsideServer);
  const percent = (ms: number) => Math.min(100, Math.max(0, ((ms - day0) / 86_400_000) * 100));
  const zoom = (ms: number) => Math.min(100, Math.max(0, ((ms - zoomStart) / ZOOM_MS) * 100));

  const submitRecheck = async () => {
    if (!recheck) return;
    setBusy("recheck");
    setError("");
    try {
      await json(await request(`${base}/fall-incidents/${encodeURIComponent(recheck.incidentId)}/ai-reviews`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ momentAt: new Date(moment).toISOString() }),
      }));
      setResult({ kind: "recheck", ai: true, text: "검토 진행 중 · 결과는 원래 사건에 기록돼요", incidentId: recheck.incidentId });
    } catch (reason) {
      if ((reason as Error & { reason?: string }).reason === "outside_incident") setOutsideServer(true);
      else setError(reason instanceof Error ? reason.message : "AI 검토를 요청하지 못했습니다.");
    } finally { setBusy(""); }
  };
  const submitReport = async (withAi: boolean) => {
    setBusy(withAi ? "report-ai" : "report");
    setError("");
    try {
      const body = await json(await request(`${base}/fall-reports`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ momentAt: new Date(moment).toISOString(), memo: memo.trim() || null,
          ...(withAi ? { requestAiReview: true } : {}) }),
      }));
      const aiError = body.aiReview?.error as string | undefined;
      setResult({ kind: "report", ai: withAi && !aiError, incidentId: body.incidentId,
        text: "놓친 낙상 신고로 사건 목록에 추가됐어요. 다른 사용자에게 알림은 가지 않아요." +
          (aiError === "consent_off" ? " 클라우드 분석 동의가 꺼져 있어 AI 검토는 보내지 않았어요."
            : aiError === "key_missing" ? " 클라우드 AI 키가 없어 AI 검토는 보내지 않았어요."
              : aiError ? " AI 검토는 시작하지 못했어요." : "") });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "신고하지 못했습니다.");
    } finally { setBusy(""); }
  };

  const momentField = (label: string) => (
    <label className="fall-tl-field">{label}
      <input type="text" inputMode="numeric" value={momentText}
        onFocus={() => { typingRef.current = true; }}
        onBlur={() => { typingRef.current = false; }}
        onChange={(e) => {
          setMomentText(e.target.value);
          const at = parseClock(e.target.value, day0);
          if (at !== null && at <= nowMs()) setCursor(at);
        }} />
    </label>
  );

  return (
    <div className="fall-page">
      <div className="fall-topbar fall-tl-top">
        <button type="button" className="fall-link" onClick={onBack}>‹ 사건 목록</button>
        <h1>연속 녹화</h1>
        <span />
      </div>
      <div className="fall-tl-body">
        <div className="fall-scene">
          <video ref={videoRef} controls playsInline />
          {videoMessage && <span className="fall-scene-hint" role="status">{videoMessage}</span>}
          <span className="fall-tl-clock">{dayLabel(dayOffsetOf(videoAt ?? moment, today), today).replace(/ \(.*\)/, "")} {hms(videoAt ?? moment)}</span>
        </div>

        <div className="fall-tl-days">
          <button type="button" disabled={offset >= DAYS - 1} onClick={() => changeDay(offset + 1)}>‹ 이전 날</button>
          <span>{dayLabel(offset, today)}</span>
          <button type="button" disabled={offset === 0} onClick={() => changeDay(offset - 1)}>다음 날 ›</button>
        </div>
        <p className="fall-hint is-center">최근 7일까지 볼 수 있어요</p>

        <div className="fall-card is-flat">
          <div className="fall-sub">24시간 타임라인 · 표시는 사건 위치</div>
          <div className="fall-tl-track" role="slider" tabIndex={0} aria-label="녹화 시각 고르기"
            aria-valuemin={0} aria-valuemax={1440} aria-valuenow={Math.round((moment - day0) / 60_000)}
            aria-valuetext={hms(moment)}
            onClick={(e) => {
              const box = e.currentTarget.getBoundingClientRect();
              setCursor(day0 + ((e.clientX - box.left) / box.width) * 86_400_000);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft") setCursor(moment - 60_000);
              if (e.key === "ArrowRight") setCursor(moment + 60_000);
            }}>
            {/* Recorded spans sit inside the rounded bar: only its two ends are round, back-to-back recordings join flat. */}
            <div className="fall-tl-gap">
              {timeline?.recordings.map((r) => (
                <div key={r.startAt} className="fall-tl-rec"
                  style={{ left: `${percent(Date.parse(r.startAt))}%`, width: `${Math.max(0.2, percent(Date.parse(r.endAt)) - percent(Date.parse(r.startAt)))}%` }} />
              ))}
            </div>
            {timeline?.incidents.map((i) => (
              <div key={i.incidentId} className={`fall-tl-mark is-${i.kind}`} style={{ left: `${percent(Date.parse(i.at))}%` }}
                title={hms(Date.parse(i.at))} />
            ))}
          </div>
          {/* The picked moment, below the bar so it never hides the selected range. */}
          <div className="fall-tl-pointer-row" aria-hidden="true">
            <span className="fall-tl-pointer" style={{ left: `${percent(moment)}%` }}>▲</span>
          </div>
          <div className="fall-tl-ticks"><span>00시</span><span>06시</span><span>12시</span><span>18시</span><span>24시</span></div>
          <div className="fall-sub fall-tl-zoom-title">고른 순간 주변 2분</div>
          <div className="fall-tl-track is-zoom" role="slider" tabIndex={0} aria-label="고른 순간 주변 2분에서 순간 고르기"
            aria-valuemin={0} aria-valuemax={120} aria-valuenow={Math.round((moment - zoomStart) / 1000)}
            aria-valuetext={hms(moment)}
            onClick={(e) => {
              const box = e.currentTarget.getBoundingClientRect();
              setCursor(zoomStart + ((e.clientX - box.left) / box.width) * ZOOM_MS);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft") setCursor(moment - 1000);
              if (e.key === "ArrowRight") setCursor(moment + 1000);
            }}>
            <div className="fall-tl-gap">
              {timeline?.recordings.map((r) => {
                const a = zoom(Date.parse(r.startAt)), b = zoom(Date.parse(r.endAt));
                return b > a ? <div key={r.startAt} className="fall-tl-rec" style={{ left: `${a}%`, width: `${b - a}%` }} /> : null;
              })}
            </div>
            {Array.from({ length: 11 }, (_, index) => (
              <div key={index} className={`fall-tl-tick ${index === 5 ? "is-major" : ""}`} style={{ left: `${(index + 1) * (100 / 12)}%` }} />
            ))}
            {timeline?.incidents.filter((i) => Date.parse(i.at) >= zoomStart && Date.parse(i.at) <= zoomStart + ZOOM_MS)
              .map((i) => <div key={i.incidentId} className={`fall-tl-mark is-${i.kind}`} style={{ left: `${zoom(Date.parse(i.at))}%` }} />)}
            {rangeEnd > zoomStart && rangeStart < zoomStart + ZOOM_MS && (
              <div className="fall-tl-range" style={{ left: `${zoom(rangeStart)}%`, width: `${zoom(rangeEnd) - zoom(rangeStart)}%` }} />
            )}
            {/* What AI receives: 12 photos from the moment to +5 s. */}
            <div className="fall-tl-ai" style={{ left: `${zoom(moment)}%`, width: `${zoom(moment + REVIEW_MS) - zoom(moment)}%` }} />
          </div>
          <div className="fall-tl-pointer-row" aria-hidden="true">
            <span className="fall-tl-pointer" style={{ left: `${zoom(moment)}%` }}>▲</span>
          </div>
          <div className="fall-tl-ticks">
            {[0, 60_000, ZOOM_MS].map((offset) => <span key={offset}>{hms(zoomStart + offset)}</span>)}
          </div>
          <div className="fall-tl-legend">
            <span><i className="is-fall" />낙상</span>
            <span><i className="is-suspected" />낙상 의심</span>
            <span><i className="is-gap" />녹화 없음</span>
            <span><i className="is-range" />선택한 구간 (30초)</span>
            <span><i className="is-ai" />AI에게 보내는 5초</span>
          </div>
        </div>

        {error && <p className="fall-notice" role="alert">{error}</p>}

        {recheck && (
          <div className="fall-card is-flat is-blue">
            <h2>사건 다시 검토</h2>
            <div className="fall-sub">{recheck.title} 사건이에요. 넘어진 순간을 확인하고 필요하면 고쳐 주세요.</div>
            {momentField("넘어지기 시작한 순간 (사건 의심 시점으로 채워짐)")}
            <div className="fall-tl-box">원래 사건 구간: {hms(rangeStart)} ~ {hms(rangeEnd)} · 이 안에서 고치면 결과가 원래 사건에 붙어요</div>
            {outside && (
              <div className="fall-tl-warn">
                <div>이 시간은 원래 사건 밖이에요. 놓친 낙상으로 새로 신고할까요?</div>
                <div className="fall-two-buttons">
                  <button type="button" className="fall-button is-soft" onClick={() => setCursor(Date.parse(recheck.momentAt))}>순간 다시 고르기</button>
                  <button type="button" className="fall-button is-purple"
                    onClick={() => { setMode({ kind: "report", momentAt: new Date(moment).toISOString() }); setResult(null); }}>새로 신고하기</button>
                </div>
              </div>
            )}
            <div className="fall-hint">찍은 순간부터 5초 후까지에서 같은 간격으로 12장을 사진만으로 보내 판정받아요.</div>
            {result?.kind === "recheck" ? (
              <>
                <div className="fall-tl-progress"><i />{result.text}</div>
                <button type="button" className="fall-link is-left" onClick={() => onOpenIncident(recheck.incidentId)}>사건으로 돌아가기 ›</button>
              </>
            ) : (
              <button type="button" className="fall-button is-blue is-large" disabled={outside || busy === "recheck"}
                onClick={() => void submitRecheck()}>{busy === "recheck" ? "보내는 중…" : "AI에게 다시 검토 받기"}</button>
            )}
          </div>
        )}

        {!recheck && !result && (
          <div className="fall-card is-flat is-purple">
            <h2>놓친 낙상 신고</h2>
            <div className="fall-sub">자동으로 감지되지 않은 낙상을 남겨요. 영상을 움직여 넘어지기 시작한 순간을 찍어 주세요.</div>
            {momentField("넘어지기 시작한 순간")}
            <div className="fall-tl-box">장면 영상: {hms(moment - PRE_MS)} ~ {hms(moment + POST_MS)} (순간 10초 전 ~ 20초 후)</div>
            <label className="fall-tl-field">메모 (선택)
              <input type="text" maxLength={500} placeholder="무엇을 봤는지 적어 주세요" value={memo} onChange={(e) => setMemo(e.target.value)} />
            </label>
            <div className="fall-tl-actions">
              <button type="button" className="fall-button is-purple-line is-large" disabled={Boolean(busy)}
                onClick={() => void submitReport(false)}>{busy === "report" ? "남기는 중…" : "신고만 남기기"}</button>
              <button type="button" className="fall-button is-purple is-large" disabled={Boolean(busy)}
                onClick={() => void submitReport(true)}>{busy === "report-ai" ? "보내는 중…" : "신고하고 AI에게 검토 받기"}</button>
              <div className="fall-hint">AI 검토를 고르면 찍은 순간부터 5초 후까지({hms(moment)} ~ {hms(moment + REVIEW_MS)})에서 같은 간격으로 12장을 사진만으로 보내 판정받아요. 결과는 기록만 되고 알림은 가지 않아요.</div>
            </div>
            <div className="fall-hint">다른 사용자에게 알림은 가지 않고, 사건 목록에 &quot;사용자 신고&quot;로 기록돼요.</div>
          </div>
        )}

        {!recheck && result?.kind === "report" && (
          <div className="fall-card is-flat is-purple">
            <h2>신고를 남겼어요</h2>
            <div className="fall-sub">{result.text}</div>
            {result.ai && <div className="fall-tl-progress"><i />AI 검토 진행 중 · 결과가 나올 때까지 다시 요청할 수 없어요</div>}
            <div className="fall-two-buttons">
              <button type="button" className="fall-button is-soft" onClick={onBack}>사건 목록 보기</button>
              <button type="button" className="fall-button is-soft" onClick={() => { setResult(null); setMemo(""); }}>다른 구간 신고</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
