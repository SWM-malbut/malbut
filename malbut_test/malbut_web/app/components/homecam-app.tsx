"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  HomecamDashboard,
  type HomecamDevice,
  type HomecamTab,
  type LiveSpeaker,
} from "./homecam-dashboard";
import { HomecamHeader } from "./homecam-header";
import {
  TALK_LIMIT_MS,
  formatTalkRemaining,
  type LiveTalk,
  type TalkEnded,
  type TalkHolder,
} from "../homecam-talk";
import {
  ArrowClockwise,
  ArrowLeft,
  CornersOut,
  Microphone,
  ShieldCheck,
  SpeakerHigh,
  SpeakerSlash,
  VideoCamera,
  WifiHigh,
} from "@phosphor-icons/react";
import {
  connectAuthorizedDeviceViewer,
  type KvsConnection,
  type KvsConnectionState,
} from "../lib/kvs-client";
import {
  AUTHORIZED_P2P_CONNECT_TIMEOUT_MS,
  AUTHORIZED_P2P_MEDIA_TIMEOUT_MS,
  AUTHORIZED_P2P_STABLE_LIVE_MS,
  AUTHORIZED_VIEWER_SETUP_TIMEOUT_MS,
  authorizedP2pReconnectDelayMs,
  canAutomaticallyReconnectAuthorizedP2p,
} from "../lib/viewer-reconnect";

type ConnectionState =
  | "idle"
  | "preparing"
  | "waiting"
  | "connecting"
  | "live"
  | "offline"
  | "error";

const VIEWER_AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  video: false,
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
  },
};

const LOCAL_DEMO_DEVICE_ID = "local-demo-homecam";
const LOCAL_HOME_CAM_DEMO =
  process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1";

