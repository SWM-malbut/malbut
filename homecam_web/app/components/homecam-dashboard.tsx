"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Bell,
  Camera,
  CaretRight,
  Cat,
  CheckCircle,
  ClockCounterClockwise,
  Dog,
  Info,
  Person,
  Play,
  ShieldCheck,
  UsersThree,
  VideoCamera,
  Waveform,
  X,
} from "@phosphor-icons/react";
import {
  HomecamHeader,
  type HomecamTab,
} from "./homecam-header";
import { RobotMapPanel } from "./robot-map-panel";

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
};

type FamilyMember = {
  id: string;
  email: string;
  role: "owner" | "family";
};

type ApiAvailability = "loading" | "ready" | "unavailable";

type HomecamDashboardProps = {
  initialTab?: HomecamTab;
  onOpenLive: (device: HomecamDevice) => Promise<void>;
  onCreateLegacyBroadcast: () => Promise<void>;
  onJoinLegacy: (roomCode: string, password: string) => void;
  creatingLegacyBroadcast: boolean;
  externalError?: string;
  legacyArchive?: React.ReactNode;
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
  };
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

function StatePill({
  active,
  tone,
  children,
}: {
  active: boolean;
  tone: "live" | "recording" | "camera" | "microphone";
  children: React.ReactNode;
}) {
  return (
    <span className={`homecam-state-pill state-${tone} ${active ? "is-active" : ""}`}>
      <span aria-hidden="true" />
      {children}
    </span>
  );
}

function EventPlayback({
  event,
  onClose,
}: {
  event: HomecamEvent;
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
    if (!video || !event.recordingId) {
      setState("error");
      setMessage("이 이벤트는 연결된 녹화 구간이 아직 없습니다.");
      return () => controller.abort();
    }

    const seekToEvent = () => {
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
    video.addEventListener("loadedmetadata", handleReady);
    video.addEventListener("error", handleError);

    void fetch(`/api/recordings/${encodeURIComponent(event.recordingId)}/playback`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ segment: event.recordingSegment }),
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
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [event]);

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
        {state === "ready" && <p>이벤트가 감지된 시각으로 이동했습니다.</p>}
      </section>
    </div>
  );
}

