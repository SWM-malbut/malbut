"use client";

import Hls from "hls.js";

export type LiveHlsState = "connecting" | "live" | "offline" | "error";

type ConnectLiveHlsInput = {
  deviceId: string;
  video: HTMLVideoElement;
  signal?: AbortSignal;
  onState: (state: LiveHlsState) => void;
  onError: (error: Error) => void;
};

type PlaybackResponse = {
  playbackUrl?: unknown;
  expiresAt?: unknown;
  error?: unknown;
};

const READY_RETRY_LIMIT = 30;
const DEFAULT_RETRY_SECONDS = 2;
const REFRESH_MARGIN_MS = 60_000;

export async function connectDeviceLiveHls(input: ConnectLiveHlsInput) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  input.signal?.addEventListener("abort", abort, { once: true });
  let player: Hls | null = null;
  let refreshTimer: number | null = null;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    controller.abort();
    input.signal?.removeEventListener("abort", abort);
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    player?.destroy();
    player = null;
    input.video.pause();
    input.video.removeAttribute("src");
    input.video.load();
  };

  const fail = (reason: unknown) => {
    if (closed || controller.signal.aborted) return;
    const error = reason instanceof Error
      ? reason
      : new Error("저장 영상을 불러오지 못했습니다.");
    input.onError(error);
    input.onState("error");
  };

  const markLive = () => {
    if (closed || input.video.videoWidth <= 0 || input.video.videoHeight <= 0) return;
    input.onState("live");
  };
  input.video.addEventListener("loadeddata", markLive);
  input.video.addEventListener("playing", markLive);

  const requestPlayback = async () => {
    for (let attempt = 0; attempt <= READY_RETRY_LIMIT; attempt += 1) {
      input.onState("connecting");
      const response = await fetch(
        `/api/devices/${encodeURIComponent(input.deviceId)}/live-playback`,
        { method: "POST", cache: "no-store", signal: controller.signal },
      );
      const payload = (await response.json().catch(() => ({}))) as PlaybackResponse;
      if (response.ok && typeof payload.playbackUrl === "string") {
        return {
          playbackUrl: payload.playbackUrl,
          expiresAt:
            typeof payload.expiresAt === "string"
              ? Date.parse(payload.expiresAt)
              : Date.now() + 300_000,
        };
      }
      if (response.status !== 425 || attempt === READY_RETRY_LIMIT) {
        throw new Error(
          typeof payload.error === "string"
            ? payload.error
            : "저장 영상 재생 정보를 받지 못했습니다.",
        );
      }
      const retrySeconds = Number(response.headers.get("retry-after"));
      await abortableDelay(
        (Number.isFinite(retrySeconds) && retrySeconds > 0
          ? retrySeconds
          : DEFAULT_RETRY_SECONDS) * 1_000,
        controller.signal,
      );
    }
    throw new Error("저장 영상 준비 시간이 초과되었습니다.");
  };

  const attach = async () => {
    const playback = await requestPlayback();
    if (closed) return;
    player?.destroy();
    player = null;
    input.video.removeAttribute("src");
    input.video.load();

    if (input.video.canPlayType("application/vnd.apple.mpegurl")) {
      input.video.src = playback.playbackUrl;
      input.video.load();
    } else if (Hls.isSupported()) {
      player = new Hls({
        enableWorker: true,
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
      });
      player.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || closed) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          player?.startLoad();
          return;
        }
        fail(new Error("저장 영상 스트림을 재생하지 못했습니다."));
      });
      player.loadSource(playback.playbackUrl);
      player.attachMedia(input.video);
    } else {
      throw new Error("이 브라우저는 저장 영상 재생을 지원하지 않습니다.");
    }

    await input.video.play().catch((error: unknown) => {
      if (error instanceof Error && error.name === "NotAllowedError") return;
      throw error;
    });
    const refreshAfter = Math.max(
      30_000,
      (Number.isFinite(playback.expiresAt) ? playback.expiresAt : Date.now() + 300_000) -
        Date.now() -
        REFRESH_MARGIN_MS,
    );
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      void attach().catch(fail);
    }, refreshAfter);
  };

  input.onState("connecting");
  void attach().catch(fail);
  return {
    close: () => {
      input.video.removeEventListener("loadeddata", markLive);
      input.video.removeEventListener("playing", markLive);
      close();
    },
  };
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