function drawLocalHomecamDemo(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  elapsedSeconds: number,
) {
  const floorTop = height * 0.57;
  const personX = width * (0.52 + Math.sin(elapsedSeconds * 0.55) * 0.18);
  const robotX = width * (0.46 + Math.sin(elapsedSeconds * 0.32) * 0.08);

  context.fillStyle = "#cbd3cc";
  context.fillRect(0, 0, width, floorTop);
  context.fillStyle = "#8f8066";
  context.fillRect(0, floorTop, width, height - floorTop);

  context.fillStyle = "#e9eee9";
  context.fillRect(width * 0.08, height * 0.1, width * 0.3, height * 0.36);
  context.strokeStyle = "#98a49d";
  context.lineWidth = 4;
  context.strokeRect(width * 0.08, height * 0.1, width * 0.3, height * 0.36);
  context.beginPath();
  context.moveTo(width * 0.23, height * 0.1);
  context.lineTo(width * 0.23, height * 0.46);
  context.moveTo(width * 0.08, height * 0.28);
  context.lineTo(width * 0.38, height * 0.28);
  context.stroke();

  context.fillStyle = "#667269";
  context.fillRect(width * 0.67, height * 0.35, width * 0.25, height * 0.2);
  context.fillStyle = "#4b544e";
  context.fillRect(width * 0.7, height * 0.31, width * 0.19, height * 0.08);

  context.strokeStyle = "rgb(255 255 255 / .18)";
  context.lineWidth = 2;
  for (let index = 0; index < 7; index += 1) {
    const ratio = index / 6;
    context.beginPath();
    context.moveTo(width * ratio, height);
    context.lineTo(width * (0.5 + (ratio - 0.5) * 0.32), floorTop);
    context.stroke();
  }

  context.fillStyle = "#303633";
  context.beginPath();
  context.arc(personX, height * 0.36, 17, 0, Math.PI * 2);
  context.fill();
  context.fillRect(personX - 15, height * 0.4, 30, height * 0.23);
  context.strokeStyle = "#303633";
  context.lineWidth = 12;
  context.beginPath();
  context.moveTo(personX - 7, height * 0.62);
  context.lineTo(personX - 18, height * 0.77);
  context.moveTo(personX + 7, height * 0.62);
  context.lineTo(personX + 18, height * 0.77);
  context.stroke();

  context.fillStyle = "#3b7778";
  context.beginPath();
  context.arc(robotX, height * 0.78, 31, Math.PI, 0);
  context.lineTo(robotX + 31, height * 0.84);
  context.lineTo(robotX - 31, height * 0.84);
  context.closePath();
  context.fill();
  context.fillStyle = "#171b19";
  context.beginPath();
  context.arc(robotX - 22, height * 0.84, 9, 0, Math.PI * 2);
  context.arc(robotX + 22, height * 0.84, 9, 0, Math.PI * 2);
  context.fill();

  context.fillStyle = "rgb(17 24 21 / .72)";
  context.fillRect(14, 14, 126, 32);
  context.fillStyle = "#fff";
  context.font = "600 14px sans-serif";
  context.fillText("LOCAL DEMO · LIVE", 25, 35);
  context.fillStyle = "rgb(255 255 255 / .82)";
  context.font = "500 13px sans-serif";
  context.fillText(
    new Intl.DateTimeFormat("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date()),
    width - 96,
    34,
  );
}

type ViewerTalkLease = {
  leaseId: string;
  clientId: string;
  generation: number;
};

const STATE_COPY: Record<ConnectionState, string> = {
  idle: "준비 전",
  preparing: "카메라 준비 중",
  waiting: "보호자 대기 중",
  connecting: "AWS 연결 중",
  live: "실시간 연결됨",
  offline: "연결 끊김",
  error: "연결 오류",
};

function formatViewerClock(value: number) {
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function Viewer({
  deviceId,
  device,
  onExit,
  embedded = false,
  onMediaReadyChange,
  onSpeakerChange,
  onTalkChange,
}: {
  deviceId: string;
  device?: HomecamDevice;
  onExit: (tab?: HomecamTab) => void;
  embedded?: boolean;
  onMediaReadyChange?: (ready: boolean) => void;
  /** Embedded in the app: the 스피커 switch lives in the 현재 상태 card, outside this viewer. */
  onSpeakerChange?: (speaker: LiveSpeaker | null) => void;
  /** Embedded in the app: the 마이크 switch (my voice to 말벗) lives in the 현재 상태 card. */
  onTalkChange?: (talk: LiveTalk | null) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const microphoneRef = useRef<MediaStream | null>(null);
  const connectionRef = useRef<KvsConnection | null>(null);
  const viewerClientIdRef = useRef("");
  const viewerMountedRef = useRef(true);
  const talkIntentRef = useRef(false);
  const talkAttemptRef = useRef(0);
  const talkLeaseRef = useRef<ViewerTalkLease | null>(null);
  const talkLeaseTimerRef = useRef<number | null>(null);
  // The switch is on while the microphone, reconnect and robot readiness are prepared.
  const talkWantedRef = useRef(false);
  const storageModeRef = useRef<boolean | null>(null);
  const viewerGenerationRef = useRef(0);
  const automaticReconnectAttemptsRef = useRef(0);
  const automaticReconnectTimerRef = useRef<number | null>(null);
  const stableLiveTimerRef = useRef<number | null>(null);
  const reconnectPendingRef = useRef(false);
  const viewerAccessRevokedRef = useRef(false);
  const viewerStateRef = useRef<ConnectionState>("connecting");
  const requestReconnectRef = useRef<
    ((options?: {
      message?: string;
      minimumDelayMs?: number;
    }) => void) | null
  >(null);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  const [microphoneAvailable, setMicrophoneAvailable] = useState(false);
  const [microphonePending, setMicrophonePending] = useState(false);
  const [microphoneNotice, setMicrophoneNotice] = useState("");
  const [talking, setTalking] = useState(false);
  const [talkLeasePending, setTalkLeasePending] = useState(false);
  const [talkWanted, setTalkWanted] = useState(false);
  const [talkHolder, setTalkHolder] = useState<TalkHolder | null>(null);
  const [talkStartedAt, setTalkStartedAt] = useState<number | null>(null);
  const [talkEnded, setTalkEnded] = useState<TalkEnded | null>(null);
  // Why talking stopped, so an unexpected stop (page hidden, reconnect) can say so.
  const talkStopReasonRef = useRef<"user" | "timeout" | null>(null);
  const wasTalkingRef = useRef(false);
  // One switch-on attempt at a time; switching off makes an older attempt stale.
  const talkRunRef = useRef(0);
  const [speakerMuted, setSpeakerMuted] = useState(true);
  const [soundBlocked, setSoundBlocked] = useState(false);
  const [viewerClockMs, setViewerClockMs] = useState(() => Date.now());
  const expectedStorageMode = deviceId
    ? false
    : typeof device?.activeSession?.storageMode === "boolean"
      ? device.activeSession.storageMode
      : null;
  const [storageMode, setStorageMode] = useState<boolean | null>(
    expectedStorageMode,
  );
  const recordingEnabled = deviceId
    ? Boolean(device?.monitoringEnabled)
    : storageMode !== false;
  const localDemoViewer = Boolean(
    LOCAL_HOME_CAM_DEMO && deviceId === LOCAL_DEMO_DEVICE_ID,
  );

  useEffect(() => {
    viewerStateRef.current = state;
    onMediaReadyChange?.(state === "live");
  }, [onMediaReadyChange, state]);

  useEffect(
    () => () => onMediaReadyChange?.(false),
    [onMediaReadyChange],
  );

  useEffect(() => {
    const timer = window.setInterval(() => setViewerClockMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    viewerAccessRevokedRef.current = false;
    automaticReconnectAttemptsRef.current = 0;
    reconnectPendingRef.current = false;
    storageModeRef.current = expectedStorageMode;
  }, [deviceId, expectedStorageMode]);

  const notifyTalkLeaseRelease = useCallback((lease: ViewerTalkLease) => {
    if (deviceId) {
      void fetch(`/api/devices/${encodeURIComponent(deviceId)}/talk-lease`, {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          leaseId: lease.leaseId,
          clientId: lease.clientId,
        }),
        keepalive: true,
      }).catch(() => undefined);
    }
  }, [deviceId]);

  const releaseTalkLease = useCallback((notifyServer = true, updateState = true) => {
    talkIntentRef.current = false;
    talkAttemptRef.current += 1;
    const track = microphoneRef.current?.getAudioTracks()[0];
    if (track) track.enabled = false;
    if (talkLeaseTimerRef.current !== null) {
      window.clearTimeout(talkLeaseTimerRef.current);
      talkLeaseTimerRef.current = null;
    }
    const lease = talkLeaseRef.current;
    talkLeaseRef.current = null;
    if (notifyServer && lease) notifyTalkLeaseRelease(lease);
    if (updateState && viewerMountedRef.current) {
      setTalking(false);
      setTalkLeasePending(false);
    }
  }, [notifyTalkLeaseRelease]);

  useEffect(() => {
    viewerMountedRef.current = true;
    return () => {
      viewerMountedRef.current = false;
      releaseTalkLease(true, false);
      microphoneRef.current?.getTracks().forEach((track) => track.stop());
      microphoneRef.current = null;
    };
  }, [releaseTalkLease]);

  useEffect(() => {
    const handleRelease = () => releaseTalkLease();
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        releaseTalkLease();
        if (
          deviceId &&
          storageModeRef.current === false &&
          automaticReconnectTimerRef.current !== null
        ) {
          window.clearTimeout(automaticReconnectTimerRef.current);
          automaticReconnectTimerRef.current = null;
          reconnectPendingRef.current = true;
        }
        return;
      }
      if (
        deviceId &&
        storageModeRef.current === false &&
        (reconnectPendingRef.current || viewerStateRef.current !== "live")
      ) {
        requestReconnectRef.current?.({
          message: "화면으로 돌아와 홈캠 연결을 다시 확인하고 있습니다.",
        });
      }
    };
    const handleOffline = () => {
      releaseTalkLease();
      if (!deviceId || storageModeRef.current !== false) return;
      if (automaticReconnectTimerRef.current !== null) {
        window.clearTimeout(automaticReconnectTimerRef.current);
        automaticReconnectTimerRef.current = null;
      }
      reconnectPendingRef.current = true;
      viewerStateRef.current = "connecting";
      setState("connecting");
    };
    const handleOnline = () => {
      if (
        deviceId &&
        storageModeRef.current === false &&
        (reconnectPendingRef.current || viewerStateRef.current !== "live")
      ) {
        requestReconnectRef.current?.({
          message: "네트워크가 복구되어 홈캠에 다시 연결하고 있습니다.",
        });
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    // The microphone is a switch, not push-to-talk: focus or a tap elsewhere keeps it on.
    window.addEventListener("pagehide", handleRelease);
    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pagehide", handleRelease);
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
    };
  }, [deviceId, releaseTalkLease]);

  useEffect(() => {
    if (!deviceId || localDemoViewer) return;
    let active = true;
    const verifyAccess = async () => {
      try {
        const response = await fetch(
          `/api/devices/${encodeURIComponent(deviceId)}/live-session`,
          { cache: "no-store" },
        );
        if (!active || (response.status !== 401 && response.status !== 403)) {
          return;
        }
        viewerAccessRevokedRef.current = true;
        viewerGenerationRef.current += 1;
        reconnectPendingRef.current = false;
        if (automaticReconnectTimerRef.current !== null) {
          window.clearTimeout(automaticReconnectTimerRef.current);
          automaticReconnectTimerRef.current = null;
        }
        if (stableLiveTimerRef.current !== null) {
          window.clearTimeout(stableLiveTimerRef.current);
          stableLiveTimerRef.current = null;
        }
        releaseTalkLease();
        connectionRef.current?.close();
        connectionRef.current = null;
        microphoneRef.current?.getTracks().forEach((track) => track.stop());
        microphoneRef.current = null;
        if (videoRef.current) videoRef.current.srcObject = null;
        viewerStateRef.current = "offline";
        setState("offline");
        setError("홈캠 접근 권한이 해제되었습니다.");
      } catch {
        // Ignore transient access-check failures while healthy media is live.
      }
    };
    window.queueMicrotask(() => void verifyAccess());
    const interval = window.setInterval(() => void verifyAccess(), 5_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [deviceId, localDemoViewer, releaseTalkLease]);

  useEffect(() => {
    let active = true;
    const generation = viewerGenerationRef.current + 1;
    viewerGenerationRef.current = generation;
    const videoElement = videoRef.current;
    if (localDemoViewer && videoElement) {
      let demoActive = true;
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 360;
      const context = canvas.getContext("2d");
      if (!context) return;
      const startedAt = performance.now();
      const draw = () => drawLocalHomecamDemo(
        context,
        canvas.width,
        canvas.height,
        (performance.now() - startedAt) / 1_000,
      );
      draw();
      const drawTimer = window.setInterval(draw, 1000 / 15);
      const stream = canvas.captureStream(15);
      videoElement.srcObject = stream;
      videoElement.muted = true;
      storageModeRef.current = false;
      viewerStateRef.current = "live";
      window.queueMicrotask(() => {
        if (!demoActive) return;
        setSpeakerMuted(true);
        setStorageMode(false);
        setError("");
        setState("live");
      });
      void videoElement.play().catch(() => undefined);
      return () => {
        demoActive = false;
        window.clearInterval(drawTimer);
        stream.getTracks().forEach((track) => track.stop());
        if (videoElement.srcObject === stream) videoElement.srcObject = null;
      };
    }
    let remoteStream: MediaStream | null = null;
    let localConnection: KvsConnection | null = null;
    let observedVideoTrack: MediaStreamTrack | null = null;
    let connectionStorageMode: boolean | null = expectedStorageMode;
    let transportLive = false;
    let mediaReady = false;
    let setupTimer: number | null = null;
    let connectTimer: number | null = null;
    let mediaTimer: number | null = null;
    const setupController = deviceId ? new AbortController() : null;
    const localAudioStream = microphoneRef.current ?? undefined;
    localAudioStream?.getAudioTracks().forEach((track) => {
      track.enabled = false;
    });
    // P2P reconnects use a fresh identity so the master cannot confuse the
    // new offer with a retiring peer. Storage is a separate device transport.
    if (storageModeRef.current !== true || !viewerClientIdRef.current) {
      viewerClientIdRef.current = `petcam-${crypto.randomUUID()}`;
    }

    const isCurrentGeneration = () =>
      active && viewerGenerationRef.current === generation;

    const clearSetupTimer = () => {
      if (setupTimer !== null) window.clearTimeout(setupTimer);
      setupTimer = null;
    };

    const clearConnectTimer = () => {
      if (connectTimer !== null) window.clearTimeout(connectTimer);
      connectTimer = null;
    };

    const clearMediaTimer = () => {
      if (mediaTimer !== null) window.clearTimeout(mediaTimer);
      mediaTimer = null;
    };

    const clearStableLiveTimer = () => {
      if (stableLiveTimerRef.current !== null) {
        window.clearTimeout(stableLiveTimerRef.current);
        stableLiveTimerRef.current = null;
      }
    };

    const cancelAutomaticReconnect = () => {
      if (automaticReconnectTimerRef.current !== null) {
        window.clearTimeout(automaticReconnectTimerRef.current);
        automaticReconnectTimerRef.current = null;
      }
      reconnectPendingRef.current = false;
    };

    const scheduleAutomaticReconnect = (
      options: {
        message?: string;
        minimumDelayMs?: number;
      } = {},
    ) => {
      if (
        !isCurrentGeneration() ||
        !deviceId ||
        viewerAccessRevokedRef.current
      ) {
        return;
      }
      const knownStorageMode =
        connectionStorageMode ?? expectedStorageMode ?? storageModeRef.current;
      if (knownStorageMode !== false) return;

      releaseTalkLease();
      clearConnectTimer();
      clearMediaTimer();
      clearStableLiveTimer();
      if (localConnection === null) setupController?.abort();

      if (
        navigator.onLine === false ||
        document.visibilityState === "hidden"
      ) {
        reconnectPendingRef.current = true;
        viewerStateRef.current = "connecting";
        setState("connecting");
        return;
      }
      if (automaticReconnectTimerRef.current !== null) return;

      const completedAttempts = automaticReconnectAttemptsRef.current;
      if (!canAutomaticallyReconnectAuthorizedP2p(completedAttempts)) {
        reconnectPendingRef.current = false;
        viewerStateRef.current = "offline";
        setError(
          "자동 재연결을 여러 번 시도했지만 연결하지 못했습니다. 다시 연결 버튼을 눌러 주세요.",
        );
        setState("offline");
        return;
      }

      const delay = Math.max(
        options.minimumDelayMs ?? 0,
        authorizedP2pReconnectDelayMs(completedAttempts),
      );
      reconnectPendingRef.current = true;
      viewerStateRef.current = "connecting";
      if (options.message) setError(options.message);
      setState("connecting");
      automaticReconnectTimerRef.current = window.setTimeout(() => {
        automaticReconnectTimerRef.current = null;
        if (!isCurrentGeneration()) return;
        if (
          navigator.onLine === false ||
          document.visibilityState === "hidden"
        ) {
          reconnectPendingRef.current = true;
          return;
        }
        reconnectPendingRef.current = false;
        viewerGenerationRef.current += 1;
        automaticReconnectAttemptsRef.current += 1;
        viewerStateRef.current = "connecting";
        setState("connecting");
        setStorageMode(null);
        setAttempt((value) => value + 1);
      }, delay);
    };
    requestReconnectRef.current = scheduleAutomaticReconnect;

    const startMediaTimer = () => {
      clearMediaTimer();
      if (
        !deviceId ||
        (connectionStorageMode ?? storageModeRef.current) !== false
      ) {
        return;
      }
      mediaTimer = window.setTimeout(() => {
        mediaTimer = null;
        scheduleAutomaticReconnect({
          message: "영상 수신이 지연되어 홈캠에 다시 연결하고 있습니다.",
        });
      }, AUTHORIZED_P2P_MEDIA_TIMEOUT_MS);
    };

    const markVideoReady = () => {
      if (
        !videoElement ||
        !observedVideoTrack ||
        observedVideoTrack.readyState !== "live" ||
        videoElement.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
        videoElement.videoWidth <= 0 ||
        videoElement.videoHeight <= 0
      ) {
        return;
      }
      mediaReady = true;
      clearMediaTimer();
      if (isCurrentGeneration() && transportLive) {
        cancelAutomaticReconnect();
        setError("");
        viewerStateRef.current = "live";
        setState("live");
        clearStableLiveTimer();
        stableLiveTimerRef.current = window.setTimeout(() => {
          stableLiveTimerRef.current = null;
          if (
            isCurrentGeneration() &&
            transportLive &&
            mediaReady
          ) {
            automaticReconnectAttemptsRef.current = 0;
          }
        }, AUTHORIZED_P2P_STABLE_LIVE_MS);
      }
    };
    const mediaReadyEvents = [
      "loadedmetadata",
      "loadeddata",
      "canplay",
      "playing",
      "timeupdate",
      "resize",
    ] as const;
    mediaReadyEvents.forEach((eventName) =>
      videoElement?.addEventListener(eventName, markVideoReady),
    );

    if (deviceId && expectedStorageMode === false) {
      setupTimer = window.setTimeout(() => {
        setupTimer = null;
        setupController?.abort();
        scheduleAutomaticReconnect({
          message: "AWS 연결 정보를 받지 못해 다시 시도하고 있습니다.",
        });
      }, AUTHORIZED_VIEWER_SETUP_TIMEOUT_MS);
      connectTimer = window.setTimeout(() => {
        connectTimer = null;
        scheduleAutomaticReconnect({
          message: "홈캠 연결 시간이 초과되어 다시 시도하고 있습니다.",
        });
      }, AUTHORIZED_P2P_CONNECT_TIMEOUT_MS);
    }

    void (async () => {
      try {
        const connectionInput = {
          clientId: viewerClientIdRef.current,
          localAudioStream,
          onStream: (stream: MediaStream) => {
            if (!active || !videoElement) return;
            remoteStream = stream;
            videoElement.srcObject = stream;
            void videoElement.play().then(markVideoReady).catch(async () => {
              if (!active) return;
              videoElement.muted = true;
              setSpeakerMuted(true);
              try {
                await videoElement.play();
                if (active) {
                  setSoundBlocked(false);
                  markVideoReady();
                }
              } catch {
                if (active) setSoundBlocked(true);
              }
            });

            const videoTrack = stream.getVideoTracks()[0];
            if (videoTrack && observedVideoTrack !== videoTrack) {
              observedVideoTrack = videoTrack;
              mediaReady = false;
              videoTrack.addEventListener("unmute", markVideoReady);
              markVideoReady();
              videoTrack.addEventListener("mute", () => {
                mediaReady = false;
                if (isCurrentGeneration() && transportLive) {
                  releaseTalkLease();
                  viewerStateRef.current = "connecting";
                  setState("connecting");
                  startMediaTimer();
                }
              });
              videoTrack.addEventListener(
                "ended",
                () => {
                  mediaReady = false;
                  transportLive = false;
                  if (videoElement.srcObject === stream) videoElement.srcObject = null;
                  if (isCurrentGeneration()) {
                    releaseTalkLease();
                    if (
                      deviceId &&
                      (connectionStorageMode ?? storageModeRef.current) === false
                    ) {
                      scheduleAutomaticReconnect({
                        message: "홈캠 영상이 종료되어 다시 연결하고 있습니다.",
                      });
                    } else {
                      viewerStateRef.current = "offline";
                      setState("offline");
                    }
                  }
                },
                { once: true },
              );
            }
          },
          callbacks: {
            onState: (next: KvsConnectionState) => {
              if (!isCurrentGeneration()) return;
              if (next === "live") {
                transportLive = true;
                clearConnectTimer();
                if (videoElement && remoteStream) videoElement.srcObject = remoteStream;
                if (!mediaReady) startMediaTimer();
                viewerStateRef.current = mediaReady ? "live" : "connecting";
                setState(mediaReady ? "live" : "connecting");
                return;
              }
              if (
                next === "connecting" &&
                transportLive &&
                (connectionStorageMode ?? storageModeRef.current) === false
              ) {
                releaseTalkLease();
                viewerStateRef.current = "connecting";
                setState("connecting");
                return;
              }
              transportLive = false;
              mediaReady = false;
              clearMediaTimer();
              if (next === "offline" && videoElement) videoElement.srcObject = null;
              releaseTalkLease();
              if (
                next === "offline" &&
                deviceId &&
                (connectionStorageMode ?? storageModeRef.current) === false
              ) {
                scheduleAutomaticReconnect({
                  message: "홈캠 연결이 끊겨 자동으로 다시 연결하고 있습니다.",
                });
              } else {
                viewerStateRef.current = next;
                setState(next);
              }
            },
            onError: (reason: Error) => {
              if (!isCurrentGeneration()) return;
              transportLive = false;
              mediaReady = false;
              clearMediaTimer();
              releaseTalkLease();
              const message = reason.message || "AWS KVS 연결에 실패했습니다.";
              if (
                deviceId &&
                (connectionStorageMode ?? storageModeRef.current) === false
              ) {
                scheduleAutomaticReconnect({
                  message,
                });
              } else {
                setError(message);
                viewerStateRef.current = "error";
                setState("error");
              }
            },
          },
        };
        const connection = await connectAuthorizedDeviceViewer({
          deviceId,
          signal: setupController?.signal,
          onStorageMode: (nextStorageMode) => {
            connectionStorageMode = nextStorageMode;
            if (!isCurrentGeneration()) return;
            storageModeRef.current = nextStorageMode;
            setStorageMode(nextStorageMode);
            if (nextStorageMode) {
              clearConnectTimer();
              clearMediaTimer();
              cancelAutomaticReconnect();
            }
          },
          ...connectionInput,
        });
        localConnection = connection;
        clearSetupTimer();
        connectionStorageMode = connection.storageMode;
        if (!isCurrentGeneration()) {
          connection.close();
        } else {
          setStorageMode(connection.storageMode);
          storageModeRef.current = connection.storageMode;
          if (connection.storageMode) {
            clearConnectTimer();
            clearMediaTimer();
            cancelAutomaticReconnect();
          } else {
            if (
              connectTimer === null &&
              !transportLive &&
              !reconnectPendingRef.current
            ) {
              connectTimer = window.setTimeout(() => {
                connectTimer = null;
                scheduleAutomaticReconnect({
                  message: "홈캠 연결 시간이 초과되어 다시 시도하고 있습니다.",
                });
              }, AUTHORIZED_P2P_CONNECT_TIMEOUT_MS);
            }
            if (transportLive && !mediaReady) startMediaTimer();
          }
          setMicrophoneNotice("");
          connectionRef.current = connection;
        }
      } catch (reason) {
        clearSetupTimer();
        if (!isCurrentGeneration()) return;
        releaseTalkLease();
        const message =
          reason instanceof Error && reason.name !== "AbortError"
            ? reason.message
            : "홈캠 연결 준비 시간이 초과되었습니다.";
        if (
          deviceId &&
          !viewerAccessRevokedRef.current &&
          (connectionStorageMode ??
            expectedStorageMode ??
            storageModeRef.current) === false
        ) {
          scheduleAutomaticReconnect({
            message,
          });
        } else {
          setError(message || "보호자 화면에 연결하지 못했습니다.");
          viewerStateRef.current = "error";
          setState("error");
        }
      }
    })();

    return () => {
      active = false;
      clearSetupTimer();
      clearConnectTimer();
      clearMediaTimer();
      clearStableLiveTimer();
      setupController?.abort();
      if (requestReconnectRef.current === scheduleAutomaticReconnect) {
        requestReconnectRef.current = null;
      }
      if (automaticReconnectTimerRef.current !== null) {
        window.clearTimeout(automaticReconnectTimerRef.current);
        automaticReconnectTimerRef.current = null;
      }
      releaseTalkLease(true, false);
      localAudioStream?.getAudioTracks().forEach((track) => {
        track.enabled = false;
      });
      localConnection?.close();
      if (connectionRef.current === localConnection) connectionRef.current = null;
      if (videoElement) {
        mediaReadyEvents.forEach((eventName) =>
          videoElement.removeEventListener(eventName, markVideoReady),
        );
        videoElement.srcObject = null;
      }
      remoteStream?.getTracks().forEach((track) => track.stop());
    };
  }, [
    attempt,
    deviceId,
    expectedStorageMode,
    localDemoViewer,
    releaseTalkLease,
  ]);

  const prepareMicrophone = async (): Promise<"attached" | "reconnecting" | "failed"> => {
    if (microphonePending || state !== "live") return "failed";
    setMicrophonePending(true);
    setMicrophoneNotice("");

    try {
      let stream = microphoneRef.current;
      if (!stream) {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error("이 브라우저에서는 마이크를 쓸 수 없어요.");
        }
        stream = await navigator.mediaDevices.getUserMedia(VIEWER_AUDIO_CONSTRAINTS);
      }
      if (!viewerMountedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return "failed";
      }

      const track = stream.getAudioTracks()[0];
      if (!track) throw new Error("사용할 수 있는 마이크를 찾지 못했어요.");
      track.enabled = false;
      microphoneRef.current = stream;
      setMicrophoneAvailable(true);
      setTalking(false);
      const connection = connectionRef.current;
      if (connection?.attachMicrophone && viewerStateRef.current === "live") {
        try {
          // The live connection already has a send-capable audio line: no reconnect.
          await connection.attachMicrophone(track);
          return "attached";
        } catch {
          // A closing connection: reconnect with the microphone in the offer instead.
        }
      }
      setError("");
      setState("connecting");
      setStorageMode(null);
      setMicrophoneNotice("마이크를 연결하기 위해 AWS 세션을 다시 연결하고 있습니다.");
      viewerGenerationRef.current += 1;
      setAttempt((value) => value + 1);
      return "reconnecting";
    } catch (reason) {
      talkWantedRef.current = false;
      if (viewerMountedRef.current) {
        setTalkWanted(false);
        setMicrophoneNotice(
          reason instanceof Error && reason.name !== "NotAllowedError"
            ? reason.message
            : "이 기기 마이크 권한이 없어요. 브라우저 설정에서 마이크를 허용해 주세요.",
        );
      }
      return "failed";
    } finally {
      if (viewerMountedRef.current) setMicrophonePending(false);
    }
  };

  /** "abandoned": the connection or switch changed under it, without its own notice. */
  const startTalking = async (): Promise<"talking" | "failed" | "abandoned" | "skipped"> => {
    const track = microphoneRef.current?.getAudioTracks()[0];
    if (
      !track ||
      viewerStateRef.current !== "live" ||
      talkIntentRef.current ||
      talkLeasePending ||
      talking
    ) {
      return "skipped";
    }
    const talkGeneration = viewerGenerationRef.current;
    const talkClientId = viewerClientIdRef.current;
    let acquiredLease: ViewerTalkLease | null = null;
    talkIntentRef.current = true;
    const talkAttempt = ++talkAttemptRef.current;
    const isCurrentTalk = () =>
      talkAttemptRef.current === talkAttempt && talkIntentRef.current &&
      viewerMountedRef.current && viewerStateRef.current === "live" &&
      viewerGenerationRef.current === talkGeneration &&
      viewerClientIdRef.current === talkClientId;

    if (deviceId) {
      setTalkLeasePending(true);
      try {
        const readyDeadline = Date.now() + 10_000;
        let ready = false;
        while (!ready) {
          if (!isCurrentTalk()) return "abandoned";
          const remainingMs = readyDeadline - Date.now();
          if (remainingMs <= 0) throw new Error("말벗이 말하기를 준비하지 못했어요. 잠시 뒤 다시 켜 주세요.");
          const requestedAt = performance.now();
          const response = await fetch(
            `/api/devices/${encodeURIComponent(deviceId)}/talk-lease`,
            {
              method: "POST",
              headers: { "content-type": "application/json" },
              signal: AbortSignal.timeout(remainingMs),
              body: JSON.stringify({
                clientId: talkClientId,
                ...(acquiredLease ? { leaseId: acquiredLease.leaseId } : {}),
              }),
            },
          );
          const payload = (await response.json().catch(() => null)) as {
            lease?: { leaseId?: string; expiresAt?: string; ready?: boolean; readyForMs?: number };
            error?: string;
            holder?: TalkHolder | null;
          } | null;
          const leaseId = payload?.lease?.leaseId;
          if (!response.ok || !leaseId) {
            if (response.status === 409 && payload?.holder) {
              // Another guardian is talking: the switch shows who and waits, not an error.
              setTalkHolder(payload.holder);
              throw new Error("");
            }
            throw new Error(
              response.status === 409
                ? "다른 보호자가 말하는 중이에요. 잠시 뒤 다시 켜 주세요."
                : payload?.error ?? "말하기 권한을 받지 못했어요.",
            );
          }
          const receivedLease: ViewerTalkLease = {
            leaseId,
            clientId: talkClientId,
            generation: talkGeneration,
          };
          if (acquiredLease && leaseId !== acquiredLease.leaseId) {
            notifyTalkLeaseRelease(receivedLease);
            throw new Error("말하기 권한이 끝났어요. 다시 켜 주세요.");
          }
          if (!isCurrentTalk()) {
            notifyTalkLeaseRelease(receivedLease);
            return "abandoned";
          }
          acquiredLease = receivedLease;
          talkLeaseRef.current = acquiredLease;
          const readyForMs = payload?.lease?.readyForMs;
          ready = payload?.lease?.ready === true && typeof readyForMs === "number" &&
            readyForMs <= 2500 && readyForMs > performance.now() - requestedAt;
          if (!ready) await new Promise<void>((resolve) => window.setTimeout(resolve, 250));
        }
        if (!isCurrentTalk()) return "abandoned";

        const renewLease = async () => {
          const currentLease = talkLeaseRef.current;
          if (
            !currentLease ||
            currentLease !== acquiredLease ||
            !talkIntentRef.current ||
            !viewerMountedRef.current ||
            viewerGenerationRef.current !== talkGeneration
          ) {
            return;
          }
          try {
            const requestedAt = performance.now();
            const renewal = await fetch(
              `/api/devices/${encodeURIComponent(deviceId)}/talk-lease`,
              {
                method: "POST",
                headers: { "content-type": "application/json" },
                signal: AbortSignal.timeout(5_000),
                body: JSON.stringify({
                  leaseId: currentLease.leaseId,
                  clientId: currentLease.clientId,
                }),
              },
            );
            const renewalPayload = (await renewal.json().catch(() => null)) as {
              lease?: { leaseId?: string; ready?: boolean; readyForMs?: number };
              error?: string;
              code?: string;
            } | null;
            if (renewal.status === 409 && renewalPayload?.code === "time_limit") {
              // The server's 3-minute limit: the same notice as this screen's own timer.
              talkStopReasonRef.current = "timeout";
              setTalkEnded("timeout");
              throw new Error("");
            }
            if (renewal.ok && renewalPayload?.lease?.leaseId &&
                renewalPayload.lease.leaseId !== currentLease.leaseId) {
              notifyTalkLeaseRelease({ ...currentLease, leaseId: renewalPayload.lease.leaseId });
            }
            if (
              !renewal.ok ||
              renewalPayload?.lease?.leaseId !== currentLease.leaseId ||
              renewalPayload.lease.ready !== true ||
              typeof renewalPayload.lease.readyForMs !== "number" ||
              renewalPayload.lease.readyForMs > 2500 ||
              !(renewalPayload.lease.readyForMs > performance.now() - requestedAt)
            ) {
              throw new Error(renewalPayload?.error ?? "말하기 연결이 끊겼어요. 다시 켜 주세요.");
            }
            if (
              talkLeaseRef.current !== currentLease ||
              !talkIntentRef.current ||
              !viewerMountedRef.current ||
              viewerGenerationRef.current !== currentLease.generation ||
              viewerClientIdRef.current !== currentLease.clientId
            ) {
              return;
            }
            talkLeaseTimerRef.current = window.setTimeout(() => void renewLease(), 8_000);
          } catch (reason) {
            if (talkLeaseRef.current !== currentLease) return;
            releaseTalkLease();
            setMicrophoneNotice(
              reason instanceof Error ? reason.message : "말하기 연결이 끊겼어요.",
            );
          }
        };
        talkLeaseTimerRef.current = window.setTimeout(() => void renewLease(), 8_000);
      } catch (reason) {
        if (
          talkAttemptRef.current !== talkAttempt ||
          viewerGenerationRef.current !== talkGeneration ||
          viewerClientIdRef.current !== talkClientId
        ) {
          return "abandoned";
        }
        releaseTalkLease();
        setMicrophoneNotice(
          reason instanceof Error ? reason.message : "말하기 권한을 받지 못했어요.",
        );
        return "failed";
      }
    }

    if (
      !isCurrentTalk() ||
      (deviceId && talkLeaseRef.current !== acquiredLease)
    ) {
      if (acquiredLease && talkLeaseRef.current === acquiredLease) {
        releaseTalkLease();
      } else {
        track.enabled = false;
      }
      return "abandoned";
    }
    track.enabled = true;
    setTalkLeasePending(false);
    setMicrophoneNotice("");
    setTalking(true);
    return "talking";
  };

  const toggleSpeaker = async () => {
    const video = videoRef.current;
    if (!video) return;
    const nextMuted = soundBlocked ? false : !speakerMuted;
    video.muted = nextMuted;
    setSpeakerMuted(nextMuted);
    if (!nextMuted) {
      try {
        await video.play();
        setSoundBlocked(false);
      } catch {
        setSoundBlocked(true);
      }
    }
  };
  const toggleSpeakerRef = useRef(toggleSpeaker);
  useEffect(() => {
    toggleSpeakerRef.current = toggleSpeaker;
  });
  useEffect(() => {
    onSpeakerChange?.({
      muted: speakerMuted,
      blocked: soundBlocked,
      toggle: () => void toggleSpeakerRef.current(),
    });
  }, [onSpeakerChange, speakerMuted, soundBlocked]);
  useEffect(() => () => onSpeakerChange?.(null), [onSpeakerChange]);

  // A call: the microphone brings this device's sound on with it.
  const turnSpeakerOn = async () => {
    if (speakerMuted || soundBlocked) await toggleSpeaker();
  };

  // 현재 상태 › 마이크: on until switched off, the viewer leaves, or 3 minutes pass.
  // One switch-on attempt. A start dropped by a connection change says so instead of
  // leaving the switch silently off.
  const runTalk = async () => {
    const run = ++talkRunRef.current;
    const outcome = await startTalking();
    if (talkRunRef.current !== run || !viewerMountedRef.current) return;
    talkWantedRef.current = false;
    setTalkWanted(false);
    if (outcome === "abandoned") {
      releaseTalkLease();
      setTalkEnded("dropped");
    }
  };

  const toggleTalk = async () => {
    if (talkWantedRef.current || talkIntentRef.current || talking || talkLeasePending) {
      talkRunRef.current += 1;
      talkWantedRef.current = false;
      talkStopReasonRef.current = "user";
      setTalkWanted(false);
      releaseTalkLease();
      return;
    }
    if (talkHolder || microphonePending || viewerStateRef.current !== "live") return;
    // Within the tap, so the browser lets the video play its sound.
    void turnSpeakerOn();
    setTalkEnded(null);
    setMicrophoneNotice("");
    talkWantedRef.current = true;
    setTalkWanted(true);
    if (!microphoneRef.current) {
      // First use: ask for the microphone and put it on the live connection. Only a
      // connection that cannot take it reconnects; the effect below then starts talking.
      const prepared = await prepareMicrophone();
      if (prepared === "reconnecting") return;
      if (prepared === "failed") {
        talkWantedRef.current = false;
        if (viewerMountedRef.current) setTalkWanted(false);
        return;
      }
    }
    await runTalk();
  };
  const toggleTalkRef = useRef(toggleTalk);
  const runTalkRef = useRef(runTalk);
  const turnSpeakerOnRef = useRef(turnSpeakerOn);
  useEffect(() => {
    toggleTalkRef.current = toggleTalk;
    runTalkRef.current = runTalk;
    turnSpeakerOnRef.current = turnSpeakerOn;
  });

  useEffect(() => {
    if (state !== "live" || !microphoneAvailable || !talkWantedRef.current ||
        talkIntentRef.current) return;
    void (async () => {
      // Reconnecting for the new microphone can mute the video again.
      await turnSpeakerOnRef.current();
      await runTalkRef.current();
    })();
  }, [state, microphoneAvailable]);

  useEffect(() => {
    if ((state !== "error" && state !== "offline") || !talkWantedRef.current) return;
    talkWantedRef.current = false;
    window.queueMicrotask(() => {
      if (!viewerMountedRef.current) return;
      setTalkWanted(false);
      setMicrophoneNotice("홈캠 연결이 끊겨 마이크를 켜지 못했어요.");
    });
  }, [state]);

  useEffect(() => {
    if (!talking) return;
    const startedAt = Date.now();
    window.queueMicrotask(() => setTalkStartedAt(startedAt));
    // 말벗 cannot hear anyone while this is on, so it never stays on unattended.
    const timer = window.setTimeout(() => {
      talkWantedRef.current = false;
      talkStopReasonRef.current = "timeout";
      releaseTalkLease();
      setTalkEnded("timeout");
    }, TALK_LIMIT_MS);
    return () => window.clearTimeout(timer);
  }, [talking, releaseTalkLease]);

  useEffect(() => {
    if (talking) {
      wasTalkingRef.current = true;
      talkStopReasonRef.current = null;
      return;
    }
    if (!wasTalkingRef.current) return;
    wasTalkingRef.current = false;
    const reason = talkStopReasonRef.current;
    talkStopReasonRef.current = null;
    // Not the switch or the 3-minute limit (page hidden, reconnect, lost lease): say so.
    if (reason === null) {
      window.queueMicrotask(() => {
        if (viewerMountedRef.current) setTalkEnded((current) => current ?? "dropped");
      });
    }
  }, [talking]);

  useEffect(() => {
    if (!deviceId || localDemoViewer || state !== "live" || talking || talkLeasePending) return;
    let active = true;
    const checkHolder = async () => {
      try {
        const response = await fetch(
          `/api/devices/${encodeURIComponent(deviceId)}/talk-lease?clientId=${encodeURIComponent(viewerClientIdRef.current)}`,
          { cache: "no-store" },
        );
        if (!active || !response.ok) return;
        const payload = (await response.json().catch(() => null)) as {
          holder?: TalkHolder | null;
        } | null;
        if (active) setTalkHolder(payload?.holder ?? null);
      } catch {
        // Keep the last known holder through a transient failure.
      }
    };
    window.queueMicrotask(() => void checkHolder());
    const interval = window.setInterval(() => void checkHolder(), 5_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [deviceId, localDemoViewer, state, talking, talkLeasePending]);

  const talkPhase = talking
    ? "talking"
    : talkWanted || talkLeasePending || microphonePending
      ? "starting"
      : "off";
  const talkRemainingMs = talking && talkStartedAt !== null
    ? Math.max(0, TALK_LIMIT_MS - (viewerClockMs - talkStartedAt))
    : TALK_LIMIT_MS;
  useEffect(() => {
    onTalkChange?.({
      phase: talkPhase,
      available: state === "live",
      remainingMs: talkRemainingMs,
      holder: talkHolder,
      ended: talkEnded,
      error: talkPhase === "off" ? microphoneNotice : "",
      toggle: () => void toggleTalkRef.current(),
    });
  }, [onTalkChange, talkPhase, state, talkRemainingMs, talkHolder, talkEnded, microphoneNotice]);
  useEffect(() => () => onTalkChange?.(null), [onTalkChange]);

  return (
    <div className={`homecam-shell homecam-stream-shell ${embedded ? "is-embedded" : ""}`}>
      <HomecamHeader activeTab="live" onNavigate={onExit} />
      <main className="homecam-main homecam-stream-main">
        <div className="homecam-stream-context">
          <button
            type="button"
            className="homecam-stream-back"
            onClick={() => onExit("live")}
          >
            <ArrowLeft size={15} weight="bold" aria-hidden="true" />
            홈캠 홈
          </button>
          <div className="homecam-stream-device">
            <span
              className={`homecam-online-dot ${state !== "offline" && state !== "error" ? "is-online" : ""}`}
              aria-hidden="true"
            />
            <strong>
              {device?.displayName ?? "등록된 홈캠"}
            </strong>
            <span data-testid="connection-status">{STATE_COPY[state]}</span>
          </div>
          <span className="homecam-stream-security">
            <ShieldCheck size={15} weight="regular" aria-hidden="true" />
            AWS KVS · PRIVATE
          </span>
        </div>

        <section
          className="homecam-live-view homecam-stream-view"
          aria-label="우리 집 실시간 홈캠"
        >
          <div className="homecam-video-card homecam-stream-video-card">
            <div className="homecam-video-frame homecam-stream-video-frame">
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted={speakerMuted}
                data-testid="guardian-video"
              />
              <div className="homecam-video-topbar">
                <span className="homecam-video-clock">{formatViewerClock(viewerClockMs)}</span>
                {talking && (
                  <span className="homecam-video-talking" role="status">
                    말하는 중 {formatTalkRemaining(talkRemainingMs)}
                  </span>
                )}
                <button type="button" onClick={() => void videoRef.current?.requestFullscreen().catch(() => undefined)} aria-label="실시간 영상 전체 화면">
                  <CornersOut size={19} weight="regular" aria-hidden="true" />
                </button>
              </div>

              {state !== "live" && (
                <div className="homecam-stream-placeholder">
                  <VideoCamera size={40} weight="light" aria-hidden="true" />
                  <h1>
                    {state === "offline"
                      ? "홈캠 연결이 끊겼어요"
                      : state === "error"
                        ? "연결을 다시 확인해 주세요"
                        : "보안 채널 연결 중"}
                  </h1>
                  <p>연결되면 우리 집 영상과 소리가 이 화면에서 바로 시작됩니다.</p>
                </div>
              )}

              <div className="homecam-video-bottom">
                <span>{recordingEnabled ? "연속 녹화" : "녹화 안 함"}</span>
                <span>허용된 보호자만 볼 수 있어요</span>
              </div>
            </div>

            {/* In the app, microphone notices sit under the 현재 상태 마이크 switch. */}
            {microphoneNotice && !embedded && (
              <p className="homecam-stream-notice" role="status">{microphoneNotice}</p>
            )}
            {error && (
              <p className="homecam-stream-notice is-error" role="alert">{error}</p>
            )}

            <div className="homecam-stream-controls">
              <div className="homecam-stream-privacy">
                <ShieldCheck size={17} weight="regular" aria-hidden="true" />
                <span>
                  {recordingEnabled
                    ? "실시간 보기는 말벗과 직접 연결되고, 연속 녹화는 따로 저장돼요."
                    : "실시간 영상은 저장하지 않아요."}
                </span>
              </div>
              {/* In the app, 스피커·카메라·마이크 are rows of the 현재 상태 card below the video. */}
              {embedded ? null : <div className="homecam-stream-control-buttons">
                <button
                  type="button"
                  className={`homecam-stream-control-button ${talking ? "is-talking" : ""}`}
                  disabled={
                    microphonePending ||
                    talkLeasePending ||
                    state !== "live"
                  }
                  onClick={() => {
                    if (!microphoneAvailable) void prepareMicrophone();
                  }}
                  onPointerDown={(event) => {
                    if (!microphoneAvailable) return;
                    event.preventDefault();
                    event.currentTarget.setPointerCapture(event.pointerId);
                    void startTalking();
                  }}
                  onPointerUp={() => releaseTalkLease()}
                  onPointerCancel={() => releaseTalkLease()}
                  onPointerLeave={() => releaseTalkLease()}
                  onBlur={() => releaseTalkLease()}
                  onKeyDown={(event) => {
                    if (
                      microphoneAvailable &&
                      !event.repeat &&
                      (event.key === " " || event.key === "Enter")
                    ) {
                      event.preventDefault();
                      void startTalking();
                    }
                  }}
                  onKeyUp={(event) => {
                    if (event.key === " " || event.key === "Enter") {
                      event.preventDefault();
                      releaseTalkLease();
                    }
                  }}
                  aria-pressed={talking}
                >
                  <Microphone size={16} weight={talking ? "fill" : "regular"} aria-hidden="true" />
                  {microphonePending
                    ? "권한 확인 중"
                    : talkLeasePending
                      ? "말하기 준비 중"
                      : !microphoneAvailable
                        ? "마이크 연결"
                        : talking
                          ? "말하는 중"
                          : "눌러서 말하기"}
                </button>
                <button
                  type="button"
                  className="homecam-stream-control-button"
                  onClick={toggleSpeaker}
                >
                  {speakerMuted && !soundBlocked
                    ? <SpeakerSlash size={16} weight="regular" aria-hidden="true" />
                    : <SpeakerHigh size={16} weight="regular" aria-hidden="true" />}
                  {soundBlocked ? "소리 재생" : speakerMuted ? "소리 켜기" : "소리 끄기"}
                </button>
                <button
                  type="button"
                  className="homecam-stream-control-button"
                  disabled={microphonePending}
                  onClick={() => {
                    if (automaticReconnectTimerRef.current !== null) {
                      window.clearTimeout(automaticReconnectTimerRef.current);
                      automaticReconnectTimerRef.current = null;
                    }
                    if (stableLiveTimerRef.current !== null) {
                      window.clearTimeout(stableLiveTimerRef.current);
                      stableLiveTimerRef.current = null;
                    }
                    automaticReconnectAttemptsRef.current = 0;
                    reconnectPendingRef.current = false;
                    viewerAccessRevokedRef.current = false;
                    releaseTalkLease();
                    setError("");
                    viewerStateRef.current = "connecting";
                    setState("connecting");
                    setSoundBlocked(false);
                    setStorageMode(null);
                    viewerGenerationRef.current += 1;
                    setAttempt((value) => value + 1);
                  }}
                >
                  <ArrowClockwise size={16} weight="bold" aria-hidden="true" />
                  다시 연결
                </button>
              </div>}
            </div>
          </div>

          {!embedded && <aside
            className="homecam-quick-grid homecam-stream-sidebar"
            aria-label="실시간 연결 정보"
          >
            <article className="homecam-summary-card">
              <span className="summary-icon" aria-hidden="true">
                <WifiHigh size={22} weight="regular" />
              </span>
              <div>
                <span>연결 상태</span>
                <strong>{STATE_COPY[state]}</strong>
              </div>
            </article>
            <article className="homecam-summary-card">
              <span className="summary-icon" aria-hidden="true">
                <ShieldCheck size={22} weight="regular" />
              </span>
              <div>
                <span>영상 보관</span>
                <strong>{recordingEnabled ? "이벤트만 저장" : "저장 안 함"}</strong>
              </div>
            </article>
            <article className="homecam-summary-card">
              <span className="summary-icon" aria-hidden="true">
                <Microphone size={22} weight="regular" />
              </span>
              <div>
                <span>보호자 마이크</span>
                <strong>
                  {!microphoneAvailable ? "연결 전" : talking ? "전송 중" : "기본 음소거"}
                </strong>
              </div>
            </article>
          </aside>}
        </section>
      </main>
    </div>
  );
}

export function HomecamApp() {
  const [inlineViewerDevice, setInlineViewerDevice] = useState<HomecamDevice | null>(null);
  const [inlineViewerReady, setInlineViewerReady] = useState(false);
  const [liveSpeaker, setLiveSpeaker] = useState<LiveSpeaker | null>(null);
  const [liveTalk, setLiveTalk] = useState<LiveTalk | null>(null);

  const closeInlineViewer = useCallback(() => {
    setInlineViewerReady(false);
    setInlineViewerDevice(null);
  }, []);

  const openRegisteredDevice = async (device: HomecamDevice) => {
    setInlineViewerDevice(null);
    setInlineViewerReady(false);
    await Promise.resolve();
    setInlineViewerDevice(device);
  };

  return (
    <HomecamDashboard
      onOpenLive={openRegisteredDevice}
      liveMediaReady={inlineViewerReady}
      liveSpeaker={liveSpeaker}
      liveTalk={liveTalk}
      onReleaseLive={closeInlineViewer}
      liveViewer={inlineViewerDevice ? ({ device }) => (
        device?.id === inlineViewerDevice.id ? (
          <Viewer
            deviceId={inlineViewerDevice.id}
            device={device}
            embedded
            onMediaReadyChange={setInlineViewerReady}
            onSpeakerChange={setLiveSpeaker}
            onTalkChange={setLiveTalk}
            onExit={closeInlineViewer}
          />
        ) : null
      ) : undefined}
    />
  );
}