export function HomecamDashboard({
  initialTab = "live",
  onOpenLive,
  onCreateLegacyBroadcast,
  onJoinLegacy,
  creatingLegacyBroadcast,
  externalError = "",
  legacyArchive,
}: HomecamDashboardProps) {
  const [devices, setDevices] = useState<HomecamDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [tab, setTab] = useState<HomecamTab>(initialTab);
  const [availability, setAvailability] = useState<ApiAvailability>("loading");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [events, setEvents] = useState<HomecamEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventCursor, setEventCursor] = useState<EventCursor | null>(null);
  const [eventFilter, setEventFilter] = useState<HomecamEventType | "all">("all");
  const [selectedEvent, setSelectedEvent] = useState<HomecamEvent | null>(null);
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
  const deepLinkedEventIdRef = useRef("");

  const selectedDevice = useMemo(
    () => devices.find((device) => device.id === selectedDeviceId) ?? devices[0] ?? null,
    [devices, selectedDeviceId],
  );

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
      if (eventFilter !== "all") params.set("type", eventFilter);
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
  }, [eventFilter, selectedDevice]);

  useEffect(() => {
    if (tab === "events") window.queueMicrotask(() => void loadEvents());
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
  const recActive = Boolean(
    selectedDevice?.online &&
    selectedDevice.mediaHealthy &&
    selectedDevice.monitoringEnabled &&
    selectedDevice.activeSession?.storageMode,
  );

  return (
    <div className="homecam-shell">
      <HomecamHeader
        activeTab={tab}
        onNavigate={setTab}
        onInstall={installApp}
        showInstall={!standalone && Boolean(installPrompt)}
      />

      <main className="homecam-main">
        {availability === "loading" && (
          <div className="homecam-loading" role="status">
            <span aria-hidden="true" />
            등록된 홈캠을 확인하고 있습니다.
          </div>
        )}

        {tab === "live" && (
          <section className="homecam-live-view" aria-label="실시간 홈캠">
            <div className="homecam-video-card">
              <div className="homecam-video-frame">
                <div className="homecam-video-topbar">
                  <div className="homecam-state-row" aria-label="홈캠 상태">
                    <StatePill
                      tone="live"
                      active={Boolean(
                        selectedDevice?.online && selectedDevice.mediaHealthy,
                      )}
                    >
                      LIVE
                    </StatePill>
                    <StatePill tone="recording" active={recActive}>REC</StatePill>
                    <StatePill tone="camera" active={Boolean(selectedDevice?.cameraEnabled)}>CAM</StatePill>
                    <StatePill tone="microphone" active={Boolean(selectedDevice?.microphoneEnabled)}>MIC</StatePill>
                  </div>
                  <span>640 × 400 · SECURE</span>
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
                  <span>{selectedDevice?.monitoringEnabled ? "모니터링 켜짐" : "녹화 꺼짐"}</span>
                  <span>영상은 허용된 가족 계정만 볼 수 있어요</span>
                </div>
              </div>
            </div>

            <div className="homecam-quick-grid">
              <article className="homecam-summary-card">
                <span className="summary-icon icon-monitoring" aria-hidden="true">
                  <ShieldCheck size={22} weight="regular" />
                </span>
                <div>
                  <span>모니터링</span>
                  <strong>{selectedDevice?.monitoringEnabled ? "켜짐 · 7일 보관" : "꺼짐"}</strong>
                </div>
                {selectedDevice && (
                  <Switch
                    checked={selectedDevice.monitoringEnabled}
                    disabled={!isOwner || Boolean(busy)}
                    label="모니터링 모드"
                    onChange={(value) => void updateSetting("monitoringEnabled", value)}
                  />
                )}
              </article>
              <button type="button" className="homecam-summary-card is-button" onClick={() => setTab("events")}>
                <span className="summary-icon icon-events" aria-hidden="true">
                  <ClockCounterClockwise size={22} weight="regular" />
                </span>
                <div>
                  <span>최근 이벤트</span>
                  <strong>타임라인 보기</strong>
                </div>
                <CaretRight className="summary-arrow" size={18} weight="bold" aria-hidden="true" />
              </button>
              <button type="button" className="homecam-summary-card is-button" onClick={() => setTab("settings")}>
                <span className="summary-icon icon-family" aria-hidden="true">
                  <UsersThree size={22} weight="regular" />
                </span>
                <div>
                  <span>가족 공유</span>
                  <strong>{family.length > 0 ? `${family.length}명 연결됨` : "계정으로 안전하게"}</strong>
                </div>
                <CaretRight className="summary-arrow" size={18} weight="bold" aria-hidden="true" />
              </button>
            </div>
          </section>
        )}

        {tab === "events" && (
          <section className="homecam-section" aria-labelledby="homecam-events-title">
            <div className="homecam-section-heading">
              <div>
                <span>최근 7일</span>
                <h1 id="homecam-events-title">이벤트 타임라인</h1>
              </div>
              <button type="button" className="homecam-refresh-button" onClick={() => void loadEvents()} disabled={eventsLoading}>
                <ArrowClockwise size={15} weight="bold" aria-hidden="true" />
                {eventsLoading ? "불러오는 중" : "새로고침"}
              </button>
            </div>
            <div className="homecam-filter-row" aria-label="이벤트 종류 필터">
              {(["all", "motion", "person", "dog", "cat"] as const).map((filter) => (
                <button
                  key={filter}
                  type="button"
                  className={eventFilter === filter ? "is-selected" : ""}
                  aria-pressed={eventFilter === filter}
                  onClick={() => setEventFilter(filter)}
                >
                  {filter === "all" ? "전체" : EVENT_LABELS[filter]}
                </button>
              ))}
            </div>
            <div className="homecam-event-list">
              {eventsLoading && events.length === 0 && (
                <div className="homecam-empty-state" role="status">이벤트를 불러오는 중입니다…</div>
              )}
              {!eventsLoading && events.length === 0 && (
                <div className="homecam-empty-state">
                  <CheckCircle size={34} weight="light" aria-hidden="true" />
                  <strong>확인할 이벤트가 없어요</strong>
                  <p>모니터링을 켜면 움직임·사람·강아지·고양이 감지 기록이 표시됩니다.</p>
                </div>
              )}
              {events.map((event) => (
                <button
                  type="button"
                  className="homecam-event-row"
                  key={event.id}
                  onClick={() => setSelectedEvent(event)}
                >
                  <span className={`event-kind-icon kind-${event.type}`} aria-hidden="true">
                    <EventKindIcon type={event.type} />
                  </span>
                  <span>
                    <strong>{EVENT_LABELS[event.type]} 감지</strong>
                    <small>
                      {formatEventTime(event.occurredAt)}
                      {event.confidence !== null ? ` · 신뢰도 ${Math.round(event.confidence * 100)}%` : ""}
                    </small>
                  </span>
                  <span className="event-play-icon" aria-hidden="true">
                    {event.recordingId
                      ? <Play size={14} weight="fill" />
                      : <CaretRight size={16} weight="bold" />}
                  </span>
                </button>
              ))}
              {eventCursor && events.length > 0 && (
                <button
                  type="button"
                  className="homecam-refresh-button"
                  disabled={eventsLoading}
                  onClick={() =>
                    void loadEvents({ append: true, before: eventCursor })
                  }
                >
                  {eventsLoading ? "불러오는 중" : "이전 이벤트 더 보기"}
                </button>
              )}
            </div>
          </section>
        )}

        {tab === "map" && <RobotMapPanel device={selectedDevice} />}

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
            <div className="homecam-settings-grid">
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

      {selectedEvent && <EventPlayback event={selectedEvent} onClose={() => setSelectedEvent(null)} />}
    </div>
  );
}

function decodeBase64Url(value: string) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}
