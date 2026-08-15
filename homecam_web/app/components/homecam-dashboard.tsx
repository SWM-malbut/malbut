"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Bell,
  Camera,
  CaretRight,
  Cat,
  CheckCircle,
  ClockCounterClockwise,
  CornersOut,
  Dog,
  Info,
  Moon,
  Person,
  Play,
  ShieldCheck,
  Sun,
  Trash,
  UsersThree,
  VideoCamera,
  Waveform,
  X,
} from "@phosphor-icons/react";
import {
  HomecamHeader,
  type HomecamTab,
} from "./homecam-header";
import {
  RobotMapPanel,
  RobotMapSummaryOverlay,
  type MapMode,
  type RobotSemantics,
  type RobotSnapshot,
} from "./robot-map-panel";

export type { HomecamTab } from "./homecam-header";

export type HomecamDevice = {
  id: string;
  displayName: string;
  online: boolean;
  lastSeenAt: string | null;
  role: "owner" | "family" | "unknown";
  monitoringEnabled: boolean;
  cameraEnabled: boolean;
  microphoneEnabled: boolean;
  mediaHealthy: boolean;
  detectorHealthy: boolean | null;
  activeSession: {
    roomCode: string;
    storageMode: boolean;
  } | null;
};

type HomecamEventType = "motion" | "person" | "dog" | "cat";
type HomecamEventFilter = HomecamEventType | "all" | "pet";
type EventCursor = { occurredAt: string; id: string };

type HomecamEvent = {
  id: string;
  type: HomecamEventType;
  confidence: number | null;
  occurredAt: string;
  recordingId: string | null;
  recordingSegment: number;
  playbackOffsetSeconds: number;
  recordingStartedAt: string | null;
  eventGroupId: string | null;
  segmentIndex: number | null;
  labels: HomecamEventType[];
  clipStartAt: string | null;
  clipEndAt: string | null;
  clipState: "detected" | "recording" | "ready" | "incomplete" | "unavailable" | "expired";
  monotonicDurationMs: number | null;
  ai: {
    status: string;
    summary: string | null;
  };
};

type FamilyMember = {
  id: string;
  email: string;
  role: "owner" | "family";
};

type ApiAvailability = "loading" | "ready" | "unavailable";
type HomecamColorMode = "dark" | "light";

type HomecamDashboardProps = {
  initialTab?: HomecamTab;
  onOpenLive: (device: HomecamDevice) => Promise<void>;
  onCreateLegacyBroadcast: () => Promise<void>;
  onJoinLegacy: (roomCode: string, password: string) => void;
  creatingLegacyBroadcast: boolean;
  externalError?: string;
  legacyArchive?: React.ReactNode;
  liveViewer?: (context: {
    eventCount: number;
    openEvents: () => void;
  }) => React.ReactNode;
};

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

const EVENT_LABELS: Record<HomecamEventType, string> = {
  motion: "움직임",
  person: "사람",
  dog: "강아지",
  cat: "고양이",
};

