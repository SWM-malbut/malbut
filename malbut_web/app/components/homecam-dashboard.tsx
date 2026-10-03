"use client";

import Image from "next/image";
import { FallHomecamSettings } from "./fall-homecam-settings";
import { FallIncidentsPanel } from "./fall-incidents-panel";
import { FallTimelinePanel, type TimelineMode } from "./fall-timeline-panel";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Bell,
  Camera,
  CaretRight,
  CheckCircle,
  CornersOut,
  Info,
  MapTrifold,
  Moon,
  Play,
  ShieldCheck,
  Sun,
  TextAa,
  UsersThree,
  VideoCamera,
  Warning,
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
  p2pHealthy: boolean;
  storageHealthy: boolean;
  storageSessionActive: boolean;
  detectorHealthy: boolean | null;
  activeSession: {
    roomCode: string;
    storageMode: boolean;
  } | null;
};

const LOCAL_DEMO_DEVICE_ID = "local-demo-homecam";
const LOCAL_HOME_CAM_DEMO =
  process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1";
const LOCAL_DEMO_DEVICE: HomecamDevice = {
  id: LOCAL_DEMO_DEVICE_ID,
  displayName: "로컬 데모 홈캠",
  online: true,
  lastSeenAt: new Date(0).toISOString(),
  role: "owner",
  monitoringEnabled: true,
  cameraEnabled: true,
  microphoneEnabled: false,
  mediaHealthy: true,
  p2pHealthy: true,
  storageHealthy: true,
  storageSessionActive: true,
  detectorHealthy: true,
  activeSession: {
    roomCode: "LOCAL1",
    storageMode: false,
  },
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
  liveMediaReady?: boolean;
  onReleaseLive?: () => void;
  liveViewer?: (context: {
    eventCount: number;
    openEvents: () => void;
    device: HomecamDevice | null;
  }) => React.ReactNode;
};

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

