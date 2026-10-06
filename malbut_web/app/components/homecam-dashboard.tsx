"use client";

import Image from "next/image";
import { FallHomecamSettings } from "./fall-homecam-settings";
import { FallIncidentsPanel } from "./fall-incidents-panel";
import { FallTimelinePanel, type TimelineMode } from "./fall-timeline-panel";
import { subscribeFallPush } from "../lib/fall-push";
import { GuardiansSettings, OwnerTransferCard, type FamilyMember } from "./guardians-settings";
import { KeyHealthNotice } from "./key-health-notice";
import { ServiceKeysSettings } from "./service-keys-settings";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  CaretRight,
  CheckCircle,
  CornersOut,
  Info,
  MapTrifold,
  ShieldCheck,
  VideoCamera,
  X,
} from "@phosphor-icons/react";
import { type HomecamTab, useHomecamAuth } from "./homecam-header";
import { UiTabBar } from "./ui-tab-bar";
import { DisplayNameForm } from "./display-name-form";
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
// Local UI demo only: the people in 목업 Guardians.
const LOCAL_DEMO_FAMILY: FamilyMember[] = [
  { id: "demo-owner", name: "김말벗", role: "owner", provider: "kakao", joinedAt: "2026-10-01T00:00:00.000Z", viaInvite: false },
  { id: "demo-g1", name: "이보호", role: "family", provider: "google", joinedAt: "2026-10-03T03:00:00.000Z", viaInvite: true },
  { id: "demo-g2", name: "박돌봄", role: "family", provider: "naver", joinedAt: new Date().toISOString(), viaInvite: true },
];

type ApiAvailability = "loading" | "ready" | "unavailable";

type HomecamDashboardProps = {
  initialTab?: HomecamTab;
  onOpenLive: (device: HomecamDevice) => Promise<void>;
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
    <article className="ui-card ui-map-card">
      <div className="ui-card-head">
        <h2>지도</h2>
        <button type="button" className="ui-text-button" onClick={() => onOpenMap("view")}>지도 열기</button>
      </div>
      <button type="button" className="homecam-home-map-preview ui-map-preview" onClick={() => onOpenMap("view")}
        aria-label="우리 집 지도 열기">
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
          <span className="ui-hint">저장된 지도를 확인하고 있어요</span>
        )}
      </button>
      {visibleRooms.length === 0 ? (
        <p className="ui-hint">방을 나누고 이름을 정하면 여기에 표시돼요.</p>
      ) : (
        <div className="ui-rooms" aria-label="방 이름으로 보내기">
          {visibleRooms.slice(0, 4).map((room) => (
            <button type="button" key={room.id} style={{ background: room.color }}
              onClick={() => onOpenMap("navigate")} aria-label={`${room.name}(으)로 보내기`}>
              {room.name}
            </button>
          ))}
        </div>
      )}
    </article>
  );
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

const LOGIN_METHOD_COPY: Record<string, string> = {
  kakao: "카카오로 로그인 중",
  naver: "네이버로 로그인 중",
  google: "Google로 로그인 중",
  email: "이메일로 로그인 중",
};