function HomeMapSummary({
  device,
  onOpenMap,
}: {
  device: HomecamDevice | null;
  onOpenMap: (mode: MapMode) => void;
}) {
  const deviceId = device?.id ?? "";
  const [robotSnapshot, setRobotSnapshot] = useState<RobotSnapshot | null>(null);
  const [semantics, setSemantics] = useState<RobotSemantics | null>(null);
  const [rooms, setRooms] = useState<Array<{ id: string; name: string; color: string }>>([]);
  const revision = robotSnapshot?.map?.revision ?? "";

  useEffect(() => {
    if (!deviceId) {
      const timer = window.setTimeout(() => {
        setRobotSnapshot(null);
      }, 0);
      return () => window.clearTimeout(timer);
    }
    const controller = new AbortController();
    const loadRobot = async () => {
      const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({})) as RobotSnapshot;
      if (response.ok && !controller.signal.aborted) setRobotSnapshot(payload);
    };
    void loadRobot().catch(() => undefined);
    const timer = window.setInterval(() => void loadRobot().catch(() => undefined), 1_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [deviceId]);

  useEffect(() => {
    if (!deviceId || !revision) {
      const timer = window.setTimeout(() => {
        setSemantics(null);
        setRooms([]);
      }, 0);
      return () => window.clearTimeout(timer);
    }
    const controller = new AbortController();
    void fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/semantic`, {
      cache: "no-store",
      signal: controller.signal,
    }).then(async (response) => {
      const semantic = await response.json().catch(() => ({})) as RobotSemantics;
      if (!response.ok || controller.signal.aborted) return;
      setSemantics(semantic);
      const userMap = asRecord(semantic.userMap);
      const features = Array.isArray(userMap.features) ? userMap.features : [];
      setRooms(features.flatMap((value, index) => {
        const feature = asRecord(value);
        const properties = asRecord(feature.properties);
        if (properties.role !== "room") return [];
        return [{
          id: stringValue(feature.id) ?? stringValue(properties.room_id) ?? `room-${index}`,
          name: stringValue(properties.name) ?? `공간 ${index + 1}`,
          color: stringValue(properties.color) ?? ["#E7EBE3", "#E3E7EE", "#EFE7DE", "#DDE9E8"][index % 4],
        }];
      }));
    }).catch(() => undefined);
    return () => controller.abort();
  }, [deviceId, revision]);

  return (
    <>
      <article className="homecam-home-map-card">
        <h2>우리 집 지도</h2>
        <button type="button" className="homecam-home-map-preview" onClick={() => onOpenMap("view")}>
          {device && revision ? (
            <>
              <Image
                src={`/api/devices/${encodeURIComponent(device.id)}/robot/map?revision=${encodeURIComponent(revision)}`}
                alt="저장된 우리 집 지도"
                fill
                unoptimized
                sizes="376px"
              />
              <RobotMapSummaryOverlay snapshot={robotSnapshot} semantics={semantics} />
            </>
          ) : (
            <span>저장된 지도를 확인하고 있어요</span>
          )}
        </button>
        <div className="homecam-home-map-actions">
          <button type="button" onClick={() => onOpenMap("navigate")}>목적지 선택</button>
          <button type="button" onClick={() => onOpenMap("view")}>지도 열기</button>
        </div>
      </article>
      <article className="homecam-home-favorites">
        <h2>자주 보내는 곳</h2>
        <div>
          {rooms.length === 0 && <p>방을 나누고 이름을 정하면 여기에 표시됩니다.</p>}
          {rooms.slice(0, 4).map((room) => (
            <button type="button" key={room.id} onClick={() => onOpenMap("navigate")}>
              <i style={{ background: room.color }} />
              <strong>{room.name}</strong>
              <span>지도에서 선택</span>
            </button>
          ))}
        </div>
      </article>
    </>
  );
}

const LEGACY_PASSWORD_LENGTH = 16;

function EventKindIcon({
  type,
  size = 20,
}: {
  type: HomecamEventType;
  size?: number;
}) {
  if (type === "person") return <Person size={size} weight="regular" />;
  if (type === "dog") return <Dog size={size} weight="regular" />;
  if (type === "cat") return <Cat size={size} weight="regular" />;
  return <Waveform size={size} weight="regular" />;
}

function eventFilterLabel(filter: "all" | "person" | "pet" | "motion") {
  if (filter === "all") return "전체";
  if (filter === "pet") return "반려동물";
  return EVENT_LABELS[filter];
}

function eventBadgeLabel(type: HomecamEventType) {
  return type === "dog" || type === "cat" ? "반려동물" : EVENT_LABELS[type];
}

function eventTitle(event: HomecamEvent) {
  if (event.type === "person") return "사람이 감지됐어요";
  if (event.type === "dog" || event.type === "cat") return "반려동물 움직임이 감지됐어요";
  return "작은 움직임이 감지됐어요";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function stringValue(...values: unknown[]) {
  return values.find((value): value is string => typeof value === "string" && value.length > 0);
}

function booleanValue(fallback: boolean, ...values: unknown[]) {
  const value = values.find((candidate) => typeof candidate === "boolean");
  return typeof value === "boolean" ? value : fallback;
}

function normalizeDevice(value: unknown): HomecamDevice | null {
  const raw = asRecord(value);
  const state = asRecord(raw.state ?? raw.status);
  const settings = asRecord(raw.settings);
  const session = asRecord(raw.activeSession ?? raw.active_session ?? raw.session);
  const id = stringValue(raw.id, raw.deviceId, raw.device_id);
  if (!id) return null;

  const roleValue = stringValue(raw.role, raw.membershipRole, raw.membership_role);
  const role = roleValue === "owner" || roleValue === "family" ? roleValue : "unknown";
  const roomCode = stringValue(session.roomCode, session.room_code);

  return {
    id,
    displayName: stringValue(raw.displayName, raw.display_name, raw.name) ?? "우리 집 홈캠",
    online: booleanValue(false, raw.online, state.online),
    lastSeenAt: stringValue(raw.lastSeenAt, raw.last_seen_at, state.lastSeenAt, state.last_seen_at) ?? null,
    role,
    monitoringEnabled: booleanValue(
      false,
      settings.monitoringEnabled,
      settings.monitoring_enabled,
      raw.monitoringEnabled,
      raw.monitoring_enabled,
      state.monitoringEnabled,
    ),
    cameraEnabled: booleanValue(
      true,
      settings.cameraEnabled,
      settings.camera_enabled,
      raw.cameraEnabled,
      raw.camera_enabled,
      state.cameraEnabled,
    ),
    microphoneEnabled: booleanValue(
      true,
      settings.microphoneEnabled,
      settings.microphone_enabled,
      raw.microphoneEnabled,
      raw.microphone_enabled,
      state.microphoneEnabled,
    ),
    mediaHealthy: booleanValue(
      false,
      state.mediaHealthy,
      state.media_healthy,
      raw.mediaHealthy,
      raw.media_healthy,
    ),
    detectorHealthy:
      typeof state.detectorHealthy === "boolean"
        ? state.detectorHealthy
        : typeof state.detector_healthy === "boolean"
          ? state.detector_healthy
          : null,
    activeSession: roomCode
      ? {
          roomCode,
          storageMode: booleanValue(false, session.storageMode, session.storage_mode),
        }
      : null,
  };
}

function normalizeEvent(value: unknown): HomecamEvent | null {
  const raw = asRecord(value);
  const recording = asRecord(raw.recording);
  const rawType = stringValue(raw.type, raw.eventType, raw.event_type);
  if (!rawType || !["motion", "person", "dog", "cat"].includes(rawType)) return null;
  const occurredAt = stringValue(raw.occurredAt, raw.occurred_at, raw.createdAt, raw.created_at);
  if (!occurredAt) return null;
  const confidenceValue = raw.confidence;
  const segmentValue = raw.recordingSegment ?? raw.recording_segment ?? recording.segment;
  const offsetValue =
    raw.playbackOffsetSeconds ?? raw.playback_offset_seconds ?? recording.offsetSeconds;
  const offsetMsValue =
    raw.recordingOffsetMs ?? raw.recording_offset_ms ?? recording.offsetMs;
  const totalOffsetSeconds =
    typeof offsetValue === "number" && Number.isFinite(offsetValue)
      ? Math.max(0, offsetValue)
      : typeof offsetMsValue === "number" && Number.isFinite(offsetMsValue)
        ? Math.max(0, offsetMsValue / 1_000)
        : 0;
  const labels = Array.isArray(raw.labels)
    ? raw.labels.filter(
        (label): label is HomecamEventType =>
          typeof label === "string" && ["motion", "person", "dog", "cat"].includes(label),
      )
    : [];
  const clipStateValue = stringValue(raw.clipState, raw.clip_state) ?? "detected";
  const clipState = [
    "detected",
    "recording",
    "ready",
    "incomplete",
    "unavailable",
    "expired",
  ].includes(clipStateValue)
    ? clipStateValue as HomecamEvent["clipState"]
    : "detected";
  const ai = asRecord(raw.ai);

  return {
    id: stringValue(raw.id, raw.eventId, raw.event_id) ?? `${rawType}:${occurredAt}`,
    type: rawType as HomecamEventType,
    confidence:
      typeof confidenceValue === "number" && Number.isFinite(confidenceValue)
        ? confidenceValue
        : null,
    occurredAt,
    recordingId:
      stringValue(raw.recordingId, raw.recording_id, recording.id) ?? null,
    recordingSegment:
      typeof segmentValue === "number" && Number.isFinite(segmentValue)
        ? Math.max(0, Math.floor(segmentValue))
        : Math.floor(totalOffsetSeconds / 3_600),
    playbackOffsetSeconds: totalOffsetSeconds % 3_600,
    recordingStartedAt:
      stringValue(raw.recordingStartedAt, raw.recording_started_at, recording.startedAt) ?? null,
    eventGroupId: stringValue(raw.eventGroupId, raw.event_group_id) ?? null,
    segmentIndex:
      typeof raw.segmentIndex === "number" && Number.isSafeInteger(raw.segmentIndex)
        ? raw.segmentIndex
        : null,
    labels,
    clipStartAt: stringValue(raw.clipStartAt, raw.clip_start_at) ?? null,
    clipEndAt: stringValue(raw.clipEndAt, raw.clip_end_at) ?? null,
    clipState,
    monotonicDurationMs:
      typeof raw.monotonicDurationMs === "number" && Number.isFinite(raw.monotonicDurationMs)
        ? raw.monotonicDurationMs
        : null,
    ai: {
      status: stringValue(ai.status) ?? "not_requested",
      summary: stringValue(ai.summary) ?? null,
    },
  };
}

function eventClipStatus(event: HomecamEvent) {
  if (!event.eventGroupId) return event.recordingId ? "지난 영상에서 재생" : "감지 기록만 있음";
  if (event.clipState === "recording") return "클립 저장 중";
  if (event.clipState === "ready") return "이벤트 클립 저장됨";
  if (event.clipState === "incomplete") return "종료 정보가 없는 클립";
  if (event.clipState === "unavailable") return "영상 없음";
  if (event.clipState === "expired") return "보관 기간 만료";
  return "감지됨";
}

function eventCanPlay(event: HomecamEvent) {
  return Boolean(
    event.recordingId &&
      (!event.eventGroupId ||
        (event.clipState === "ready" && event.clipStartAt && event.clipEndAt)),
  );
}

function formatEventTime(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatLiveClock(value: number) {
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function normalizeLegacyCode(value: string) {
  return value.replace(/[^A-HJ-NP-Z2-9]/gi, "").slice(0, 6).toUpperCase();
}

function normalizeLegacyPassword(value: string) {
  const raw = value.replace(/[^A-HJ-NP-Z2-9]/gi, "").slice(0, LEGACY_PASSWORD_LENGTH).toUpperCase();
  return raw.match(/.{1,4}/g)?.join("-") ?? raw;
}

function Switch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      className={`homecam-switch ${checked ? "is-on" : ""}`}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}

function EventPlayback({
  event,
  deviceId,
  onClose,
}: {
  event: HomecamEvent;
  deviceId: string;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const video = videoRef.current;
    const controller = new AbortController();
    let dispose: () => void = () => undefined;
    let seekAdjustmentSeconds = 0;
    let clipDurationSeconds: number | null = null;
    const isBoundedClip = Boolean(event.eventGroupId && event.clipState === "ready");
    if (!video || !event.recordingId) {
      setState("error");
      setMessage("이 이벤트는 연결된 녹화 구간이 아직 없습니다.");
      return () => controller.abort();
    }

    const seekToEvent = () => {
      if (isBoundedClip) {
        video.currentTime = seekAdjustmentSeconds;
        return;
      }
      const explicitOffset = event.playbackOffsetSeconds;
      const calculatedOffset =
        event.recordingStartedAt
          ? Math.max(0, (Date.parse(event.occurredAt) - Date.parse(event.recordingStartedAt)) / 1_000)
          : 0;
      const rawTarget =
        explicitOffset !== null && Number.isFinite(explicitOffset)
          ? explicitOffset
          : Number.isFinite(calculatedOffset)
            ? calculatedOffset
            : 0;
      const target = Math.max(0, rawTarget - seekAdjustmentSeconds);
      if (target > 0 && Number.isFinite(video.duration)) {
        video.currentTime = Math.min(target, Math.max(0, video.duration - 0.25));
      } else if (target > 0) {
        video.currentTime = target;
      }
    };

    const handleReady = () => {
      seekToEvent();
      setState("ready");
      void video.play().catch(() => undefined);
    };
    const handleError = () => {
      setState("error");
      setMessage("이벤트 영상을 불러오지 못했습니다.");
    };
    const handleTimeUpdate = () => {
      if (
        clipDurationSeconds !== null &&
        video.currentTime >= seekAdjustmentSeconds + clipDurationSeconds
      ) {
        video.pause();
      }
    };
    video.addEventListener("loadedmetadata", handleReady);
    video.addEventListener("error", handleError);
    video.addEventListener("timeupdate", handleTimeUpdate);

    const playbackEndpoint = isBoundedClip
      ? `/api/devices/${encodeURIComponent(deviceId)}/events/${encodeURIComponent(event.id)}/playback`
      : `/api/recordings/${encodeURIComponent(event.recordingId)}/playback`;
    void fetch(playbackEndpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: isBoundedClip ? JSON.stringify({}) : JSON.stringify({ segment: event.recordingSegment }),
      signal: controller.signal,
    })
      .then(async (response) => {
        const payload = asRecord(await response.json().catch(() => ({})));
        const playbackUrl = stringValue(payload.playbackUrl, payload.playback_url);
        if (!response.ok || !playbackUrl) {
          throw new Error(stringValue(payload.error) ?? "재생 주소를 발급하지 못했습니다.");
        }
        const adjustment = payload.seekAdjustmentSeconds;
        seekAdjustmentSeconds =
          typeof adjustment === "number" &&
          Number.isFinite(adjustment) &&
          adjustment >= 0
            ? adjustment
            : 0;
        const duration = payload.durationSeconds;
        clipDurationSeconds =
          typeof duration === "number" && Number.isFinite(duration) && duration > 0
            ? duration
            : null;
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = playbackUrl;
          video.load();
          return;
        }
        return import("hls.js").then(({ default: Hls }) => {
          if (!Hls.isSupported()) throw new Error("이 브라우저는 HLS 재생을 지원하지 않습니다.");
          const player = new Hls({ enableWorker: true });
          player.on(Hls.Events.MANIFEST_PARSED, handleReady);
          player.on(Hls.Events.ERROR, (_name, data) => {
            if (data.fatal) handleError();
          });
          player.loadSource(playbackUrl);
          player.attachMedia(video);
          dispose = () => player.destroy();
        });
      })
      .catch((reason) => {
        if (controller.signal.aborted) return;
        setState("error");
        setMessage(reason instanceof Error ? reason.message : "이벤트 영상을 불러오지 못했습니다.");
      });

    return () => {
      controller.abort();
      dispose();
      video.removeEventListener("loadedmetadata", handleReady);
      video.removeEventListener("error", handleError);
      video.removeEventListener("timeupdate", handleTimeUpdate);
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [deviceId, event]);

  return (
    <div className="event-playback" role="dialog" aria-modal="true" aria-label="이벤트 영상 재생">
      <button className="event-playback-backdrop" aria-label="닫기" onClick={onClose} />
      <section className="event-playback-sheet">
        <div className="event-playback-heading">
          <div>
            <span className={`event-kind kind-${event.type}`}>
              <EventKindIcon type={event.type} size={14} />
              {EVENT_LABELS[event.type]}
            </span>
            <strong>{formatEventTime(event.occurredAt)}</strong>
          </div>
          <button type="button" className="homecam-icon-button" onClick={onClose} aria-label="이벤트 영상 닫기">
            <X size={18} weight="regular" />
          </button>
        </div>
        <video ref={videoRef} controls playsInline />
        {state === "loading" && <p role="status">해당 시각의 녹화를 불러오는 중입니다…</p>}
        {state === "error" && <p className="homecam-inline-error" role="alert">{message}</p>}
        {state === "ready" && <p>{event.eventGroupId ? "감지 전 5초부터 이벤트 종료까지 재생합니다." : "이벤트가 감지된 시각으로 이동했습니다."}</p>}
      </section>
    </div>
  );
}

export function HomecamDashboard({
  initialTab = "home",
  onOpenLive,
  onCreateLegacyBroadcast,
  onJoinLegacy,
  creatingLegacyBroadcast,
  externalError = "",
  legacyArchive,
  liveViewer,
}: HomecamDashboardProps) {
  const [devices, setDevices] = useState<HomecamDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [tab, setTab] = useState<HomecamTab>(initialTab);
  const [mapEntryMode, setMapEntryMode] = useState<MapMode>("view");
  const [availability, setAvailability] = useState<ApiAvailability>("loading");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [events, setEvents] = useState<HomecamEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventCursor, setEventCursor] = useState<EventCursor | null>(null);
  const [eventFilter, setEventFilter] = useState<HomecamEventFilter>("all");
  const [selectedEvent, setSelectedEvent] = useState<HomecamEvent | null>(null);
  const [focusedEventId, setFocusedEventId] = useState("");
  const [family, setFamily] = useState<FamilyMember[]>([]);
  const [familyLoading, setFamilyLoading] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [pushEnabled, setPushEnabled] = useState(false);
  const [pushSubscriptionId, setPushSubscriptionId] = useState("");
  const [pushEndpointRegistrationCount, setPushEndpointRegistrationCount] = useState(0);
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [standalone, setStandalone] = useState(false);
  const [legacyOpen, setLegacyOpen] = useState(false);
  const [legacyCode, setLegacyCode] = useState("");
  const [legacyPassword, setLegacyPassword] = useState("");
  const [colorMode, setColorMode] = useState<HomecamColorMode>("dark");
  const [liveClockMs, setLiveClockMs] = useState(() => Date.now());
  const deepLinkedEventIdRef = useRef("");
  const openMap = useCallback((mode: MapMode) => {
    setMapEntryMode(mode);
    setTab("map");
  }, []);

  useEffect(() => {
    const stored = window.localStorage.getItem("malbut-color-mode");
    const preferred = stored === "dark" || stored === "light"
      ? stored
      : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    window.queueMicrotask(() => setColorMode(preferred));
  }, []);

  useEffect(() => {
    if (tab !== "live") return;
    const timer = window.setInterval(() => setLiveClockMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [tab]);

  const selectedDevice = useMemo(
    () => devices.find((device) => device.id === selectedDeviceId) ?? devices[0] ?? null,
    [devices, selectedDeviceId],
  );
  const visibleEvents = useMemo(() => events.filter((event) =>
    eventFilter === "all" || event.type === eventFilter ||
      (eventFilter === "pet" && (event.type === "dog" || event.type === "cat"))), [eventFilter, events]);
  const focusedEvent = visibleEvents.find((event) => event.id === focusedEventId) ?? visibleEvents[0] ?? null;
  const eventCounts = useMemo(() => ({
    all: events.length,
    motion: events.filter((event) => event.type === "motion").length,
    person: events.filter((event) => event.type === "person").length,
    pet: events.filter((event) => event.type === "dog" || event.type === "cat").length,
  }), [events]);

  const loadDevices = useCallback(async (quiet = false) => {
    if (!quiet) setAvailability("loading");
    try {
      const response = await fetch("/api/devices", { cache: "no-store" });
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "등록된 기기를 불러오지 못했습니다.");
      const rawDevices = Array.isArray(payload.devices)
        ? payload.devices
        : Array.isArray(payload.items)
          ? payload.items
          : [];
      const nextDevices = rawDevices
        .map(normalizeDevice)
        .filter((device): device is HomecamDevice => device !== null);
      setDevices(nextDevices);
      setSelectedDeviceId((current) =>
        nextDevices.some((device) => device.id === current) ? current : nextDevices[0]?.id ?? "",
      );
      setAvailability("ready");
      if (!quiet) setNotice("");
    } catch (reason) {
      if (!quiet) {
        setAvailability("unavailable");
        setNotice(
          reason instanceof Error
            ? reason.message
            : "홈캠 기기 API가 아직 연결되지 않았습니다.",
        );
      }
    }
  }, []);

  useEffect(() => {
    window.queueMicrotask(() => void loadDevices());
    const interval = window.setInterval(() => void loadDevices(true), 15_000);
    return () => window.clearInterval(interval);
  }, [loadDevices]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedView = params.get("view");
    const requestedDevice = params.get("device")?.trim() ?? "";
    const requestedEvent = params.get("event")?.trim() ?? "";
    if (requestedEvent) deepLinkedEventIdRef.current = requestedEvent;
    window.queueMicrotask(() => {
      if (requestedDevice) setSelectedDeviceId(requestedDevice);
      if (requestedView === "events" || requestedEvent) setTab("events");
      if (requestedView === "map") setTab("map");
      if (requestedView === "settings") setTab("settings");
    });
  }, []);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(display-mode: standalone)");
    const updateStandalone = () =>
      setStandalone(mediaQuery.matches || ("standalone" in navigator && Boolean((navigator as Navigator & { standalone?: boolean }).standalone)));
    updateStandalone();
    mediaQuery.addEventListener("change", updateStandalone);
    const captureInstallPrompt = (event: Event) => {
      event.preventDefault();
      setInstallPrompt(event as BeforeInstallPromptEvent);
    };
    window.addEventListener("beforeinstallprompt", captureInstallPrompt);
    return () => {
      mediaQuery.removeEventListener("change", updateStandalone);
      window.removeEventListener("beforeinstallprompt", captureInstallPrompt);
    };
  }, []);

  useEffect(() => {
    if (
      !selectedDevice ||
      !("serviceWorker" in navigator) ||
      !("PushManager" in window)
    ) {
      return;
    }
    const controller = new AbortController();
    void Promise.all([
      navigator.serviceWorker.ready.then((registration) =>
        registration.pushManager.getSubscription(),
      ),
      fetch("/api/push-subscriptions", {
        cache: "no-store",
        signal: controller.signal,
      }).then(async (response) => {
        const payload = asRecord(await response.json().catch(() => ({})));
        return response.ok && Array.isArray(payload.subscriptions)
          ? payload.subscriptions
          : [];
      }),
    ])
      .then(([browserSubscription, subscriptions]) => {
        if (controller.signal.aborted) return;
        const serverSubscription = subscriptions
          .map(asRecord)
          .find((subscription) =>
            stringValue(subscription.deviceId, subscription.device_id) === selectedDevice.id &&
            stringValue(subscription.endpoint) === browserSubscription?.endpoint
          );
        const registrationCount = subscriptions
          .map(asRecord)
          .filter((subscription) =>
            stringValue(subscription.endpoint) === browserSubscription?.endpoint
          ).length;
        setPushEnabled(Boolean(browserSubscription && serverSubscription));
        setPushSubscriptionId(stringValue(serverSubscription?.id) ?? "");
        setPushEndpointRegistrationCount(registrationCount);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [selectedDevice]);

  const removeEventFromList = useCallback(async (event: HomecamEvent) => {
    if (!selectedDevice) return;
    const confirmed = window.confirm(
      "이 이벤트를 목록에서 삭제할까요? 저장된 원본 영상은 7일 보관 기간이 지나면 자동으로 삭제됩니다.",
    );
    if (!confirmed) return;
    setBusy(`event-delete:${event.id}`);
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/events/${encodeURIComponent(event.id)}`,
        { method: "DELETE" },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) {
        throw new Error(stringValue(payload.error) ?? "이벤트를 삭제하지 못했습니다.");
      }
      setEvents((current) => current.filter((item) => item.id !== event.id));
      setFocusedEventId("");
      setSelectedEvent((current) => current?.id === event.id ? null : current);
      setNotice(
        stringValue(payload.message) ??
          "목록에서 삭제했습니다. 원본 영상은 보관 기간이 지나면 자동 삭제됩니다.",
      );
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "이벤트를 삭제하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }, [selectedDevice]);

  const loadEvents = useCallback(async (options?: {
    append?: boolean;
    before?: EventCursor | null;
  }) => {
    if (!selectedDevice) return;
    const append = options?.append === true;
    setEventsLoading(true);
    setNotice("");
    try {
      const params = new URLSearchParams();
      if (append && options?.before) {
        params.set("before", options.before.occurredAt);
        params.set("beforeId", options.before.id);
      }
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/events?${params}`,
        { cache: "no-store" },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "이벤트 기록을 불러오지 못했습니다.");
      const rawEvents = Array.isArray(payload.events)
        ? payload.events
        : Array.isArray(payload.items)
          ? payload.items
          : [];
      const nextEvents = rawEvents
        .map(normalizeEvent)
        .filter((event): event is HomecamEvent => event !== null)
        .sort((left, right) => Date.parse(right.occurredAt) - Date.parse(left.occurredAt));
      const deepLinkedEventId = append ? "" : deepLinkedEventIdRef.current;
      let deepLinkedEvent = nextEvents.find(
        (event) => event.id === deepLinkedEventId,
      );
      if (deepLinkedEventId && !deepLinkedEvent) {
        const exactParams = new URLSearchParams({ event: deepLinkedEventId });
        const exactResponse = await fetch(
          `/api/devices/${encodeURIComponent(selectedDevice.id)}/events?${exactParams}`,
          { cache: "no-store" },
        );
        const exactPayload = asRecord(
          await exactResponse.json().catch(() => ({})),
        );
        const exactEvents = Array.isArray(exactPayload.events)
          ? exactPayload.events
          : [];
        deepLinkedEvent = exactEvents
          .map(normalizeEvent)
          .find((item): item is HomecamEvent => item !== null);
        if (deepLinkedEvent) nextEvents.unshift(deepLinkedEvent);
      }
      setEvents((current) => {
        const byId = new Map(
          [...(append ? current : []), ...nextEvents].map((item) => [
            item.id,
            item,
          ]),
        );
        return [...byId.values()].sort(
          (left, right) =>
            Date.parse(right.occurredAt) - Date.parse(left.occurredAt),
        );
      });
      const nextBefore = stringValue(payload.nextBefore);
      const nextBeforeId = stringValue(payload.nextBeforeId);
      setEventCursor(
        nextBefore && nextBeforeId
          ? { occurredAt: nextBefore, id: nextBeforeId }
          : null,
      );
      if (deepLinkedEvent) {
        deepLinkedEventIdRef.current = "";
        setSelectedEvent(deepLinkedEvent);
      } else if (deepLinkedEventId) {
        deepLinkedEventIdRef.current = "";
      }
    } catch (reason) {
      if (!append) {
        setEvents([]);
        setEventCursor(null);
      }
      setNotice(
        reason instanceof Error
          ? reason.message
          : "이벤트 API가 준비되면 최근 7일 기록이 여기에 표시됩니다.",
      );
    } finally {
      setEventsLoading(false);
    }
  }, [selectedDevice]);

  useEffect(() => {
    if (tab === "events" || tab === "home") window.queueMicrotask(() => void loadEvents());
  }, [loadEvents, tab]);

  const loadFamily = useCallback(async () => {
    if (!selectedDevice) return;
    setFamilyLoading(true);
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/family`,
        { cache: "no-store" },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "가족 목록을 불러오지 못했습니다.");
      const rawMembers = Array.isArray(payload.members)
        ? payload.members
        : Array.isArray(payload.family)
          ? payload.family
          : [];
      setFamily(
        rawMembers.flatMap((value) => {
          const raw = asRecord(value);
          const email = stringValue(raw.email, raw.userEmail, raw.user_email);
          const id = stringValue(raw.id, raw.memberId, raw.member_id) ?? email;
          const roleValue = stringValue(raw.role);
          if (!id || !email || (roleValue !== "owner" && roleValue !== "family")) return [];
          return [{ id, email, role: roleValue }];
        }),
      );
    } catch (reason) {
      setNotice(
        reason instanceof Error
          ? reason.message
          : "가족 관리 API가 아직 연결되지 않았습니다.",
      );
    } finally {
      setFamilyLoading(false);
    }
  }, [selectedDevice]);

  useEffect(() => {
    if (tab === "settings") window.queueMicrotask(() => void loadFamily());
  }, [loadFamily, tab]);

  const updateSetting = async (
    key: "monitoringEnabled" | "cameraEnabled" | "microphoneEnabled",
    value: boolean,
  ) => {
    if (!selectedDevice || busy) return;
    setBusy(key);
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/settings`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ [key]: value }),
        },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "설정을 저장하지 못했습니다.");
      const returnedSettings = asRecord(payload.settings);
      setDevices((current) =>
        current.map((device) =>
          device.id === selectedDevice.id
            ? {
                ...device,
                [key]: value,
                monitoringEnabled: booleanValue(
                  key === "monitoringEnabled" ? value : device.monitoringEnabled,
                  returnedSettings.monitoringEnabled,
                  returnedSettings.monitoring_enabled,
                ),
                cameraEnabled: booleanValue(
                  key === "cameraEnabled" ? value : device.cameraEnabled,
                  returnedSettings.cameraEnabled,
                  returnedSettings.camera_enabled,
                ),
                microphoneEnabled: booleanValue(
                  key === "microphoneEnabled" ? value : device.microphoneEnabled,
                  returnedSettings.microphoneEnabled,
                  returnedSettings.microphone_enabled,
                ),
              }
            : device,
        ),
      );
      setNotice("홈캠 설정을 저장했습니다.");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "설정을 저장하지 못했습니다.");
    } finally {
      setBusy("");
    }
  };

  const openLive = async () => {
    if (!selectedDevice || busy) return;
    setBusy("live");
    setNotice("");
    try {
      await onOpenLive(selectedDevice);
    } catch (reason) {
      setNotice(
        reason instanceof Error
          ? reason.message
          : "실시간 연결을 시작하지 못했습니다.",
      );
    } finally {
      setBusy("");
    }
  };

  const togglePush = async () => {
    if (busy) return;
    if (
      !("serviceWorker" in navigator) ||
      !("PushManager" in window) ||
      !("Notification" in window)
    ) {
      setNotice("이 브라우저는 Web Push를 지원하지 않습니다.");
      return;
    }
    setBusy("push");
    setNotice("");
    try {
      const registration = await navigator.serviceWorker.ready;
      const current = await registration.pushManager.getSubscription();
      if (pushEnabled) {
        if (current) {
          if (!pushSubscriptionId) throw new Error("해제할 알림 구독을 찾지 못했습니다.");
          const response = await fetch(
            `/api/push-subscriptions/${encodeURIComponent(pushSubscriptionId)}`,
            { method: "DELETE" },
          );
          const payload = asRecord(await response.json().catch(() => ({})));
          if (!response.ok) throw new Error(stringValue(payload.error) ?? "알림 해제를 저장하지 못했습니다.");
          if (pushEndpointRegistrationCount <= 1) await current.unsubscribe();
        }
        setPushEnabled(false);
        setPushSubscriptionId("");
        setPushEndpointRegistrationCount((count) => Math.max(0, count - 1));
        setNotice("이 기기의 알림을 껐습니다.");
        return;
      }

      if (!selectedDevice) throw new Error("알림을 받을 홈캠을 먼저 선택해 주세요.");
      const permission = await Notification.requestPermission();
      if (permission !== "granted") throw new Error("알림 권한이 허용되지 않았습니다.");
      let keyResponse = await fetch("/api/push-subscriptions/vapid-public-key", {
        cache: "no-store",
      });
      if (keyResponse.status === 404) {
        keyResponse = await fetch("/api/push/vapid-public-key", { cache: "no-store" });
      }
      const keyPayload = asRecord(await keyResponse.json().catch(() => ({})));
      const publicKey = stringValue(keyPayload.publicKey, keyPayload.vapidPublicKey);
      if (!keyResponse.ok || !publicKey) {
        throw new Error(stringValue(keyPayload.error) ?? "푸시 공개 키를 불러오지 못했습니다.");
      }
      const subscription =
        current ??
        await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: decodeBase64Url(publicKey),
        });
      const serialized = subscription.toJSON();
      const response = await fetch("/api/push-subscriptions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          deviceId: selectedDevice.id,
          endpoint: serialized.endpoint,
          keys: serialized.keys,
        }),
      });
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) {
        if (!current) await subscription.unsubscribe().catch(() => undefined);
        throw new Error(stringValue(payload.error) ?? "푸시 구독을 저장하지 못했습니다.");
      }
      setPushEnabled(true);
      setPushEndpointRegistrationCount((count) => Math.max(1, count + 1));
      const saved = asRecord(payload.subscription);
      setPushSubscriptionId(stringValue(saved.id) ?? "");
      setNotice("사람·반려동물·움직임 알림을 받을 수 있습니다.");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "알림 설정에 실패했습니다.");
    } finally {
      setBusy("");
    }
  };

  const inviteFamily = async () => {
    if (!selectedDevice || !inviteEmail.trim() || busy) return;
    setBusy("family");
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/family`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ email: inviteEmail.trim().toLowerCase() }),
        },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "가족을 초대하지 못했습니다.");
      setInviteEmail("");
      setNotice("가족 계정에 홈캠 접근 권한을 부여했습니다.");
      await loadFamily();
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "가족을 초대하지 못했습니다.");
    } finally {
      setBusy("");
    }
  };

  const removeFamily = async (member: FamilyMember) => {
    if (!selectedDevice || busy) return;
    setBusy(`family:${member.id}`);
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/family`,
        {
          method: "DELETE",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ email: member.email }),
        },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "가족 권한을 해제하지 못했습니다.");
      setFamily((current) => current.filter((item) => item.id !== member.id));
      setNotice(`${member.email}의 접근 권한을 해제했습니다.`);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "가족 권한을 해제하지 못했습니다.");
    } finally {
      setBusy("");
    }
  };

  const installApp = async () => {
    if (!installPrompt) return;
    await installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    if (choice.outcome === "accepted") {
      setStandalone(true);
      setInstallPrompt(null);
    }
  };

  const isOwner = selectedDevice?.role === "owner";
  return (
    <div className={`homecam-shell homecam-dashboard-shell tab-${tab} theme-${colorMode}`}>
      <HomecamHeader
        activeTab={tab}
        onNavigate={(nextTab) => {
          if (nextTab === "map") openMap("view");
          else setTab(nextTab);
        }}
        onInstall={installApp}
        showInstall={!standalone && Boolean(installPrompt)}
      />

      <main className="homecam-main">
        {tab !== "map" && <div className="homecam-device-bar">
          {tab !== "home" && <h1>{tab === "live" ? "홈캠" : tab === "events" ? "이벤트" : "설정"}</h1>}
          {tab === "events" && (
            <>
              <div className="homecam-event-period">최근 7일</div>
              <div className="homecam-filter-row" aria-label="이벤트 종류 필터">
                {(["all", "person", "pet", "motion"] as const).map((filter) => (
                  <button
                    key={filter}
                    type="button"
                    className={eventFilter === filter ? "is-selected" : ""}
                    aria-pressed={eventFilter === filter}
                    onClick={() => { setEventFilter(filter); setFocusedEventId(""); }}
                  >
                    {eventFilterLabel(filter)} {eventCounts[filter]}
                  </button>
                ))}
              </div>
              <span className="homecam-device-bar-meta">검색 · 보관 정책에 따라 자동 삭제</span>
              <button type="button" className="homecam-refresh-button" onClick={() => void loadEvents()} disabled={eventsLoading}>
                <ArrowClockwise size={15} weight="bold" aria-hidden="true" />
                {eventsLoading ? "불러오는 중" : "새로고침"}
              </button>
            </>
          )}
          {tab !== "events" && <div className="homecam-device-selector">
            <span className="homecam-device-avatar" aria-hidden="true">말</span>
            <label htmlFor="homecam-device-select">말벗 기기</label>
            {devices.length > 1 ? (
              <select
                id="homecam-device-select"
                value={selectedDevice?.id ?? ""}
                onChange={(event) => setSelectedDeviceId(event.target.value)}
              >
                {devices.map((device) => (
                  <option key={device.id} value={device.id}>{device.displayName}</option>
                ))}
              </select>
            ) : (
              <strong>{selectedDevice?.displayName ?? "등록된 말벗 없음"}</strong>
            )}
          </div>}
          {tab !== "events" && <span className={`homecam-connection-pill ${selectedDevice?.online ? "is-online" : ""}`}>
            <i aria-hidden="true" />
            {selectedDevice?.online
              ? tab === "live" && selectedDevice.mediaHealthy ? "실시간 연결됨" : "연결됨"
              : "오프라인"}
          </span>}
          {tab === "live" && (
            <span className="homecam-device-bar-meta">
              {selectedDevice?.mediaHealthy ? "보안 영상 채널 준비됨" : "영상 채널 확인 중"}
              {` · ${selectedDevice?.monitoringEnabled ? "이벤트 영상만 저장" : "영상 저장 안 함"}`}
            </span>
          )}
          {tab === "settings" && <span className="homecam-device-bar-meta">{selectedDevice?.role === "owner" ? "소유자 설정" : "읽기 전용"}</span>}
        </div>}

        {availability === "loading" && (
          <div className="homecam-loading" role="status">
            <span aria-hidden="true" />
            등록된 홈캠을 확인하고 있습니다.
          </div>
        )}

        {tab === "home" && (
          <section className="homecam-home-view" aria-label="말벗 지금 상태">
            <div className="homecam-home-workspace">
              <div className="homecam-home-primary">
                <article className="homecam-home-hero">
                  <div>
                    <span>지금 말벗은</span>
                    <h1>
                      {selectedDevice?.online
                        ? "집 안에서 대기하고 있어요"
                        : "연결을 기다리고 있어요"}
                    </h1>
                    <div className="homecam-home-chips">
                      <span>{selectedDevice?.online ? "기기 연결됨" : "기기 오프라인"}</span>
                      <span>{selectedDevice?.mediaHealthy ? "영상 상태 정상" : "영상 확인 필요"}</span>
                      <span>{selectedDevice?.cameraEnabled ? "카메라 켜짐" : "카메라 꺼짐"} · {selectedDevice?.microphoneEnabled ? "마이크 켜짐" : "마이크 꺼짐"}</span>
                    </div>
                  </div>
                  <div className="homecam-home-actions">
                    <button type="button" onClick={() => openMap("navigate")}>지도에서 보내기</button>
                    <button type="button" className="is-secondary" onClick={() => setTab("live")}>홈캠 열기</button>
                  </div>
                </article>

                <div className="homecam-home-main-grid">
                  <button type="button" className="homecam-home-camera" onClick={() => setTab("live")}>
                    <span className="homecam-home-live"><i aria-hidden="true" />실시간</span>
                    <VideoCamera size={42} weight="light" aria-hidden="true" />
                    <strong>{selectedDevice?.online ? "거실 실시간 영상 열기" : "홈캠 연결 상태 확인"}</strong>
                    <small>보호자 계정으로 안전하게 연결합니다</small>
                    <span className="homecam-home-camera-action">홈캠 크게 보기</span>
                  </button>

                  <article className="homecam-home-events">
                    <header>
                      <div><h2>확인이 필요해요</h2><span>{events.length}건</span></div>
                      <button type="button" onClick={() => setTab("events")}>전체 보기</button>
                    </header>
                    <div>
                      {eventsLoading && events.length === 0 && <p>이벤트를 확인하고 있어요…</p>}
                      {!eventsLoading && events.length === 0 && (
                        <p className="is-safe"><CheckCircle size={24} weight="fill" /> 지금 상태는 안전해요</p>
                      )}
                      {events.slice(0, 3).map((event) => (
                        <button type="button" key={event.id} onClick={() => { setFocusedEventId(event.id); setTab("events"); }}>
                          <span className={`event-kind-icon kind-${event.type}`}><EventKindIcon type={event.type} /></span>
                          <span><strong>{EVENT_LABELS[event.type]} 감지</strong><small>{formatEventTime(event.occurredAt)}</small></span>
                          <CaretRight size={17} weight="bold" />
                        </button>
                      ))}
                    </div>
                  </article>
                </div>
              </div>

              <aside className="homecam-home-sidebar">
                <HomeMapSummary device={selectedDevice} onOpenMap={openMap} />
                <article className="homecam-home-privacy">
                  <ShieldCheck size={24} weight="regular" aria-hidden="true" />
                  <div><span>개인정보</span><strong>{selectedDevice?.monitoringEnabled ? "이벤트 영상만 저장하고 있어요" : "영상 저장을 사용하지 않아요"}</strong></div>
                  <button type="button" onClick={() => setTab("settings")}>저장 설정 보기</button>
                </article>
              </aside>
            </div>
          </section>
        )}

        {tab === "live" && (
          <section className="homecam-live-view" aria-label="실시간 홈캠">
            {liveViewer?.({ eventCount: events.length, openEvents: () => setTab("events") }) ?? <div className="homecam-video-card">
              <div className="homecam-video-frame">
                <div className="homecam-video-topbar">
                  <span className="homecam-video-clock">{formatLiveClock(liveClockMs)}</span>
                  <button type="button" onClick={() => void openLive()} disabled={!selectedDevice?.online || !selectedDevice.cameraEnabled || busy === "live"} aria-label="실시간 영상을 크게 보기">
                    <CornersOut size={19} weight="regular" aria-hidden="true" />
                  </button>
                </div>
                <div className="homecam-video-message">
                  <VideoCamera size={38} weight="light" aria-hidden="true" />
                  <h2>
                    {!selectedDevice
                      ? "홈캠을 연결해 주세요"
                      : !selectedDevice.cameraEnabled
                        ? "카메라가 꺼져 있어요"
                        : selectedDevice.online
                          ? "보안 채널 연결 대기 중"
                          : "홈캠이 오프라인이에요"}
                  </h2>
                  <button
                    type="button"
                    className="homecam-live-button"
                    onClick={() => void openLive()}
                    disabled={!selectedDevice?.online || !selectedDevice.cameraEnabled || busy === "live"}
                  >
                    <Play size={15} weight="fill" aria-hidden="true" />
                    {busy === "live" ? "연결 중" : "실시간 보기"}
                  </button>
                </div>
                <div className="homecam-video-bottom">
                  <span className="homecam-video-control-chip">
                    <Camera size={16} weight="regular" aria-hidden="true" />
                    {selectedDevice?.cameraEnabled ? "카메라 켜짐" : "카메라 꺼짐"}
                  </span>
                  <span className="homecam-video-control-chip">
                    <ShieldCheck size={16} weight="regular" aria-hidden="true" />
                    {selectedDevice?.monitoringEnabled ? "이벤트만 저장" : "저장 안 함"}
                  </span>
                  <button type="button" onClick={() => setTab("events")}>
                    최근 클립 {events.length}건
                  </button>
                </div>
              </div>
            </div>}

            <div className="homecam-quick-grid">
              <article className="homecam-live-state-card">
                <h2>지금 상태</h2>
                <div className="homecam-live-state-list">
                  <div>
                    <i className={selectedDevice?.online && selectedDevice.mediaHealthy ? "is-good" : ""} aria-hidden="true" />
                    <span>영상 연결</span>
                    <strong>{selectedDevice?.online && selectedDevice.mediaHealthy ? "좋음" : selectedDevice?.online ? "준비 중" : "오프라인"}</strong>
                  </div>
                  <div>
                    <i className={selectedDevice?.cameraEnabled ? "is-good" : ""} aria-hidden="true" />
                    <span>카메라</span>
                    <strong>{selectedDevice?.cameraEnabled ? "켜짐" : "꺼짐"}</strong>
                  </div>
                  <div>
                    <i className={selectedDevice?.microphoneEnabled ? "is-good" : ""} aria-hidden="true" />
                    <span>보호자 마이크</span>
                    <strong>{selectedDevice?.microphoneEnabled ? "사용 가능" : "꺼짐"}</strong>
                  </div>
                  <div>
                    <i aria-hidden="true" />
                    <span>영상 저장</span>
                    <strong>{selectedDevice?.monitoringEnabled ? "이벤트만" : "안 함"}</strong>
                    {selectedDevice && (
                      <Switch
                        checked={selectedDevice.monitoringEnabled}
                        disabled={!isOwner || Boolean(busy)}
                        label="이벤트 영상 저장"
                        onChange={(value) => void updateSetting("monitoringEnabled", value)}
                      />
                    )}
                  </div>
                </div>
              </article>
              <article className="homecam-live-recent-card">
                <div><h2>최근 이벤트 클립</h2><button type="button" onClick={() => setTab("events")}>전체 보기</button></div>
                <section>
                  {events.slice(0, 2).map((event) => (
                    <button type="button" key={event.id} onClick={() => { setFocusedEventId(event.id); setTab("events"); }}>
                      <span className={`kind-${event.type}`}><EventKindIcon type={event.type} /></span>
                      <small>{formatEventTime(event.occurredAt)}</small>
                      <strong>{eventBadgeLabel(event.type)}</strong>
                    </button>
                  ))}
                  {events.length === 0 && <p>아직 저장된 이벤트 클립이 없습니다.</p>}
                </section>
              </article>
              <div className="homecam-live-side-actions">
                <button type="button" onClick={() => void openLive()} disabled={!selectedDevice?.online || !selectedDevice.cameraEnabled || busy === "live"}>
                  <ArrowClockwise size={16} weight="bold" aria-hidden="true" />
                  {liveViewer ? "연결 재시도" : "실시간 연결"}
                </button>
                <button
                  type="button"
                  disabled={!selectedDevice || !isOwner || Boolean(busy)}
                  onClick={() => selectedDevice && void updateSetting("cameraEnabled", !selectedDevice.cameraEnabled)}
                >
                  <Camera size={16} weight="regular" aria-hidden="true" />
                  {selectedDevice?.cameraEnabled ? "카메라 끄기" : "카메라 켜기"}
                </button>
              </div>
            </div>
          </section>
        )}

        {tab === "events" && (
          <section className="homecam-section" aria-labelledby="homecam-events-title">
            <h1 id="homecam-events-title" className="sr-only">이벤트</h1>
            <div className="homecam-events-workspace">
              <div className="homecam-event-list">
                {visibleEvents.length > 0 && <h2>최근 이벤트</h2>}
                {eventsLoading && events.length === 0 && (
                  <div className="homecam-empty-state" role="status">이벤트를 불러오는 중입니다…</div>
                )}
                {!eventsLoading && visibleEvents.length === 0 && (
                  <div className="homecam-empty-state">
                    <CheckCircle size={34} weight="light" aria-hidden="true" />
                    <strong>확인할 이벤트가 없어요</strong>
                    <p>{eventFilter === "all" ? "모니터링을 켜면 감지 기록이 시간순으로 표시됩니다." : "선택한 종류의 이벤트가 없습니다."}</p>
                  </div>
                )}
                {visibleEvents.map((event) => (
                  <button
                    type="button"
                    className={`homecam-event-row ${focusedEvent?.id === event.id ? "is-focused" : ""}`}
                    key={event.id}
                    onClick={() => setFocusedEventId(event.id)}
                  >
                    <span className={`homecam-event-thumbnail kind-${event.type}`} aria-hidden="true">
                      <EventKindIcon type={event.type} />
                      <small>{event.clipState === "recording" ? "저장 중" : eventCanPlay(event) ? "클립" : "기록"}</small>
                    </span>
                    <span className="homecam-event-row-copy">
                      <small><i className={`kind-${event.type}`}>{eventBadgeLabel(event.type)}</i>{formatEventTime(event.occurredAt)}</small>
                      <strong>{eventTitle(event)}</strong>
                      <em>{eventClipStatus(event)}{event.confidence !== null ? ` · 신뢰도 ${Math.round(event.confidence * 100)}%` : ""}</em>
                    </span>
                    <span className="event-play-icon" aria-hidden="true">
                      {eventCanPlay(event)
                        ? <Play size={14} weight="fill" />
                        : <CaretRight size={16} weight="bold" />}
                    </span>
                  </button>
                ))}
                {eventCursor && events.length > 0 && (
                  <button type="button" className="homecam-refresh-button" disabled={eventsLoading} onClick={() => void loadEvents({ append: true, before: eventCursor })}>
                    {eventsLoading ? "불러오는 중" : "이전 이벤트 더 보기"}
                  </button>
                )}
              </div>
              <aside className="homecam-event-detail">
                {focusedEvent ? (
                  <>
                    <div className="homecam-event-detail-video">
                      <span className="homecam-event-video-meta">{eventBadgeLabel(focusedEvent.type)} · {formatEventTime(focusedEvent.occurredAt)}</span>
                      <div><EventKindIcon type={focusedEvent.type} /><span>{eventClipStatus(focusedEvent)}</span></div>
                      {eventCanPlay(focusedEvent) && <button type="button" onClick={() => setSelectedEvent(focusedEvent)}><Play size={17} weight="fill" /> 영상 재생</button>}
                    </div>
                    <div className="homecam-event-detail-copy">
                      <span><i className={`kind-${focusedEvent.type}`}>{eventBadgeLabel(focusedEvent.type)}</i>{formatEventTime(focusedEvent.occurredAt)}</span>
                      <h2>{eventTitle(focusedEvent)}</h2>
                      <div>
                        <strong>감지 정보</strong>
                        <p>{focusedEvent.eventGroupId && focusedEvent.clipState === "ready" ? "감지 전 5초부터 마지막 움직임 10초 후까지 저장된 구간입니다." : focusedEvent.recordingId ? "지난 저장 영상에서 감지 시각을 확인할 수 있습니다." : "이 이벤트는 감지 기록만 있으며 연결된 영상 구간은 없습니다."} 자동 감지 결과는 실제 상황과 다를 수 있습니다.</p>
                        {focusedEvent.ai.summary && <p>{focusedEvent.ai.summary}</p>}
                      </div>
                      <div className="homecam-event-detail-actions">
                        <button type="button" onClick={() => setSelectedEvent(focusedEvent)} disabled={!eventCanPlay(focusedEvent)}>영상 클립 확인</button>
                        {isOwner && (
                          <button type="button" onClick={() => void removeEventFromList(focusedEvent)} disabled={busy === `event-delete:${focusedEvent.id}`}>
                            <Trash size={16} weight="regular" /> 목록에서 삭제
                          </button>
                        )}
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="homecam-empty-state"><ClockCounterClockwise size={34} weight="light" /><strong>이벤트를 선택해 주세요</strong><p>왼쪽 목록에서 기록을 선택하면 상세 내용이 표시됩니다.</p></div>
                )}
              </aside>
            </div>
          </section>
        )}

        {tab === "map" && <RobotMapPanel key={mapEntryMode} device={selectedDevice} initialMode={mapEntryMode} />}

        {tab === "settings" && (
          <section className="homecam-section" aria-labelledby="homecam-settings-title">
            <div className="homecam-section-heading">
              <div>
                <span>개인정보 보호</span>
                <h1 id="homecam-settings-title">홈캠 설정</h1>
              </div>
              <span className="homecam-role-badge">
                {selectedDevice?.role === "owner"
                  ? "소유자"
                  : selectedDevice?.role === "family"
                    ? "가족"
                    : "읽기 전용"}
              </span>
            </div>
            <div className="homecam-settings-workspace">
              <aside className="homecam-settings-nav" aria-label="설정 항목">
                <button type="button" className="is-active">화면 모드</button>
                <button type="button">가족 구성원</button>
                <button type="button">로봇 이름</button>
                <button type="button">카메라와 마이크</button>
                <button type="button">알림</button>
                <button type="button">영상 보관</button>
                <button type="button">이벤트 감지</button>
                <button type="button">개인정보</button>
                <button type="button" onClick={() => openMap("view")}>지도 관리</button>
                <button type="button">연결된 장치</button>
                <button type="button">소프트웨어 정보</button>
              </aside>
              <div className="homecam-settings-grid">
              <section className="homecam-settings-card homecam-display-settings">
                <div className="settings-card-heading">
                  <span className="settings-heading-icon" aria-hidden="true">
                    {colorMode === "dark"
                      ? <Moon size={21} weight="regular" />
                      : <Sun size={21} weight="regular" />}
                  </span>
                  <div>
                    <h2>화면 모드</h2>
                    <p>홈·홈캠·이벤트·지도·설정 화면의 밝기를 선택합니다.</p>
                  </div>
                </div>
                <div className="homecam-theme-options" role="group" aria-label="화면 모드 선택">
                  <button type="button" className={colorMode === "light" ? "is-active" : ""} onClick={() => {
                    window.localStorage.setItem("malbut-color-mode", "light");
                    setColorMode("light");
                  }}>
                    <Sun size={19} weight="regular" aria-hidden="true" />
                    화이트 모드
                  </button>
                  <button type="button" className={colorMode === "dark" ? "is-active" : ""} onClick={() => {
                    window.localStorage.setItem("malbut-color-mode", "dark");
                    setColorMode("dark");
                  }}>
                    <Moon size={19} weight="regular" aria-hidden="true" />
                    블랙 모드
                  </button>
                </div>
              </section>

              <section className="homecam-settings-card">
                <div className="settings-card-heading">
                  <span className="settings-heading-icon" aria-hidden="true">
                    <Camera size={21} weight="regular" />
                  </span>
                  <div>
                    <h2>카메라와 녹화</h2>
                    <p>설정 상태는 로봇 화면에도 항상 표시됩니다.</p>
                  </div>
                </div>
                <div className="homecam-setting-row">
                  <div><strong>모니터링 모드</strong><span>연속 녹화와 AI 이벤트 알림 · 7일 보관</span></div>
                  <Switch
                    checked={Boolean(selectedDevice?.monitoringEnabled)}
                    disabled={!selectedDevice || !isOwner || Boolean(busy)}
                    label="모니터링 모드"
                    onChange={(value) => void updateSetting("monitoringEnabled", value)}
                  />
                </div>
                <div className="homecam-setting-row">
                  <div><strong>카메라</strong><span>끄면 라이브와 감지를 모두 중지합니다.</span></div>
                  <Switch
                    checked={Boolean(selectedDevice?.cameraEnabled)}
                    disabled={!selectedDevice || !isOwner || Boolean(busy)}
                    label="카메라"
                    onChange={(value) => void updateSetting("cameraEnabled", value)}
                  />
                </div>
                <div className="homecam-setting-row">
                  <div><strong>로봇 마이크</strong><span>집 안의 소리를 보호자에게 전달합니다.</span></div>
                  <Switch
                    checked={Boolean(selectedDevice?.microphoneEnabled)}
                    disabled={!selectedDevice || !isOwner || Boolean(busy)}
                    label="로봇 마이크"
                    onChange={(value) => void updateSetting("microphoneEnabled", value)}
                  />
                </div>
              </section>

              <section className="homecam-settings-card">
                <div className="settings-card-heading">
                  <span className="settings-heading-icon" aria-hidden="true">
                    <Bell size={21} weight="regular" />
                  </span>
                  <div>
                    <h2>이벤트 알림</h2>
                    <p>알림에는 사진 없이 종류와 시각만 표시합니다.</p>
                  </div>
                </div>
                <div className="homecam-setting-row">
                  <div><strong>Web Push</strong><span>사람·반려동물·움직임이 감지되면 알려드려요.</span></div>
                  <Switch
                    checked={pushEnabled}
                    disabled={busy === "push"}
                    label="Web Push 알림"
                    onChange={() => void togglePush()}
                  />
                </div>
                <p className="homecam-ios-note">
                  iPhone·iPad는 이 사이트를 홈 화면에 설치한 뒤 알림을 켤 수 있습니다.
                </p>
              </section>

              <section className="homecam-settings-card homecam-family-card">
                <div className="settings-card-heading">
                  <span className="settings-heading-icon" aria-hidden="true">
                    <UsersThree size={21} weight="regular" />
                  </span>
                  <div>
                    <h2>가족 계정</h2>
                    <p>가족은 라이브·지난 영상·PTT를 사용할 수 있습니다.</p>
                  </div>
                </div>
                {isOwner && (
                  <div className="homecam-family-invite">
                    <label>
                      <span className="sr-only">초대할 가족 이메일</span>
                      <input
                        type="email"
                        value={inviteEmail}
                        onChange={(event) => setInviteEmail(event.target.value)}
                        placeholder="family@example.com"
                        autoComplete="email"
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => void inviteFamily()}
                      disabled={!inviteEmail.includes("@") || busy === "family"}
                    >
                      초대
                    </button>
                  </div>
                )}
                <div className="homecam-family-list" aria-busy={familyLoading}>
                  {familyLoading && <p>가족 계정을 불러오는 중입니다…</p>}
                  {!familyLoading && family.length === 0 && <p>아직 연결된 가족 계정이 없습니다.</p>}
                  {!familyLoading && family.map((member) => (
                    <div key={member.id}>
                      <span className="family-avatar" aria-hidden="true">{member.email.slice(0, 1).toUpperCase()}</span>
                      <span><strong>{member.email}</strong><small>{member.role === "owner" ? "소유자" : "가족"}</small></span>
                      {isOwner && member.role !== "owner" && (
                        <button
                          type="button"
                          onClick={() => void removeFamily(member)}
                          disabled={busy === `family:${member.id}`}
                        >
                          권한 해제
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </section>
              </div>
            </div>
          </section>
        )}

        {(externalError || (notice && (tab !== "live" || selectedDevice))) && (
          <div className="homecam-notice" role="status">
            <Info size={18} weight="bold" aria-hidden="true" />
            <p>{externalError || notice}</p>
            {availability === "unavailable" && (
              <button type="button" onClick={() => void loadDevices()}>다시 확인</button>
            )}
          </div>
        )}

        {tab === "settings" && (
          <details className="homecam-legacy" open={legacyOpen} onToggle={(event) => setLegacyOpen(event.currentTarget.open)}>
            <summary>개발·이전 버전 연결</summary>
            <div>
              <p>등록된 가족 계정 연결이 준비되지 않았을 때만 기존 코드+비밀번호 시청 방식을 사용합니다.</p>
              <div className="homecam-legacy-actions">
                <button
                  type="button"
                  onClick={() => void onCreateLegacyBroadcast()}
                  disabled={creatingLegacyBroadcast}
                >
                  {creatingLegacyBroadcast ? "세션 만드는 중" : "브라우저 카메라 송출"}
                </button>
                <label>
                  <span className="sr-only">기존 세션 코드</span>
                  <input
                    value={legacyCode}
                    onChange={(event) => setLegacyCode(normalizeLegacyCode(event.target.value))}
                    placeholder="6자리 코드"
                    maxLength={6}
                    autoComplete="one-time-code"
                  />
                </label>
                <label>
                  <span className="sr-only">기존 시청 비밀번호</span>
                  <input
                    value={legacyPassword}
                    onChange={(event) => setLegacyPassword(normalizeLegacyPassword(event.target.value))}
                    placeholder="기존 시청 비밀번호"
                    maxLength={19}
                    autoComplete="off"
                  />
                </label>
                <button
                  type="button"
                  onClick={() => onJoinLegacy(legacyCode, legacyPassword)}
                  disabled={legacyCode.length !== 6 || legacyPassword.replace(/-/g, "").length !== LEGACY_PASSWORD_LENGTH}
                >
                  기존 세션 입장
                </button>
              </div>
              {legacyArchive && <div className="homecam-legacy-archive">{legacyArchive}</div>}
            </div>
          </details>
        )}
      </main>

      {selectedEvent && selectedDevice && <EventPlayback event={selectedEvent} deviceId={selectedDevice.id} onClose={() => setSelectedEvent(null)} />}
    </div>
  );
}

function decodeBase64Url(value: string) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}