function HomeMapSummary({
  device,
  onOpenMap,
}: {
  device: HomecamDevice | null;
  onOpenMap: (mode: MapMode) => void;
}) {
  const deviceId = device?.id ?? "";
  const localDemo = LOCAL_HOME_CAM_DEMO && deviceId === LOCAL_DEMO_DEVICE_ID;
  const [robotSnapshot, setRobotSnapshot] = useState<RobotSnapshot | null>(null);
  const [semantics, setSemantics] = useState<RobotSemantics | null>(null);
  const [rooms, setRooms] = useState<Array<{ id: string; name: string; color: string }>>([]);
  const revision = robotSnapshot?.map?.revision ?? "";

  useEffect(() => {
    if (localDemo) return;
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
  }, [deviceId, localDemo]);

  useEffect(() => {
    if (localDemo) return;
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
  }, [deviceId, localDemo, revision]);

  const visibleRooms = localDemo
    ? [
        { id: "demo-living", name: "거실", color: "#DDE9E8" },
        { id: "demo-bedroom", name: "침실", color: "#E3E7EE" },
        { id: "demo-kitchen", name: "주방", color: "#EFE7DE" },
      ]
    : rooms;

  return (
    <>
      <article className="homecam-home-map-card">
        <h2>지도</h2>
        <button type="button" className="homecam-home-map-preview" onClick={() => onOpenMap("view")}>
          {localDemo ? (
            <span className="homecam-home-map-demo" aria-label="로컬 데모 우리 집 지도">
              <i className="is-room-one" />
              <i className="is-room-two" />
              <i className="is-zone" />
              <b aria-label="말벗 현재 위치" />
            </span>
          ) : device && revision ? (
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
        <h2>주요 목적지</h2>
        <div>
          {visibleRooms.length === 0 && <p>방을 나누고 이름을 정하면 여기에 표시됩니다.</p>}
          {visibleRooms.slice(0, 4).map((room) => (
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
  const sessions = asRecord(raw.activeSessions ?? raw.active_sessions);
  const storageSession = asRecord(sessions.storage);
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
    p2pHealthy: booleanValue(
      false,
      state.p2pHealthy,
      state.p2p_healthy,
      raw.p2pHealthy,
      raw.p2p_healthy,
      state.mediaHealthy,
    ),
    storageHealthy: booleanValue(
      false,
      state.storageHealthy,
      state.storage_healthy,
      raw.storageHealthy,
      raw.storage_healthy,
      state.mediaHealthy,
    ),
    storageSessionActive: Boolean(
      stringValue(storageSession.id) ||
      (roomCode && booleanValue(false, session.storageMode, session.storage_mode)),
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

function formatIncidentTime(value: string) {
  const date = new Date(value);
  const sameDay = date.toDateString() === new Date().toDateString();
  const clock = date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
  return sameDay ? `오늘 ${clock}` : `${date.toLocaleDateString("ko-KR", { month: "long", day: "numeric" })} ${clock}`;
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

function LocalDemoMapPanel({
  mode,
  onModeChange,
}: {
  mode: MapMode;
  onModeChange: (mode: MapMode) => void;
}) {
  const modeCopy = mode === "navigate"
    ? ["목적지 선택 모드", "지도에서 보낼 곳을 눌러 이동 화면과 PiP 배치를 확인하세요."]
    : mode === "rooms"
      ? ["방 편집 모드", "저장된 방 경계와 이름이 홈캠 화면 위에서도 읽히는지 확인하세요."]
      : mode === "zones"
        ? ["구역 편집 모드", "진입 금지 구역과 가상 벽이 PiP에 가리지 않는지 확인하세요."]
        : ["지도 보기 모드", "저장된 공간과 말벗의 현재 위치를 확인하세요."];

  return (
    <section className="homecam-section robot-map-section homecam-local-demo-map" aria-labelledby="robot-map-title">
      <div className="robot-map-topbar">
        <h1 id="robot-map-title">우리 집</h1>
        <div className="robot-map-mode-tabs" aria-label="지도 모드">
          {([
            ["view", "보기"],
            ["navigate", "목적지 선택"],
            ["rooms", "방 편집"],
            ["zones", "구역 편집"],
          ] as Array<[MapMode, string]>).map(([candidate, label]) => (
            <button
              key={candidate}
              type="button"
              className={mode === candidate ? "is-active" : ""}
              onClick={() => onModeChange(candidate)}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="robot-map-top-status is-online">
          <i aria-hidden="true" />
          연결됨 · 위치 확인됨
        </span>
      </div>

      <div className="robot-map-layout">
        <div className="robot-map-primary">
          <div className={`robot-map-mode-banner mode-${mode}`}>
            <strong>{modeCopy[0]}</strong>
            <span>{modeCopy[1]}</span>
          </div>
          <div className={`robot-map-card mode-${mode}`}>
            <div className="homecam-local-demo-map-canvas" aria-label="로컬 데모 우리 집 지도">
              <svg viewBox="0 0 800 480" role="img" aria-label="방과 구역이 표시된 로컬 데모 지도">
                <defs>
                  <pattern id="local-demo-zone-hatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(35)">
                    <rect width="12" height="12" fill="rgba(192,64,47,.08)" />
                    <line x1="0" y1="0" x2="0" y2="12" stroke="rgba(192,64,47,.5)" strokeWidth="3" />
                  </pattern>
                </defs>
                <path className="homecam-local-demo-floor" d="M35 35H765V445H35z" />
                <path className="homecam-local-demo-wall" d="M35 35H765V445H35V35M310 35V225H35M310 225H495V445M495 225H765M310 345H495" />
                <path className="homecam-local-demo-furniture" d="M82 78h128v52H82zM560 72h140v82H560zM93 310h150v78H93zM555 300h120v82H555z" />
                <rect className="homecam-local-demo-zone" x="525" y="260" width="155" height="145" rx="8" />
                <line className="homecam-local-demo-virtual-wall" x1="310" y1="225" x2="495" y2="225" />
                {mode === "navigate" && <path className="homecam-local-demo-route" d="M390 330C425 302 457 296 520 245S625 205 670 190" />}
                {mode === "navigate" && <circle className="homecam-local-demo-goal" cx="670" cy="190" r="12" />}
              </svg>
              <span className="homecam-local-demo-room is-living">거실</span>
              <span className="homecam-local-demo-room is-bedroom">침실</span>
              <span className="homecam-local-demo-room is-kitchen">주방</span>
              <span className="homecam-local-demo-room is-study">작업실</span>
              <span className="homecam-local-demo-zone-label">진입 금지</span>
              <span className="homecam-local-demo-robot" aria-label="말벗 현재 위치"><i /></span>
            </div>
          </div>
          <div className="robot-map-legend">
            <span><i className="is-robot" />말벗 위치와 방향</span>
            <span><i className="is-room" />방 경계·이름</span>
            <span><i className="is-zone is-restricted" />진입 금지</span>
            <span><i className="is-virtual-wall" />가상 벽</span>
          </div>
        </div>

        <aside className="robot-map-sidebar">
          <div className="robot-map-summary">
            <h2>말벗이 집 안에서 대기하고 있어요</h2>
            <div className="robot-map-summary-grid">
              <div><span>로봇 연결</span><strong>정상</strong></div>
              <div><span>현재 위치</span><strong>확인됨</strong></div>
              <div><span>현재 공간</span><strong>거실</strong></div>
              <div><span>구역 확인</span><strong>문제 없음</strong></div>
            </div>
          </div>
          <div className="robot-map-panel-card homecam-local-demo-note">
            <MapTrifold size={26} weight="light" aria-hidden="true" />
            <div>
              <strong>로컬 UI 데모</strong>
              <p>실제 로봇 명령은 전송하지 않습니다. 홈캠 PiP의 크기와 위치만 확인할 수 있어요.</p>
            </div>
          </div>
        </aside>
      </div>
    </section>
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
  liveMediaReady = false,
  onReleaseLive,
  liveViewer,
}: HomecamDashboardProps) {
  const [devices, setDevices] = useState<HomecamDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [tab, setTab] = useState<HomecamTab>(initialTab);
  const [mapEntryMode, setMapEntryMode] = useState<MapMode>("view");
  const [availability, setAvailability] = useState<ApiAvailability>("loading");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  // Fall incidents needing a human: shown on home/live; the 사건 tab has the full list.
  const [openIncidents, setOpenIncidents] = useState<Array<{
    incidentId: string; occurredAt: string; unacknowledged: boolean; aiFailed: boolean; fallSeen: boolean;
  }>>([]);
  const [incidentsLoading, setIncidentsLoading] = useState(false);
  const [focusedIncidentId, setFocusedIncidentId] = useState("");
  const [timelineMode, setTimelineMode] = useState<TimelineMode | null>(null);
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
  const [textSize, setTextSize] = useState<"default" | "large">("default");
  const [liveClockMs, setLiveClockMs] = useState(() => Date.now());
  const [storageGraceUntilMs, setStorageGraceUntilMs] = useState(0);
  const [livePipPosition, setLivePipPosition] = useState<{ x: number; y: number } | null>(null);
  const livePipRef = useRef<HTMLElement>(null);
  const livePipDragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
  } | null>(null);
  const localDemoAutoplayRef = useRef(false);
  const navigationStateReadyRef = useRef(false);
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
    const stored = window.localStorage.getItem("malbut-text-size");
    if (stored === "large") window.queueMicrotask(() => setTextSize("large"));
  }, []);

  useEffect(() => {
    // 큰 글자 모드는 토큰(--fs-*)을 문서 단위로 확대한다. 지도·영상은 영향 없음.
    if (textSize === "large") {
      document.documentElement.dataset.textSize = "large";
    } else {
      delete document.documentElement.dataset.textSize;
    }
    return () => {
      delete document.documentElement.dataset.textSize;
    };
  }, [textSize]);

  useEffect(() => {
    if (tab !== "live") return;
    const timer = window.setInterval(() => setLiveClockMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [tab]);

  const selectedDevice = useMemo(
    () => devices.find((device) => device.id === selectedDeviceId) ?? devices[0] ?? null,
    [devices, selectedDeviceId],
  );
  const devicePollIntervalMs = Boolean(
    selectedDevice?.online &&
    selectedDevice.monitoringEnabled &&
    selectedDevice.cameraEnabled &&
    !selectedDevice.storageHealthy
  ) ? 1_000 : 15_000;
  const displayedMediaReady = liveViewer
    ? liveMediaReady
    : Boolean(selectedDevice?.online && selectedDevice.p2pHealthy);
  const liveViewerActive = Boolean(liveViewer);
  const livePipActive = tab !== "live" && liveViewerActive;

  const moveLivePip = useCallback((clientX: number, clientY: number) => {
    const element = livePipRef.current;
    const drag = livePipDragRef.current;
    if (!element || !drag) return;
    const margin = 12;
    const bounds = element.getBoundingClientRect();
    const maxX = Math.max(margin, window.innerWidth - bounds.width - margin);
    const maxY = Math.max(margin, window.innerHeight - bounds.height - margin);
    setLivePipPosition({
      x: Math.min(maxX, Math.max(margin, clientX - drag.offsetX)),
      y: Math.min(maxY, Math.max(margin, clientY - drag.offsetY)),
    });
  }, []);

  const beginLivePipDrag = useCallback((event: React.PointerEvent<HTMLElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button")) return;
    const bounds = livePipRef.current?.getBoundingClientRect();
    if (!bounds) return;
    livePipDragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - bounds.left,
      offsetY: event.clientY - bounds.top,
    };
    setLivePipPosition({ x: bounds.left, y: bounds.top });
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }, []);

  const endLivePipDrag = useCallback((event: React.PointerEvent<HTMLElement>) => {
    if (livePipDragRef.current?.pointerId !== event.pointerId) return;
    livePipDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  useEffect(() => {
    if (!livePipActive || !livePipPosition) return;
    const keepInsideViewport = () => {
      const bounds = livePipRef.current?.getBoundingClientRect();
      if (!bounds) return;
      const margin = 12;
      setLivePipPosition((current) => current && ({
        x: Math.min(
          Math.max(margin, window.innerWidth - bounds.width - margin),
          Math.max(margin, current.x),
        ),
        y: Math.min(
          Math.max(margin, window.innerHeight - bounds.height - margin),
          Math.max(margin, current.y),
        ),
      }));
    };
    window.addEventListener("resize", keepInsideViewport);
    return () => window.removeEventListener("resize", keepInsideViewport);
  }, [livePipActive, livePipPosition]);

  const loadDevices = useCallback(async (quiet = false) => {
    if (LOCAL_HOME_CAM_DEMO) {
      setDevices([LOCAL_DEMO_DEVICE]);
      setSelectedDeviceId(LOCAL_DEMO_DEVICE_ID);
      setAvailability("ready");
      if (!quiet) setNotice("");
      return;
    }
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
    const interval = window.setInterval(
      () => void loadDevices(true),
      devicePollIntervalMs,
    );
    return () => window.clearInterval(interval);
  }, [devicePollIntervalMs, loadDevices]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedView = params.get("view");
    const requestedDevice = params.get("device")?.trim() ?? "";
    // Fall push notifications open /?view=events&device=…&incident=….
    const requestedIncident = params.get("incident")?.trim() ?? "";
    const requestedMapMode = params.get("mapMode");
    window.queueMicrotask(() => {
      if (requestedDevice) setSelectedDeviceId(requestedDevice);
      if (
        requestedMapMode === "view" ||
        requestedMapMode === "navigate" ||
        requestedMapMode === "rooms" ||
        requestedMapMode === "zones"
      ) {
        setMapEntryMode(requestedMapMode);
      }
      if (requestedIncident) {
        setFocusedIncidentId(requestedIncident);
        setTab("events");
      }
      else if (
        requestedView === "home" ||
        requestedView === "live" ||
        requestedView === "map" ||
        requestedView === "events" ||
        requestedView === "robot" ||
        requestedView === "settings"
      ) {
        setTab(requestedView);
      }
      navigationStateReadyRef.current = true;
    });
  }, []);

  useEffect(() => {
    if (!navigationStateReadyRef.current) return;
    const url = new URL(window.location.href);
    if (tab === "home") url.searchParams.delete("view");
    else url.searchParams.set("view", tab);
    if (tab === "map") url.searchParams.set("mapMode", mapEntryMode);
    else url.searchParams.delete("mapMode");
    url.searchParams.delete("event");
    if (tab === "events" && focusedIncidentId) url.searchParams.set("incident", focusedIncidentId);
    else url.searchParams.delete("incident");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, [focusedIncidentId, mapEntryMode, tab]);

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

  const loadOpenIncidents = useCallback(async () => {
    if (!selectedDevice || (LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID)) {
      setOpenIncidents([]);
      return;
    }
    setIncidentsLoading(true);
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/fall-incidents?filter=check`,
        { cache: "no-store" },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "사건을 불러오지 못했습니다.");
      const rows = Array.isArray(payload.incidents) ? payload.incidents.map(asRecord) : [];
      setOpenIncidents(rows.flatMap((row) => {
        const incidentId = stringValue(row.incidentId);
        const occurredAt = stringValue(row.occurredAt);
        return incidentId && occurredAt ? [{
          incidentId, occurredAt, unacknowledged: row.unacknowledged === true,
          aiFailed: row.aiFailed === true, fallSeen: row.fallSeen === true,
        }] : [];
      }));
    } catch {
      setOpenIncidents([]);
    } finally {
      setIncidentsLoading(false);
    }
  }, [selectedDevice]);

  useEffect(() => {
    if (tab === "home" || tab === "live") window.queueMicrotask(() => void loadOpenIncidents());
  }, [loadOpenIncidents, tab]);

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
    if (LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID) {
      // Local UI demo: no server, change the shown state only.
      setDevices((current) => current.map((device) =>
        device.id === selectedDevice.id ? { ...device, [key]: value } : device));
      return;
    }
    if (key === "monitoringEnabled") {
      setStorageGraceUntilMs(value ? Date.now() + 15_000 : 0);
    }
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
      if (key === "monitoringEnabled") setStorageGraceUntilMs(0);
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

  useEffect(() => {
    if (
      !LOCAL_HOME_CAM_DEMO ||
      selectedDevice?.id !== LOCAL_DEMO_DEVICE_ID ||
      liveViewerActive ||
      localDemoAutoplayRef.current
    ) return;

    localDemoAutoplayRef.current = true;
    void onOpenLive(selectedDevice).catch((reason) => {
      localDemoAutoplayRef.current = false;
      setNotice(
        reason instanceof Error
          ? reason.message
          : "로컬 데모 영상을 시작하지 못했습니다.",
      );
    });
  }, [liveViewerActive, onOpenLive, selectedDevice]);

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
  const storageEnabled = Boolean(selectedDevice?.monitoringEnabled);
  const storageCanRun = Boolean(storageEnabled && selectedDevice?.cameraEnabled);
  const storageReady = Boolean(
    selectedDevice?.online &&
    storageCanRun &&
    selectedDevice.storageHealthy,
  );
  const storageConnecting = Boolean(
    selectedDevice?.online &&
    storageCanRun &&
    !storageReady &&
    (selectedDevice.storageSessionActive || liveClockMs < storageGraceUntilMs),
  );
  const storageError = Boolean(
    storageCanRun &&
    !storageReady &&
    !storageConnecting,
  );
  const storageStateLabel = !storageEnabled
    ? "저장 안 함"
    : !selectedDevice?.cameraEnabled
      ? "카메라 꺼짐 · 저장 대기"
    : storageReady
      ? "연속 녹화 중"
      : storageConnecting
        ? "연속 녹화 준비 중"
        : "연속 녹화 오류";
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
        {tab !== "map" && tab !== "robot" && <div className="homecam-device-bar">
          <h1>{tab === "home" ? "홈" : tab === "live" ? "홈캠" : tab === "events" ? "사건" : "설정"}</h1>
          <div className="homecam-device-selector">
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
          </div>
          <span className={`homecam-connection-pill ${selectedDevice?.online ? "is-online" : ""}`}>
            <i aria-hidden="true" />
            {selectedDevice?.online
              ? tab === "live" && displayedMediaReady ? "실시간 연결됨" : "연결됨"
              : "오프라인"}
          </span>
          {tab === "live" && (
            <span className="homecam-device-bar-meta homecam-live-channel-summary" aria-live="polite">
              <span className={displayedMediaReady ? "is-ready" : "is-pending"}>
                {displayedMediaReady ? "보안 영상 채널 연결됨" : "영상 채널 연결 중"}
              </span>
              <b aria-hidden="true">·</b>
              <span
                className={
                  storageReady
                    ? "is-ready"
                    : storageConnecting
                      ? "is-pending"
                      : storageError
                        ? "is-error"
                        : ""
                }
              >
                {storageStateLabel}
              </span>
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
                      <span>{selectedDevice?.p2pHealthy ? "실시간 영상 준비됨" : "영상 연결 준비 중"}</span>
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
                      <div><h2>확인이 필요해요</h2><span>{openIncidents.length}건</span></div>
                      <button type="button" onClick={() => setTab("events")}>전체 보기</button>
                    </header>
                    <div>
                      {incidentsLoading && openIncidents.length === 0 && <p>사건을 확인하고 있어요…</p>}
                      {!incidentsLoading && openIncidents.length === 0 && (
                        <p className="is-safe"><CheckCircle size={24} weight="fill" /> 확인할 사건이 없어요</p>
                      )}
                      {openIncidents.slice(0, 3).map((incident) => (
                        <button type="button" key={incident.incidentId} onClick={() => { setFocusedIncidentId(incident.incidentId); setTab("events"); }}>
                          <span className="fall-incident-icon is-check"><Warning size={20} weight="bold" /></span>
                          <span>
                            <strong>{incident.unacknowledged ? "아무도 확인하지 않음 · " : ""}{incident.fallSeen ? "낙상" : "낙상 의심"}</strong>
                            <small>{formatIncidentTime(incident.occurredAt)}{incident.aiFailed ? " · AI 검증 실패" : ""}</small>
                          </span>
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
                  <div><span>개인정보</span><strong>{selectedDevice?.monitoringEnabled ? "연속 녹화로 저장하고 있어요 (7일 보관)" : "영상 저장을 사용하지 않아요"}</strong></div>
                  <button type="button" onClick={() => setTab("settings")}>저장 설정 보기</button>
                </article>
              </aside>
            </div>
          </section>
        )}

        {(tab === "live" || liveViewerActive) && (
          <section
            ref={livePipRef}
            className={`homecam-live-view ${livePipActive ? "is-pip" : ""}`}
            aria-label={livePipActive ? "실시간 홈캠 미니 화면" : "실시간 홈캠"}
            style={livePipActive && livePipPosition ? {
              left: livePipPosition.x,
              top: livePipPosition.y,
            } : undefined}
            onPointerDown={livePipActive ? beginLivePipDrag : undefined}
            onPointerMove={livePipActive ? (event) => {
              if (livePipDragRef.current?.pointerId === event.pointerId) {
                moveLivePip(event.clientX, event.clientY);
              }
            } : undefined}
            onPointerUp={livePipActive ? endLivePipDrag : undefined}
            onPointerCancel={livePipActive ? endLivePipDrag : undefined}
          >
            {livePipActive && (
              <div className="homecam-live-pip-actions">
                  <button type="button" onClick={() => setTab("live")} aria-label="홈캠 크게 보기">
                    <CornersOut size={17} weight="bold" aria-hidden="true" />
                  </button>
                  {onReleaseLive && (
                    <button
                      type="button"
                      onClick={() => {
                        setLivePipPosition(null);
                        onReleaseLive();
                      }}
                      aria-label="미니 영상 닫기. 카메라와 영상 저장은 계속 유지됩니다."
                    >
                      <X size={17} weight="bold" aria-hidden="true" />
                    </button>
                  )}
              </div>
            )}
            {liveViewer?.({
              eventCount: openIncidents.length,
              openEvents: () => setTab("events"),
              device: selectedDevice,
            }) ?? <div className="homecam-video-card">
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
                    {selectedDevice?.monitoringEnabled ? "연속 녹화" : "녹화 안 함"}
                  </span>
                  <button type="button" onClick={() => setTab("events")}>
                    확인할 사건 {openIncidents.length}건
                  </button>
                </div>
              </div>
            </div>}

            <div className="homecam-quick-grid">
              <article className="homecam-live-state-card">
                <h2>현재 상태</h2>
                <div className="homecam-live-state-list">
                  <div>
                    <i className={displayedMediaReady ? "is-good" : ""} aria-hidden="true" />
                    <span>영상 연결</span>
                    <strong>{displayedMediaReady ? "연결됨" : selectedDevice?.online ? "연결 중" : "오프라인"}</strong>
                  </div>
                  <div>
                    <i className={selectedDevice?.cameraEnabled ? "is-good" : ""} aria-hidden="true" />
                    <span>카메라 전원</span>
                    <strong>{selectedDevice?.cameraEnabled ? "켜짐" : "꺼짐"}</strong>
                    {selectedDevice && (
                      <Switch
                        checked={selectedDevice.cameraEnabled}
                        disabled={!isOwner || Boolean(busy)}
                        label="카메라 전원"
                        onChange={(value) => void updateSetting("cameraEnabled", value)}
                      />
                    )}
                  </div>
                  <div>
                    <i className={selectedDevice?.microphoneEnabled ? "is-good" : ""} aria-hidden="true" />
                    <span>보호자 마이크</span>
                    <strong>{selectedDevice?.microphoneEnabled ? "사용 가능" : "꺼짐"}</strong>
                  </div>
                  <div>
                    <i
                      className={
                        storageReady
                          ? "is-good"
                          : storageConnecting
                            ? "is-pending"
                            : storageError
                              ? "is-error"
                              : ""
                      }
                      aria-hidden="true"
                    />
                    <span>영상 저장</span>
                    <strong aria-live="polite">
                      {!storageEnabled
                        ? "안 함"
                        : !selectedDevice?.cameraEnabled
                          ? "카메라 꺼짐"
                        : storageReady
                          ? "저장 중"
                          : storageConnecting
                            ? "준비 중"
                            : "저장 오류"}
                    </strong>
                    {selectedDevice && (
                      <Switch
                        checked={selectedDevice.monitoringEnabled}
                        disabled={!isOwner || Boolean(busy)}
                        label="연속 녹화"
                        onChange={(value) => void updateSetting("monitoringEnabled", value)}
                      />
                    )}
                  </div>
                </div>
              </article>
              <article className="homecam-live-recent-card">
                <div><h2>확인이 필요한 사건</h2><button type="button" onClick={() => setTab("events")}>전체 보기</button></div>
                <section>
                  {openIncidents.slice(0, 2).map((incident) => (
                    <button type="button" key={incident.incidentId} onClick={() => { setFocusedIncidentId(incident.incidentId); setTab("events"); }}>
                      <span className="fall-incident-icon is-check"><Warning size={18} weight="bold" /></span>
                      <small>{formatIncidentTime(incident.occurredAt)}</small>
                      <strong>{incident.fallSeen ? "낙상" : "낙상 의심"}</strong>
                    </button>
                  ))}
                  {openIncidents.length === 0 && <p>확인할 사건이 없어요.</p>}
                </section>
              </article>
              <div className="homecam-live-side-actions">
                <button type="button" onClick={() => void openLive()} disabled={!selectedDevice?.online || !selectedDevice.cameraEnabled || busy === "live"}>
                  <ArrowClockwise size={16} weight="bold" aria-hidden="true" />
                  {liveViewer ? "연결 재시도" : "실시간 연결"}
                </button>
              </div>
            </div>
          </section>
        )}

        {tab === "events" && (
          <section className="homecam-section" aria-labelledby="homecam-events-title">
            <h1 id="homecam-events-title" className="sr-only">사건</h1>
            {selectedDevice && timelineMode ? (
              <FallTimelinePanel
                key={`${selectedDevice.id}-${timelineMode.kind}`}
                deviceId={selectedDevice.id}
                demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
                mode={timelineMode}
                onBack={() => setTimelineMode(null)}
                onOpenIncident={(incidentId) => { setFocusedIncidentId(incidentId); setTimelineMode(null); }}
              />
            ) : selectedDevice ? (
              <FallIncidentsPanel
                key={selectedDevice.id}
                deviceId={selectedDevice.id}
                demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
                initialIncidentId={focusedIncidentId || undefined}
                onIncidentChange={(incidentId) => setFocusedIncidentId(incidentId ?? "")}
                onOpenLive={() => void openLive()}
                onOpenTimeline={(incident) => setTimelineMode(incident ? { kind: "recheck", ...incident } : { kind: "report" })}
              />
            ) : (
              <div className="homecam-empty-state"><strong>등록된 말벗이 없어요</strong><p>로봇을 연결하면 낙상 사건이 여기에 표시됩니다.</p></div>
            )}
          </section>
        )}

        {tab === "map" && (
          LOCAL_HOME_CAM_DEMO && selectedDevice?.id === LOCAL_DEMO_DEVICE_ID
            ? <LocalDemoMapPanel mode={mapEntryMode} onModeChange={setMapEntryMode} />
            : <RobotMapPanel key={mapEntryMode} device={selectedDevice} initialMode={mapEntryMode} />
        )}

        {tab === "robot" && (
          <RobotMapPanel key={`robot-${selectedDevice?.id ?? ""}`} device={selectedDevice} controlsMode="managed" />
        )}

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
                <button type="button">낙상 감지</button>
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
                    <p>홈·홈캠·사건·지도·설정 화면의 밝기를 선택합니다.</p>
                  </div>
                </div>
                <div className="homecam-theme-options" role="group" aria-label="화면 모드 선택">
                  <button type="button" className={colorMode === "light" ? "is-active" : ""} onClick={() => {
                    window.localStorage.setItem("malbut-color-mode", "light");
                    setColorMode("light");
                  }}>
                    <Sun size={19} weight="regular" aria-hidden="true" />
                    라이트 모드
                  </button>
                  <button type="button" className={colorMode === "dark" ? "is-active" : ""} onClick={() => {
                    window.localStorage.setItem("malbut-color-mode", "dark");
                    setColorMode("dark");
                  }}>
                    <Moon size={19} weight="regular" aria-hidden="true" />
                    다크 모드
                  </button>
                </div>
                <div className="homecam-setting-row homecam-text-size-row">
                  <div>
                    <strong>큰 글자</strong>
                    <span>화면 전체 글자를 키웁니다 · 본문 16→18px</span>
                  </div>
                  <div className="homecam-theme-options" role="group" aria-label="글자 크기 선택">
                    <button type="button" className={textSize === "default" ? "is-active" : ""} onClick={() => {
                      window.localStorage.setItem("malbut-text-size", "default");
                      setTextSize("default");
                    }}>
                      <TextAa size={19} weight="regular" aria-hidden="true" />
                      기본
                    </button>
                    <button type="button" className={textSize === "large" ? "is-active" : ""} onClick={() => {
                      window.localStorage.setItem("malbut-text-size", "large");
                      setTextSize("large");
                    }}>
                      <TextAa size={22} weight="bold" aria-hidden="true" />
                      크게
                    </button>
                  </div>
                </div>
              </section>

              {selectedDevice && (
                <FallHomecamSettings
                  key={selectedDevice.id}
                  deviceId={selectedDevice.id}
                  demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
                  isOwner={isOwner}
                  cameraEnabled={selectedDevice.cameraEnabled}
                  recordingEnabled={selectedDevice.monitoringEnabled}
                  microphoneEnabled={selectedDevice.microphoneEnabled}
                  settingBusy={Boolean(busy)}
                  onUpdateSetting={(settingKey, value) => void updateSetting(settingKey, value)}
                />
              )}

              <section className="homecam-settings-card">
                <div className="settings-card-heading">
                  <span className="settings-heading-icon" aria-hidden="true">
                    <Bell size={21} weight="regular" />
                  </span>
                  <div>
                    <h2>낙상 알림</h2>
                    <p>알림에는 사진 없이 단계와 시각만 표시합니다.</p>
                  </div>
                </div>
                <div className="homecam-setting-row">
                  <div><strong>Web Push</strong><span>넘어짐이 의심되면 알려드려요. 아무도 확인하지 않으면 [재발신]해요.</span></div>
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

    </div>
  );
}

function decodeBase64Url(value: string) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}