function loginMethodCopy(providers: string[]) {
  const first = ["kakao", "naver", "google", "email"].find((provider) => providers.includes(provider));
  return first ? LOGIN_METHOD_COPY[first] : "로그인 정보를 확인하고 있어요";
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
      className={`fall-switch ${checked ? "is-on" : ""}`}
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
  const [pushEnabled, setPushEnabled] = useState(false);
  const [pushSubscriptionId, setPushSubscriptionId] = useState("");
  const [pushEndpointRegistrationCount, setPushEndpointRegistrationCount] = useState(0);
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [standalone, setStandalone] = useState(false);
  const [settingsView, setSettingsView] = useState<"main" | "homecam" | "guardians" | "keys" | "owner" | "name">("main");
  const [account, setAccount] = useState<{ userId: string | null; name: string | null; email: string | null; providers: string[] } | null>(null);
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
  const { authStatus, signingOut, signOut } = useHomecamAuth();

  useEffect(() => {
    if (!authStatus?.authenticated) return;
    const controller = new AbortController();
    void fetch("/api/account", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return;
        const payload = asRecord(await response.json().catch(() => ({})));
        setAccount({
          userId: stringValue(payload.userId) ?? null,
          name: stringValue(payload.displayName) ?? null,
          email: stringValue(payload.email) ?? null,
          providers: Array.isArray(payload.providers)
            ? payload.providers.filter((value): value is string => typeof value === "string")
            : [],
        });
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [authStatus?.authenticated]);
  const localDemoAutoplayRef = useRef(false);
  const navigationStateReadyRef = useRef(false);
  const openMap = useCallback((mode: MapMode) => {
    setMapEntryMode(mode);
    setTab("map");
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
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "등록된 말벗을 불러오지 못했습니다.");
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
            : "말벗 API가 아직 연결되지 않았습니다.",
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
    if (LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID) {
      setFamily((current) => current.length ? current : LOCAL_DEMO_FAMILY);
      return;
    }
    setFamilyLoading(true);
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/family`,
        { cache: "no-store" },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "보호자 목록을 불러오지 못했습니다.");
      const rawMembers = Array.isArray(payload.members)
        ? payload.members
        : Array.isArray(payload.family)
          ? payload.family
          : [];
      setFamily(
        rawMembers.flatMap((value) => {
          const raw = asRecord(value);
          const id = stringValue(raw.userId);
          const name = stringValue(raw.name) ?? "이름 없는 사용자";
          const roleValue = stringValue(raw.role);
          if (!id || (roleValue !== "owner" && roleValue !== "family")) return [];
          return [{
            id, name, role: roleValue,
            provider: stringValue(raw.provider) ?? null,
            joinedAt: stringValue(raw.createdAt) ?? "",
            viaInvite: raw.viaInvite === true,
          }];
        }),
      );
    } catch (reason) {
      setNotice(
        reason instanceof Error
          ? reason.message
          : "보호자 관리 API가 아직 연결되지 않았습니다.",
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
        setNotice("이 말벗의 알림을 껐습니다.");
        return;
      }

      if (!selectedDevice) throw new Error("알림을 받을 홈캠을 먼저 선택해 주세요.");
      const saved = await subscribeFallPush(selectedDevice.id);
      setPushEnabled(true);
      setPushEndpointRegistrationCount((count) => Math.max(1, count + 1));
      setPushSubscriptionId(saved.subscriptionId);
      setNotice("사람·반려동물·움직임 알림을 받을 수 있습니다.");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "알림 설정에 실패했습니다.");
    } finally {
      setBusy("");
    }
  };

  const removeFamily = async (member: FamilyMember) => {
    if (!selectedDevice || busy) return false;
    if (LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID) {
      setFamily((current) => current.filter((item) => item.id !== member.id));
      return true;
    }
    setBusy(`family:${member.id}`);
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(selectedDevice.id)}/family`,
        {
          method: "DELETE",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ userId: member.id }),
        },
      );
      const payload = asRecord(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(stringValue(payload.error) ?? "보호자 권한을 해제하지 못했습니다.");
      setFamily((current) => current.filter((item) => item.id !== member.id));
      return true;
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "보호자 권한을 해제하지 못했습니다.");
      return false;
    } finally {
      setBusy("");
    }
  };

  const ownerTransferred = async (member: FamilyMember) => {
    if (LOCAL_HOME_CAM_DEMO && selectedDevice?.id === LOCAL_DEMO_DEVICE_ID) {
      setFamily((current) => current.map((item) => ({
        ...item, role: item.id === member.id ? "owner" : item.role === "owner" ? "family" : item.role,
      })));
      setDevices((current) => current.map((device) => device.id === LOCAL_DEMO_DEVICE_ID ? { ...device, role: "family" } : device));
      return;
    }
    await Promise.all([loadDevices(true), loadFamily()]);
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
  const isGuardianView = selectedDevice?.role !== "owner";
  const roleLabel = selectedDevice?.role === "owner" ? "소유자" : selectedDevice?.role === "family" ? "보호자" : "읽기 전용";
  const connectionText = selectedDevice?.online
    ? tab === "live" && displayedMediaReady ? "실시간 연결됨" : "연결됨"
    : "오프라인";
  // 홈 영상 카드: 지금 볼 수 없으면 빨간 "실시간" 대신 그 이유를 회색으로.
  const homeLiveBlocked = !selectedDevice?.online ? "오프라인" : !selectedDevice.cameraEnabled ? "카메라 꺼짐" : null;
  const showTopBar = tab === "home" || tab === "live" || (tab === "settings" && settingsView === "main");
  const navigate = (nextTab: HomecamTab) => {
    if (nextTab === "map") openMap("view");
    else {
      if (nextTab === "settings") setSettingsView("main");
      setTab(nextTab);
    }
  };
  const settingsBack = (title: string) => (
    <div className="ui-subhead">
      <button type="button" className="ui-back" onClick={() => setSettingsView("main")}>‹ 설정</button>
      <h1>{title}</h1>
    </div>
  );

  return (
    <div className={`homecam-shell homecam-dashboard-shell ui-app tab-${tab}`}>
      <main className="homecam-main ui-main">
        {showTopBar && (
          <header className="ui-top">
            <div className="ui-top-row">
              <h1>{tab === "home" ? "홈" : tab === "live" ? "홈캠" : "설정"}</h1>
              <span className={`ui-pill ${selectedDevice?.online ? "is-ok" : ""}`}><i aria-hidden="true" />{connectionText}</span>
            </div>
            {/* 홈·홈캠·설정 모두 같은 머리. 홈캠의 영상·녹화 상태는 아래 "현재 상태" 카드에 있다. */}
            <div className="ui-device">
              <span className="ui-device-avatar" aria-hidden="true">말</span>
              <span className="ui-device-text">
                <label htmlFor="homecam-device-select">연결된 말벗</label>
                {devices.length > 1 ? (
                  <select id="homecam-device-select" value={selectedDevice?.id ?? ""}
                    onChange={(event) => setSelectedDeviceId(event.target.value)}>
                    {devices.map((device) => (
                      <option key={device.id} value={device.id}>{device.displayName}</option>
                    ))}
                  </select>
                ) : (
                  <strong>{selectedDevice?.displayName ?? "등록된 말벗 없음"}</strong>
                )}
              </span>
            </div>
          </header>
        )}

        {availability === "loading" && (
          <div className="ui-loading" role="status">
            <span aria-hidden="true" />
            등록된 말벗을 확인하고 있어요.
          </div>
        )}

        {tab === "home" && availability === "ready" && devices.length === 0 && (
          <section className="ui-screen" aria-label="말벗 등록">
            <article className="ui-card">
              <h2>아직 연결된 말벗이 없어요</h2>
              <p className="ui-hint ui-long">등록 코드를 입력하면 이 계정이 말벗의 소유자가 돼요. 소유자는 설정을 바꾸고 보호자를 초대할 수 있어요.</p>
              <a className="ui-button is-strong" href="/register">등록 코드 입력하기</a>
            </article>
          </section>
        )}

        {tab === "home" && !(availability === "ready" && devices.length === 0) && (
          <section className="ui-screen ui-home" aria-label="말벗 지금 상태">
            {selectedDevice && (
              <KeyHealthNotice
                key={selectedDevice.id}
                deviceId={selectedDevice.id}
                isOwner={isOwner}
                demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
                onOpenKeys={() => { setSettingsView("keys"); setTab("settings"); }}
              />
            )}
            <article className="ui-card ui-hero">
              <span className="ui-caption">지금 말벗은</span>
              <strong className="ui-hero-title">
                {selectedDevice?.online ? "집 안에서 대기하고 있어요" : "연결을 기다리고 있어요"}
              </strong>
              <div className="ui-chips">
                <span className={selectedDevice?.online ? "is-ok" : ""}>{selectedDevice?.online ? "말벗 연결됨" : "말벗 오프라인"}</span>
                <span className={selectedDevice?.p2pHealthy ? "is-ok" : ""}>{selectedDevice?.p2pHealthy ? "실시간 영상 준비됨" : "영상 연결 준비 중"}</span>
                <span className={selectedDevice?.online ? "is-ok" : ""}>{selectedDevice?.cameraEnabled ? "카메라 켜짐" : "카메라 꺼짐"} · {selectedDevice?.microphoneEnabled ? "마이크 켜짐" : "마이크 꺼짐"}</span>
              </div>
              <div className="ui-two-buttons">
                <button type="button" className="ui-button is-strong" onClick={() => setTab("live")}>홈캠 열기</button>
                <button type="button" className="ui-button" onClick={() => openMap("navigate")}>지도에서 보내기</button>
              </div>
            </article>

            <button type="button" className="ui-card ui-camera-card" onClick={() => setTab("live")} aria-label="거실 실시간 영상 열기">
              <span className="ui-camera-media">
                <span className={`ui-live-tag ${homeLiveBlocked ? "is-off" : ""}`}>● {homeLiveBlocked ?? "실시간"}</span>
                <VideoCamera size={44} weight="light" aria-hidden="true" />
              </span>
              <span className="ui-camera-text">
                <span>
                  <strong>{selectedDevice?.online ? "거실 실시간 영상 열기" : "홈캠 연결 상태 확인"}</strong>
                  <small>보호자 계정으로 안전하게 연결합니다</small>
                </span>
                <span className="ui-link-text">크게 보기</span>
              </span>
            </button>

            <article className="ui-card ui-incidents">
              <div className="ui-card-head">
                <h2>확인이 필요해요 <span>{openIncidents.length}건</span></h2>
                <button type="button" className="ui-text-button" onClick={() => setTab("events")}>전체 보기</button>
              </div>
              {incidentsLoading && openIncidents.length === 0 && <p className="ui-hint">사건을 확인하고 있어요…</p>}
              {!incidentsLoading && openIncidents.length === 0 && (
                <p className="ui-safe"><CheckCircle size={20} weight="bold" aria-hidden="true" /> 확인할 사건이 없어요</p>
              )}
              {openIncidents.slice(0, 3).map((incident) => (
                <button type="button" key={incident.incidentId}
                  className={`ui-incident ${incident.unacknowledged ? "is-urgent" : ""}`}
                  onClick={() => { setFocusedIncidentId(incident.incidentId); setTab("events"); }}>
                  <span className={`ui-badge ${incident.unacknowledged ? "is-danger" : "is-warn"}`}>
                    {incident.unacknowledged ? "아무도 확인하지 않음" : "확인 필요"}
                  </span>
                  <strong>{incident.fallSeen ? "낙상" : "낙상 의심"}</strong>
                  <small>{formatIncidentTime(incident.occurredAt)}{incident.aiFailed ? " · AI 판정 실패" : ""}</small>
                </button>
              ))}
            </article>

            <HomeMapSummary device={selectedDevice} onOpenMap={openMap} />

            <article className="ui-card ui-privacy">
              <ShieldCheck size={24} weight="regular" aria-hidden="true" />
              <span><small>개인정보</small><strong>{selectedDevice?.monitoringEnabled ? "연속 녹화로 저장하고 있어요 (7일 보관)" : "영상 저장을 사용하지 않아요"}</strong></span>
              <button type="button" className="ui-text-button" onClick={() => { setSettingsView("homecam"); setTab("settings"); }}>저장 설정</button>
            </article>
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
            }) ?? (
              <div className="ui-video">
                <div className="ui-video-tags">
                  <span>{selectedDevice?.cameraEnabled ? "카메라 켜짐" : "카메라 꺼짐"}</span>
                  <span>{selectedDevice?.monitoringEnabled ? "연속 녹화" : "녹화 안 함"}</span>
                </div>
                <VideoCamera size={38} weight="light" aria-hidden="true" />
                <strong>
                  {!selectedDevice
                    ? "홈캠을 연결해 주세요"
                    : !selectedDevice.cameraEnabled
                      ? "카메라가 꺼져 있어요"
                      : selectedDevice.online
                        ? "보안 채널 연결 대기 중"
                        : "홈캠이 오프라인이에요"}
                </strong>
                <span className="ui-video-clock">{formatLiveClock(liveClockMs)}</span>
              </div>
            )}

            <div className="homecam-quick-grid ui-live-cards">
              <button type="button" className="ui-button is-strong ui-wide" onClick={() => void openLive()}
                disabled={!selectedDevice?.online || !selectedDevice.cameraEnabled || busy === "live"}>
                <ArrowClockwise size={16} weight="bold" aria-hidden="true" />
                {busy === "live" ? "연결 중" : liveViewer ? "연결 재시도" : "실시간 연결"}
              </button>

              <article className="ui-card ui-rows">
                <h2>현재 상태</h2>
                <div>
                  <span>영상 연결</span>
                  <strong className={displayedMediaReady ? "is-good" : ""}>{displayedMediaReady ? "연결됨" : selectedDevice?.online ? "연결 중" : "오프라인"}</strong>
                </div>
                <div>
                  <span>카메라 전원</span>
                  {/* 보면서 바로 끄는 것은 카메라뿐: 연속 녹화는 설정 › 홈캠 설정에서 바꾼다. */}
                  {selectedDevice && (isOwner ? (
                    <Switch
                      checked={selectedDevice.cameraEnabled}
                      disabled={Boolean(busy)}
                      label="카메라 전원"
                      onChange={(value) => void updateSetting("cameraEnabled", value)}
                    />
                  ) : (
                    <strong className={selectedDevice.cameraEnabled ? "is-good" : ""}>{selectedDevice.cameraEnabled ? "켜짐" : "꺼짐"}</strong>
                  ))}
                </div>
                <div>
                  <span>보호자 마이크</span>
                  <strong className={selectedDevice?.microphoneEnabled ? "is-good" : ""}>{selectedDevice?.microphoneEnabled ? "사용 가능" : "꺼짐"}</strong>
                </div>
                <div>
                  <span>영상 저장</span>
                  <strong
                    aria-live="polite"
                    className={
                      storageReady
                        ? "is-good"
                        : storageConnecting
                          ? "is-pending"
                          : storageError
                            ? "is-error"
                            : ""
                    }
                  >
                    {!storageEnabled
                      ? "안 함"
                      : !selectedDevice?.cameraEnabled
                        ? "카메라 꺼짐"
                      : storageReady
                        ? "저장 중 · 연속 녹화"
                        : storageConnecting
                          ? "준비 중"
                          : "저장 오류"}
                  </strong>
                </div>
              </article>

              <article className="ui-card ui-incidents">
                <div className="ui-card-head">
                  <h2>확인이 필요한 사건</h2>
                  <button type="button" className="ui-text-button" onClick={() => setTab("events")}>전체 보기</button>
                </div>
                {openIncidents.length === 0 && <p className="ui-hint">확인할 사건이 없어요.</p>}
                {openIncidents.slice(0, 2).map((incident) => (
                  <button type="button" key={incident.incidentId}
                    className={`ui-incident ${incident.unacknowledged ? "is-urgent" : ""}`}
                    onClick={() => { setFocusedIncidentId(incident.incidentId); setTab("events"); }}>
                    <span className={`ui-badge ${incident.unacknowledged ? "is-danger" : "is-warn"}`}>
                      {incident.unacknowledged ? "아무도 확인하지 않음" : "확인 필요"}
                    </span>
                    <strong>{incident.fallSeen ? "낙상" : "낙상 의심"}</strong>
                    <small>{formatIncidentTime(incident.occurredAt)}</small>
                  </button>
                ))}
              </article>
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
              <div className="homecam-empty-state"><strong>등록된 말벗이 없어요</strong><p>말벗을 연결하면 낙상 사건이 여기에 표시됩니다.</p></div>
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

        {tab === "settings" && settingsView === "main" && (
          <section className="ui-screen ui-settings" aria-label="설정">
            <article className="ui-card ui-account">
              <span className="ui-account-avatar" aria-hidden="true">{account?.name ? [...account.name][0] : authStatus?.authenticated ? "나" : "?"}</span>
              <span className="ui-account-text">
                <strong>{account?.name ?? account?.email ?? (authStatus?.authenticated ? "로그인한 계정" : "로그인 상태 확인 중")} <span className={`ui-badge ${isOwner ? "is-accent" : ""}`}>{roleLabel}</span></strong>
                <small>{loginMethodCopy(account?.providers ?? [])}</small>
              </span>
            </article>
            <div className="ui-two-buttons ui-account-actions">
              <button type="button" className="ui-button" onClick={() => setSettingsView("name")}
                disabled={!authStatus?.authenticated}>
                이름 바꾸기
              </button>
              <button type="button" className="ui-button" onClick={() => void signOut()}
                disabled={!authStatus?.authenticated || signingOut}>
                {signingOut ? "로그아웃 중" : "로그아웃"}
              </button>
            </div>

            {isGuardianView && <p className="ui-info">설정은 소유자만 바꿀 수 있어요. 지금 상태만 보여요.</p>}

            <div className="ui-group">
              <span className="ui-group-title">우리 집 말벗</span>
              <div className="ui-card ui-list">
                <button type="button" onClick={() => setSettingsView("homecam")}>
                  <span><strong>홈캠 설정</strong><small>카메라 · 연속 녹화 · 낙상 감지 · 클라우드 AI · 말벗 마이크</small></span>
                  <CaretRight size={18} aria-hidden="true" />
                </button>
                <button type="button" onClick={() => setSettingsView("guardians")}>
                  <span><strong>보호자</strong><small>{isOwner ? `함께 보는 사람 ${family.filter((member) => member.role === "family").length}명 · 초대 링크 만들기` : "함께 보는 사람 보기"}</small></span>
                  <CaretRight size={18} aria-hidden="true" />
                </button>
                {isOwner && (
                  <button type="button" onClick={() => setSettingsView("keys")}>
                    <span><strong>AI·서비스 키</strong><small>대화 · 날씨 · 낙상 AI 확인에 쓰는 키</small></span>
                    <CaretRight size={18} aria-hidden="true" />
                  </button>
                )}
                {isOwner && (
                  <button type="button" onClick={() => setSettingsView("owner")}>
                    <span><strong>소유자 넘기기 · 다시 등록</strong><small>관리를 다른 보호자에게 맡기거나 말벗을 옮길 때</small></span>
                    <CaretRight size={18} aria-hidden="true" />
                  </button>
                )}
              </div>
            </div>

            <div className="ui-group">
              <span className="ui-group-title">알림</span>
              <article className="ui-card ui-setting">
                <div className="ui-setting-row">
                  <span><strong>낙상 알림</strong><small>낙상이 의심되면 알려드려요. 아무도 확인하지 않으면 [재발신]해요.</small></span>
                  <Switch
                    checked={pushEnabled}
                    disabled={busy === "push"}
                    label="낙상 알림"
                    onChange={() => void togglePush()}
                  />
                </div>
                <small className="ui-note">알림에는 사진 없이 단계와 시각만 표시해요. 이 휴대폰에만 적용돼요.</small>
                <small className="ui-note">iPhone·iPad는 이 사이트를 홈 화면에 설치한 뒤 알림을 켤 수 있어요.</small>
              </article>
            </div>

            <div className="ui-group">
              <span className="ui-group-title">화면</span>
              <article className="ui-card ui-setting">
                <span><strong>큰 글자</strong><small>화면 전체 글자를 키워요 · 본문 16→18px</small></span>
                <div className="ui-segment" role="group" aria-label="글자 크기">
                  {(["default", "large"] as const).map((size) => (
                    <button type="button" key={size} aria-pressed={textSize === size}
                      className={textSize === size ? "is-on" : ""}
                      onClick={() => {
                        window.localStorage.setItem("malbut-text-size", size);
                        setTextSize(size);
                      }}>
                      {size === "default" ? "기본" : "크게"}
                    </button>
                  ))}
                </div>
              </article>
            </div>

            <div className="ui-group">
              <span className="ui-group-title">정보</span>
              <div className="ui-card ui-list">
                <div className="ui-list-row"><span>연결된 말벗</span><small>{devices.length}대</small></div>
                {!standalone && installPrompt && (
                  <button type="button" onClick={() => void installApp()}>
                    <span><strong>홈 화면에 설치</strong><small>앱처럼 바로 열고 알림을 받을 수 있어요</small></span>
                    <CaretRight size={18} aria-hidden="true" />
                  </button>
                )}
              </div>
            </div>

            <div className="ui-group">
              <span className="ui-group-title">개발자 메뉴 (디자인 그대로)</span>
              <div className="ui-card ui-list is-dev">
                <button type="button" onClick={() => setTab("robot")}>
                  <span>개발자 화면 (주행·디버그)</span>
                  <CaretRight size={18} aria-hidden="true" />
                </button>
              </div>
            </div>
          </section>
        )}

        {tab === "settings" && settingsView === "homecam" && (
          <section className="ui-screen ui-settings-sub" aria-label="홈캠 설정">
            {settingsBack("홈캠 설정")}
            {selectedDevice ? (
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
            ) : <p className="ui-hint">등록된 말벗이 없어요.</p>}
          </section>
        )}

        {tab === "settings" && settingsView === "keys" && selectedDevice && (
          <ServiceKeysSettings
            key={selectedDevice.id}
            deviceId={selectedDevice.id}
            demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
            onBack={() => setSettingsView("main")}
          />
        )}

        {tab === "settings" && settingsView === "owner" && (
          <section className="ui-screen ui-settings-sub" aria-label="소유자 넘기기 · 다시 등록">
            {settingsBack("소유자 넘기기 · 다시 등록")}
            {selectedDevice && (
              <OwnerTransferCard
                key={selectedDevice.id}
                deviceId={selectedDevice.id}
                family={family}
                demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
                onTransferred={(member) => void ownerTransferred(member)}
              />
            )}
            {isOwner && <article className="ui-card">
              <h2>등록 코드로 다시 등록</h2>
              <p className="ui-hint ui-long">소유자 계정을 쓸 수 없게 됐거나 말벗을 다른 집으로 옮길 때 써요. 새 등록 코드를 입력한 사람이 새 소유자가 되고, 지금의 소유자와 보호자는 모두 지워져요.</p>
              <p className="ui-hint ui-long">지난 사건 기록과 의견을 지울지 남길지는 다시 등록할 때 골라요. 새 등록 코드는 말벗 팀에게 받을 수 있어요.</p>
              <a className="ui-button is-danger-line" href="/register">등록 코드 입력하기</a>
            </article>}
          </section>
        )}

        {tab === "settings" && settingsView === "name" && (
          <section className="ui-screen ui-settings-sub" aria-label="이름 바꾸기">
            {settingsBack("이름 바꾸기")}
            <article className="ui-card">
              <p className="ui-hint">함께 보는 다른 보호자에게 이 이름으로 보여요. 사건에 남긴 의견이나 처리한 사람도 이 이름으로 표시돼요.</p>
              <DisplayNameForm
                initialName={account?.name ?? ""}
                submitLabel="저장"
                onSaved={(name) => {
                  setAccount((current) => ({ userId: null, email: null, providers: [], ...current, name }));
                  setSettingsView("main");
                  setNotice("이름을 바꿨어요.");
                }}
              />
            </article>
          </section>
        )}

        {tab === "settings" && settingsView === "guardians" && selectedDevice && (
          <GuardiansSettings
            key={selectedDevice.id}
            deviceId={selectedDevice.id}
            isOwner={isOwner}
            myUserId={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID ? LOCAL_DEMO_FAMILY[0].id : account?.userId ?? null}
            family={family}
            loading={familyLoading}
            busy={Boolean(busy)}
            demo={LOCAL_HOME_CAM_DEMO && selectedDevice.id === LOCAL_DEMO_DEVICE_ID}
            onBack={() => setSettingsView("main")}
            onRemove={removeFamily}
          />
        )}

        {notice && (tab !== "live" || selectedDevice) && (
          <div className="ui-notice" role="status">
            <Info size={18} weight="bold" aria-hidden="true" />
            <p>{notice}</p>
            {availability === "unavailable" && (
              <button type="button" className="ui-text-button" onClick={() => void loadDevices()}>다시 확인</button>
            )}
          </div>
        )}
      </main>
      <UiTabBar activeTab={tab} onNavigate={navigate} />
    </div>
  );
}
