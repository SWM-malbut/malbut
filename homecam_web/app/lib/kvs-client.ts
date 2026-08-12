"use client";

import { AUTHORIZED_P2P_DISCONNECT_GRACE_MS } from "./viewer-reconnect";

export type KvsConnectionState = "waiting" | "connecting" | "live" | "offline";

type KvsRole = "MASTER" | "VIEWER";

type WebRtcWindow = typeof window & {
  webkitRTCPeerConnection?: typeof RTCPeerConnection;
};

type KvsSessionConfig = {
  role: KvsRole;
  region: string;
  channelArn: string;
  channelEndpoint: string;
  signedWssUrl: string;
  iceServers: RTCIceServer[];
  expiresAt: string;
  roomCode: string;
  clientId: string | null;
  storageMode?: boolean;
};

type KvsStatusResponse = {
  correlationId?: string;
  success?: boolean;
  errorType?: string;
  statusCode?: string;
  description?: string;
};

type KvsSignalingClient = {
  on(event: "open", callback: () => void): void;
  on(event: "close", callback: () => void): void;
  on(event: "error", callback: (error: Error) => void): void;
  on(
    event: "sdpOffer",
    callback: (offer: RTCSessionDescriptionInit, senderClientId?: string) => void,
  ): void;
  on(
    event: "sdpAnswer",
    callback: (answer: RTCSessionDescriptionInit) => void,
  ): void;
  on(
    event: "iceCandidate",
    callback: (candidate: RTCIceCandidateInit, senderClientId?: string) => void,
  ): void;
  on(event: "statusResponse", callback: (response: KvsStatusResponse) => void): void;
  open(): void;
  close(): void;
  sendSdpOffer(offer: RTCSessionDescription): void;
  sendSdpAnswer(
    answer: RTCSessionDescription,
    recipientClientId?: string,
    correlationId?: string,
  ): void;
  sendIceCandidate(
    candidate: RTCIceCandidate,
    recipientClientId?: string,
    correlationId?: string,
  ): void;
  drainPendingIceCandidates(clientId?: string): void;
  resetIceCandidateState(clientId?: string): void;
};

type KvsSdk = {
  Role: { MASTER: "MASTER"; VIEWER: "VIEWER" };
  SignalingClient: new (config: {
    channelARN: string;
    channelEndpoint: string;
    clientId?: string;
    role: KvsRole;
    region: string;
    requestSigner: {
      getSignedURL: () => Promise<string>;
    };
    enableEarlyIceCandidateBuffering: boolean;
  }) => KvsSignalingClient;
};

type ConnectionCallbacks = {
  onState: (state: KvsConnectionState) => void;
  onError: (error: Error) => void;
};

export type KvsConnection = {
  close: () => void;
  storageMode: boolean;
};

let sdkPromise: Promise<KvsSdk> | null = null;

export async function createLiveSession() {
  const response = await fetch("/api/live-sessions", {
    method: "POST",
    headers: { "content-type": "application/json" },
  });
  const payload = (await response.json()) as {
    session?: { roomCode: string; viewerPassword: string };
    error?: string;
  };
  if (!response.ok || !payload.session) {
    throw new Error(payload.error ?? "스트리밍 세션을 만들지 못했습니다.");
  }
  return payload.session;
}

export async function endLiveSession(roomCode: string) {
  await fetch("/api/live-sessions", {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ roomCode }),
    keepalive: true,
  }).catch(() => undefined);
}

export async function connectKvsMaster(input: {
  roomCode: string;
  stream: MediaStream;
  onRemoteStream?: (stream: MediaStream) => void;
  callbacks: ConnectionCallbacks;
}): Promise<KvsConnection> {
  const PeerConnection = requireRtcPeerConnection();
  const [sdk, config] = await Promise.all([
    loadKvsSdk(),
    requestKvsSession(input.roomCode, "MASTER"),
  ]);
  if (config.storageMode) {
    return connectKvsStorageParticipant({
      role: "MASTER",
      roomCode: input.roomCode,
      localStream: input.stream,
      onRemoteStream: input.onRemoteStream,
      callbacks: input.callbacks,
      PeerConnection,
      sdk,
      config,
    });
  }
  let closed = false;
  let peer: RTCPeerConnection | null = null;
  let remoteStream: MediaStream | null = null;
  let remoteClientId: string | null = null;
  const queuedCandidates: Array<{ candidate: RTCIceCandidateInit; sender?: string }> = [];

  const signaling = new sdk.SignalingClient({
    channelARN: config.channelArn,
    channelEndpoint: config.channelEndpoint,
    role: sdk.Role.MASTER,
    region: config.region,
    requestSigner: { getSignedURL: async () => config.signedWssUrl },
    enableEarlyIceCandidateBuffering: true,
  });

  const closePeer = () => {
    peer?.close();
    peer = null;
    remoteStream?.getTracks().forEach((track) => track.stop());
    remoteStream = null;
    remoteClientId = null;
    queuedCandidates.length = 0;
  };

  signaling.on("open", () => {
    if (!closed) input.callbacks.onState("waiting");
  });

  signaling.on("sdpOffer", async (offer, senderClientId) => {
    if (closed || !senderClientId) return;
    closePeer();
    remoteClientId = senderClientId;
    input.callbacks.onState("connecting");

    const nextPeer = new PeerConnection({ iceServers: config.iceServers });
    peer = nextPeer;
    input.stream.getTracks().forEach((track) => nextPeer.addTrack(track, input.stream));

    nextPeer.onicecandidate = ({ candidate }) => {
      if (candidate && remoteClientId === senderClientId) {
        signaling.sendIceCandidate(candidate, senderClientId);
      }
    };
    nextPeer.ontrack = (event) => {
      if (closed || peer !== nextPeer) return;
      const stream = event.streams[0] ?? remoteStream ?? new MediaStream();
      if (!stream.getTracks().some((track) => track.id === event.track.id)) {
        stream.addTrack(event.track);
      }
      remoteStream = stream;
      input.onRemoteStream?.(stream);
    };
    nextPeer.onconnectionstatechange = () => {
      if (nextPeer !== peer || closed) return;
      if (nextPeer.connectionState === "connected") input.callbacks.onState("live");
      if (["failed", "disconnected"].includes(nextPeer.connectionState)) {
        input.callbacks.onState("waiting");
      }
    };

    try {
      await nextPeer.setRemoteDescription(offer);
      signaling.drainPendingIceCandidates(senderClientId);
      for (const entry of queuedCandidates.splice(0)) {
        if (!entry.sender || entry.sender === senderClientId) {
          await nextPeer.addIceCandidate(entry.candidate);
        }
      }
      const answer = await nextPeer.createAnswer();
      await nextPeer.setLocalDescription(answer);
      if (nextPeer.localDescription) {
        signaling.sendSdpAnswer(nextPeer.localDescription, senderClientId);
      }
    } catch (error) {
      input.callbacks.onError(toError(error, "보호자 연결 요청을 처리하지 못했습니다."));
    }
  });

  signaling.on("iceCandidate", (candidate, senderClientId) => {
    if (closed || (senderClientId && remoteClientId && senderClientId !== remoteClientId)) return;
    if (peer?.remoteDescription) {
      peer.addIceCandidate(candidate).catch(() => undefined);
    } else {
      queuedCandidates.push({ candidate, sender: senderClientId });
    }
  });

  signaling.on("close", () => {
    if (!closed) input.callbacks.onState("offline");
  });
  signaling.on("error", (error) => {
    if (!closed) input.callbacks.onError(error);
  });
  signaling.open();

  return {
    storageMode: false,
    close() {
      closed = true;
      closePeer();
      signaling.close();
    },
  };
}

export async function connectKvsViewer(input: {
  roomCode: string;
  viewerPassword: string;
  clientId?: string;
  localAudioStream?: MediaStream;
  onStream: (stream: MediaStream) => void;
  callbacks: ConnectionCallbacks;
}): Promise<KvsConnection> {
  const PeerConnection = requireRtcPeerConnection();
  const clientId = input.clientId ?? `petcam-${crypto.randomUUID()}`;
  const [sdk, config] = await Promise.all([
    loadKvsSdk(),
    requestKvsSession(input.roomCode, "VIEWER", clientId, input.viewerPassword),
  ]);
  if (config.storageMode) {
    return connectKvsStorageParticipant({
      role: "VIEWER",
      roomCode: input.roomCode,
      viewerPassword: input.viewerPassword,
      clientId,
      localStream: input.localAudioStream ?? null,
      onRemoteStream: input.onStream,
      callbacks: input.callbacks,
      PeerConnection,
      sdk,
      config,
    });
  }
  return connectKvsP2pViewer({
    clientId,
    localAudioStream: input.localAudioStream ?? null,
    onStream: input.onStream,
    callbacks: input.callbacks,
    PeerConnection,
    sdk,
    config,
  });
}

export async function connectAuthorizedDeviceViewer(input: {
  deviceId: string;
  clientId?: string;
  localAudioStream?: MediaStream;
  signal?: AbortSignal;
  onStorageMode?: (storageMode: boolean) => void;
  onStream: (stream: MediaStream) => void;
  callbacks: ConnectionCallbacks;
}): Promise<KvsConnection> {
  const PeerConnection = requireRtcPeerConnection();
  const clientId = input.clientId ?? `homecam-${crypto.randomUUID()}`;
  const [sdk, config] = await Promise.all([
    loadKvsSdk(),
    requestAuthorizedDeviceSession(input.deviceId, clientId, input.signal, false),
  ]);
  input.onStorageMode?.(Boolean(config.storageMode));
  if (config.storageMode) {
    const refreshConfig = (signal: AbortSignal) =>
      requestAuthorizedDeviceSession(input.deviceId, clientId, signal, false);
    return connectKvsStorageParticipant({
      role: "VIEWER",
      roomCode: config.roomCode,
      clientId,
      localStream: input.localAudioStream ?? null,
      onRemoteStream: input.onStream,
      callbacks: input.callbacks,
      PeerConnection,
      sdk,
      config,
      refreshConfig,
      requestStorageJoin: async (signal) => {
        await requestAuthorizedDeviceSession(input.deviceId, clientId, signal, true);
      },
    });
  }
  return connectKvsP2pViewer({
    clientId,
    localAudioStream: input.localAudioStream ?? null,
    onStream: input.onStream,
    callbacks: input.callbacks,
    PeerConnection,
    sdk,
    config,
    disconnectGraceMs: AUTHORIZED_P2P_DISCONNECT_GRACE_MS,
  });
}

function connectKvsP2pViewer(input: {
  clientId: string;
  localAudioStream: MediaStream | null;
  onStream: (stream: MediaStream) => void;
  callbacks: ConnectionCallbacks;
  PeerConnection: typeof RTCPeerConnection;
  sdk: KvsSdk;
  config: KvsSessionConfig;
  disconnectGraceMs?: number;
}): KvsConnection {
  let closed = false;
  let disconnectTimer: number | null = null;
  const queuedCandidates: RTCIceCandidateInit[] = [];
  const peer = new input.PeerConnection({ iceServers: input.config.iceServers });
  peer.addTransceiver("video", { direction: "recvonly" });
  const localAudioTrack = input.localAudioStream?.getAudioTracks()[0];
  if (localAudioTrack && input.localAudioStream) {
    peer.addTrack(localAudioTrack, input.localAudioStream);
  } else {
    peer.addTransceiver("audio", { direction: "recvonly" });
  }

  const signaling = new input.sdk.SignalingClient({
    channelARN: input.config.channelArn,
    channelEndpoint: input.config.channelEndpoint,
    clientId: input.clientId,
    role: input.sdk.Role.VIEWER,
    region: input.config.region,
    requestSigner: { getSignedURL: async () => input.config.signedWssUrl },
    enableEarlyIceCandidateBuffering: true,
  });

  peer.onicecandidate = ({ candidate }) => {
    if (candidate && !closed) signaling.sendIceCandidate(candidate);
  };
  peer.ontrack = (event) => {
    const stream = event.streams[0] ?? new MediaStream([event.track]);
    input.onStream(stream);
  };
  peer.onconnectionstatechange = () => {
    if (closed) return;
    if (peer.connectionState === "connected") {
      if (disconnectTimer !== null) window.clearTimeout(disconnectTimer);
      disconnectTimer = null;
      input.callbacks.onState("live");
    }
    if (peer.connectionState === "failed") {
      if (disconnectTimer !== null) window.clearTimeout(disconnectTimer);
      disconnectTimer = null;
      input.callbacks.onState("offline");
    }
    if (peer.connectionState === "disconnected" && disconnectTimer === null) {
      const graceMs = Math.max(0, input.disconnectGraceMs ?? 0);
      if (graceMs === 0) {
        input.callbacks.onState("offline");
        return;
      }
      input.callbacks.onState("connecting");
      disconnectTimer = window.setTimeout(() => {
        disconnectTimer = null;
        if (!closed && peer.connectionState === "disconnected") {
          input.callbacks.onState("offline");
        }
      }, graceMs);
    }
  };

  signaling.on("open", async () => {
    if (closed) return;
    input.callbacks.onState("connecting");
    try {
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      if (peer.localDescription) signaling.sendSdpOffer(peer.localDescription);
    } catch (error) {
      input.callbacks.onError(toError(error, "AWS 연결 제안을 만들지 못했습니다."));
    }
  });

  signaling.on("sdpAnswer", async (answer) => {
    if (closed) return;
    try {
      await peer.setRemoteDescription(answer);
      signaling.drainPendingIceCandidates();
      for (const candidate of queuedCandidates.splice(0)) {
        await peer.addIceCandidate(candidate);
      }
    } catch (error) {
      input.callbacks.onError(toError(error, "카메라 응답을 처리하지 못했습니다."));
    }
  });

  signaling.on("iceCandidate", (candidate) => {
    if (closed) return;
    if (peer.remoteDescription) {
      peer.addIceCandidate(candidate).catch(() => undefined);
    } else {
      queuedCandidates.push(candidate);
    }
  });
  signaling.on("close", () => {
    if (
      !closed &&
      peer.connectionState !== "connected" &&
      disconnectTimer === null
    ) {
      input.callbacks.onState("offline");
    }
  });
  signaling.on("error", (error) => {
    if (
      !closed &&
      peer.connectionState !== "connected" &&
      disconnectTimer === null
    ) {
      input.callbacks.onError(error);
    }
  });
  signaling.open();

  return {
    storageMode: false,
    close() {
      closed = true;
      if (disconnectTimer !== null) window.clearTimeout(disconnectTimer);
      disconnectTimer = null;
      signaling.close();
      peer.close();
    },
  };
}

const STORAGE_JOIN_MAX_ATTEMPTS = 6;
const STORAGE_JOIN_RETRY_MS = 6_000;
const STORAGE_JOIN_TIMEOUT_MS = 6_000;
const STORAGE_PEER_CONNECT_TIMEOUT_MS = 30_000;
const STORAGE_RENEW_MS = 55 * 60_000;
const STORAGE_RENEW_RETRY_MS = 30_000;
const STORAGE_VIDEO_MAX_BITRATE = 700_000;
const STORAGE_AUDIO_MAX_BITRATE = 32_000;

type StorageParticipantInput = {
  role: KvsRole;
  roomCode: string;
  viewerPassword?: string;
  clientId?: string;
  localStream: MediaStream | null;
  onRemoteStream?: (stream: MediaStream) => void;
  callbacks: ConnectionCallbacks;
  PeerConnection: typeof RTCPeerConnection;
  sdk: KvsSdk;
  config: KvsSessionConfig;
  onReconnectNeeded?: () => void;
  refreshConfig?: (signal: AbortSignal) => Promise<KvsSessionConfig>;
  requestStorageJoin?: (signal: AbortSignal) => Promise<void>;
};

function connectKvsStorageParticipant(input: StorageParticipantInput): KvsConnection {
  if (input.role === "VIEWER") {
    input.localStream?.getAudioTracks().forEach((track) => {
      track.enabled = false;
    });
  }
  let closed = false;
  let generation = 0;
  let connection: KvsConnection | null = null;
  let renewalTimer: number | null = null;
  let renewalAbortController: AbortController | null = null;

  const scheduleRenewal = (delay: number) => {
    if (closed) return;
    if (renewalTimer !== null) window.clearTimeout(renewalTimer);
    renewalTimer = window.setTimeout(() => {
      renewalTimer = null;
      void renew();
    }, delay);
  };

  const renew = async () => {
    if (closed) return;
    const currentGeneration = generation + 1;
    generation = currentGeneration;
    input.callbacks.onState("connecting");
    const controller = new AbortController();
    renewalAbortController = controller;

    try {
      const config = input.refreshConfig
        ? await input.refreshConfig(controller.signal)
        : await requestKvsSession(
            input.roomCode,
            input.role,
            input.clientId,
            input.viewerPassword,
            controller.signal,
          );
      if (!config.storageMode) {
        throw new Error("AWS 저장 모드가 더 이상 활성화되어 있지 않습니다.");
      }
      if (closed || generation !== currentGeneration) return;

      connection?.close();
      connection = null;
      connection = connectKvsStorageGeneration({
        ...input,
        config,
        onReconnectNeeded: () => scheduleRenewal(0),
      });
      scheduleRenewal(STORAGE_RENEW_MS);
    } catch (error) {
      if (closed || controller.signal.aborted || generation !== currentGeneration) return;
      input.callbacks.onError(toError(error, "AWS 저장 연결을 갱신하지 못했습니다."));
      scheduleRenewal(STORAGE_RENEW_RETRY_MS);
    } finally {
      if (renewalAbortController === controller) renewalAbortController = null;
    }
  };

  connection = connectKvsStorageGeneration({
    ...input,
    onReconnectNeeded: () => scheduleRenewal(0),
  });
  scheduleRenewal(STORAGE_RENEW_MS);

  return {
    storageMode: true,
    close() {
      if (closed) return;
      closed = true;
      generation += 1;
      if (renewalTimer !== null) window.clearTimeout(renewalTimer);
      renewalTimer = null;
      renewalAbortController?.abort();
      renewalAbortController = null;
      connection?.close();
      connection = null;
    },
  };
}

function connectKvsStorageGeneration(input: StorageParticipantInput): KvsConnection {
  const localTracks = getStorageLocalTracks(input.role, input.localStream);
  let closed = false;
  let peer: RTCPeerConnection | null = null;
  let remoteStream: MediaStream | null = null;
  let offerReceived = false;
  let joinAttempts = 0;
  let joinRetryTimer: number | null = null;
  let joinRequestTimer: number | null = null;
  let joinAbortController: AbortController | null = null;
  let peerConnectTimer: number | null = null;
  let reconnectRequested = false;
  const queuedCandidates: RTCIceCandidateInit[] = [];

  const signaling = new input.sdk.SignalingClient({
    channelARN: input.config.channelArn,
    channelEndpoint: input.config.channelEndpoint,
    clientId: input.role === "VIEWER" ? input.clientId : undefined,
    role: input.sdk.Role[input.role],
    region: input.config.region,
    requestSigner: { getSignedURL: async () => input.config.signedWssUrl },
    enableEarlyIceCandidateBuffering: true,
  });

  const clearJoinWork = () => {
    if (joinRetryTimer !== null) window.clearTimeout(joinRetryTimer);
    if (joinRequestTimer !== null) window.clearTimeout(joinRequestTimer);
    joinRetryTimer = null;
    joinRequestTimer = null;
    joinAbortController?.abort();
    joinAbortController = null;
  };

  const clearPeerConnectTimer = () => {
    if (peerConnectTimer !== null) window.clearTimeout(peerConnectTimer);
    peerConnectTimer = null;
  };

  const closePeer = () => {
    clearPeerConnectTimer();
    peer?.close();
    peer = null;
    queuedCandidates.length = 0;
    remoteStream?.getTracks().forEach((track) => track.stop());
    remoteStream = null;
  };

  const requestReconnect = () => {
    if (closed || reconnectRequested) return;
    reconnectRequested = true;
    input.callbacks.onState("offline");
    input.onReconnectNeeded?.();
  };

  const runJoinAttempt = async () => {
    if (closed || offerReceived) return;
    joinAttempts += 1;
    const controller = new AbortController();
    joinAbortController = controller;
    joinRequestTimer = window.setTimeout(() => controller.abort(), STORAGE_JOIN_TIMEOUT_MS);
    let failure: Error | null = null;

    try {
      if (input.requestStorageJoin) {
        await input.requestStorageJoin(controller.signal);
      } else {
        await requestKvsStorageJoin(
          input.roomCode,
          input.role,
          input.clientId,
          input.viewerPassword,
          controller.signal,
        );
      }
    } catch (error) {
      failure = toError(error, "AWS 저장 세션 참가 요청에 실패했습니다.");
    } finally {
      if (joinRequestTimer !== null) window.clearTimeout(joinRequestTimer);
      joinRequestTimer = null;
      if (joinAbortController === controller) joinAbortController = null;
    }

    if (closed || offerReceived) return;
    if (joinAttempts >= STORAGE_JOIN_MAX_ATTEMPTS) {
      input.callbacks.onError(
        failure ?? new Error("AWS 저장 세션에서 연결 제안을 받지 못했습니다."),
      );
      requestReconnect();
      return;
    }

    joinRetryTimer = window.setTimeout(() => {
      joinRetryTimer = null;
      void runJoinAttempt();
    }, STORAGE_JOIN_RETRY_MS);
  };

  signaling.on("open", () => {
    if (closed) return;
    input.callbacks.onState("connecting");
    void runJoinAttempt();
  });

  signaling.on("sdpOffer", async (offer, senderClientId) => {
    if (closed || senderClientId) return;
    offerReceived = true;
    clearJoinWork();
    closePeer();
    input.callbacks.onState("connecting");

    const nextPeer = new input.PeerConnection({ iceServers: input.config.iceServers });
    const nextRemoteStream = new MediaStream();
    peer = nextPeer;
    remoteStream = nextRemoteStream;
    peerConnectTimer = window.setTimeout(() => {
      peerConnectTimer = null;
      if (peer === nextPeer && nextPeer.connectionState !== "connected") {
        requestReconnect();
      }
    }, STORAGE_PEER_CONNECT_TIMEOUT_MS);
    localTracks.forEach((track) => {
      if (input.localStream) nextPeer.addTrack(track, input.localStream);
      else nextPeer.addTrack(track);
    });

    nextPeer.onicecandidate = ({ candidate }) => {
      if (
        !candidate ||
        closed ||
        peer !== nextPeer ||
        isHostIceCandidate(candidate)
      ) {
        return;
      }
      try {
        signaling.sendIceCandidate(
          candidate,
          undefined,
          `storage-ice-${crypto.randomUUID()}`,
        );
      } catch (error) {
        input.callbacks.onError(toError(error, "AWS에 ICE 후보를 보내지 못했습니다."));
      }
    };
    nextPeer.ontrack = ({ track }) => {
      if (closed || peer !== nextPeer) return;
      if (!nextRemoteStream.getTracks().some((entry) => entry.id === track.id)) {
        nextRemoteStream.addTrack(track);
      }
      input.onRemoteStream?.(nextRemoteStream);
    };
    nextPeer.onconnectionstatechange = () => {
      if (closed || peer !== nextPeer) return;
      if (nextPeer.connectionState === "connected") {
        clearPeerConnectTimer();
        input.callbacks.onState("live");
      }
      if (["failed", "disconnected"].includes(nextPeer.connectionState)) {
        clearPeerConnectTimer();
        requestReconnect();
      }
    };

    try {
      await nextPeer.setRemoteDescription(offer);
      if (closed || peer !== nextPeer) return;
      signaling.drainPendingIceCandidates();
      for (const candidate of queuedCandidates.splice(0)) {
        await nextPeer.addIceCandidate(candidate);
      }
      preferStorageCodecs(nextPeer);
      await limitStorageSenders(nextPeer);
      const answer = await nextPeer.createAnswer();
      await nextPeer.setLocalDescription(answer);
      if (closed || peer !== nextPeer || !nextPeer.localDescription) return;
      signaling.sendSdpAnswer(
        nextPeer.localDescription,
        undefined,
        `storage-answer-${crypto.randomUUID()}`,
      );
    } catch (error) {
      if (closed || peer !== nextPeer) return;
      input.callbacks.onError(toError(error, "AWS 저장 세션 제안을 처리하지 못했습니다."));
      requestReconnect();
    }
  });

  signaling.on("iceCandidate", (candidate, senderClientId) => {
    if (closed || senderClientId) return;
    if (peer?.remoteDescription) {
      peer.addIceCandidate(candidate).catch((error) => {
        if (!closed) {
          input.callbacks.onError(toError(error, "AWS ICE 후보를 적용하지 못했습니다."));
        }
      });
    } else {
      queuedCandidates.push(candidate);
    }
  });
  signaling.on("statusResponse", (status) => {
    if (closed || !isFailedStatusResponse(status)) return;
    input.callbacks.onError(
      new Error(
        status.description ??
          `${status.errorType ?? "AWS signaling error"}${
            status.statusCode ? ` (${status.statusCode})` : ""
          }`,
      ),
    );
    if (peer?.connectionState !== "connected") requestReconnect();
  });
  signaling.on("close", () => {
    if (closed) return;
    clearJoinWork();
    if (peer?.connectionState !== "connected") requestReconnect();
  });
  signaling.on("error", (error) => {
    if (!closed) {
      input.callbacks.onError(error);
      if (peer?.connectionState !== "connected") requestReconnect();
    }
  });
  signaling.open();

  return {
    storageMode: true,
    close() {
      closed = true;
      clearJoinWork();
      closePeer();
      signaling.resetIceCandidateState();
      signaling.close();
    },
  };
}

function getStorageLocalTracks(role: KvsRole, stream: MediaStream | null) {
  if (role === "MASTER") {
    const videoTrack = stream?.getVideoTracks()[0];
    const audioTrack = stream?.getAudioTracks()[0];
    if (!videoTrack || !audioTrack) {
      throw new Error("AWS 영상 저장에는 카메라와 마이크 입력이 모두 필요합니다.");
    }
    return [videoTrack, audioTrack];
  }

  const audioTrack = stream?.getAudioTracks()[0];
  if (!audioTrack) return [];
  return [audioTrack];
}

function preferStorageCodecs(peer: RTCPeerConnection) {
  for (const transceiver of peer.getTransceivers()) {
    const kind = transceiver.receiver.track.kind;
    if (kind !== "video" && kind !== "audio") continue;
    const mimeType = kind === "video" ? "video/h264" : "audio/opus";
    const codecs = RTCRtpReceiver.getCapabilities(kind)?.codecs.filter(
      (codec) => codec.mimeType.toLowerCase() === mimeType,
    );
    if (!codecs?.length) {
      throw new Error(`${kind === "video" ? "H.264" : "Opus"} 코덱을 지원하지 않습니다.`);
    }
    transceiver.setCodecPreferences(codecs);
  }
}

async function limitStorageSenders(peer: RTCPeerConnection) {
  for (const sender of peer.getSenders()) {
    const kind = sender.track?.kind;
    if (kind !== "video" && kind !== "audio") continue;
    const parameters = sender.getParameters();
    if (!parameters.encodings.length) {
      throw new Error("WebRTC 송출 비트레이트를 제한하지 못했습니다.");
    }
    for (const encoding of parameters.encodings) {
      encoding.maxBitrate =
        kind === "video" ? STORAGE_VIDEO_MAX_BITRATE : STORAGE_AUDIO_MAX_BITRATE;
      if (kind === "video") encoding.maxFramerate = 15;
    }
    await sender.setParameters(parameters);
  }
}

function isHostIceCandidate(candidate: RTCIceCandidate) {
  return candidate.type === "host" || /\styp host(?:\s|$)/i.test(candidate.candidate);
}

function isFailedStatusResponse(status: KvsStatusResponse) {
  if (status.success === false) return true;
  if (!status.statusCode) return false;
  const code = Number(status.statusCode);
  return Number.isFinite(code) && code >= 400;
}

async function requestKvsSession(
  roomCode: string,
  role: KvsRole,
  clientId?: string,
  viewerPassword?: string,
  signal?: AbortSignal,
) {
  const response = await fetch("/api/kvs/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ roomCode, role, clientId, viewerPassword }),
    signal,
  });
  const payload = (await response.json()) as KvsSessionConfig & { error?: string };
  if (!response.ok) throw new Error(payload.error ?? "AWS 연결 정보를 받지 못했습니다.");
  return payload;
}

async function requestAuthorizedDeviceSession(
  deviceId: string,
  clientId: string,
  signal?: AbortSignal,
  joinStorage = false,
) {
  const response = await fetch(
    `/api/devices/${encodeURIComponent(deviceId)}/live-session`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ clientId, joinStorage }),
      signal,
    },
  );
  const payload = (await response.json().catch(() => null)) as
    | (KvsSessionConfig & {
        activeSession?: { roomCode?: string };
        error?: string;
      })
    | null;
  const roomCode = payload?.activeSession?.roomCode;
  if (!response.ok || !payload || !roomCode) {
    throw new Error(payload?.error ?? "홈캠 실시간 연결 정보를 받지 못했습니다.");
  }
  return { ...payload, roomCode };
}

async function requestKvsStorageJoin(
  roomCode: string,
  role: KvsRole,
  clientId: string | undefined,
  viewerPassword: string | undefined,
  signal: AbortSignal,
) {
  const response = await fetch("/api/kvs/join", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ roomCode, role, clientId, viewerPassword }),
    signal,
  });
  const payload = (await response.json().catch(() => null)) as {
    joined?: boolean;
    error?: string;
  } | null;
  if (!response.ok || !payload?.joined) {
    throw new Error(payload?.error ?? "AWS 저장 세션에 참가하지 못했습니다.");
  }
}

async function loadKvsSdk(): Promise<KvsSdk> {
  if (!sdkPromise) {
    sdkPromise = new Promise<KvsSdk>((resolve, reject) => {
      const current = (window as typeof window & { KVSWebRTC?: KvsSdk }).KVSWebRTC;
      if (current) {
        resolve(current);
        return;
      }

      const script = document.createElement("script");
      script.src = "/vendor/kvs-webrtc.min.js";
      script.async = true;
      script.onload = () => {
        const sdk = (window as typeof window & { KVSWebRTC?: KvsSdk }).KVSWebRTC;
        if (sdk) resolve(sdk);
        else reject(new Error("AWS KVS WebRTC SDK를 불러오지 못했습니다."));
      };
      script.onerror = () => reject(new Error("AWS KVS WebRTC SDK 파일을 불러오지 못했습니다."));
      document.head.appendChild(script);
    });
  }
  return sdkPromise;
}

function toError(error: unknown, fallback: string) {
  return error instanceof Error ? error : new Error(fallback);
}

function requireRtcPeerConnection(): typeof RTCPeerConnection {
  const browserWindow = window as WebRtcWindow;
  const PeerConnection =
    browserWindow.RTCPeerConnection ?? browserWindow.webkitRTCPeerConnection;
  if (typeof PeerConnection !== "function") {
    throw new Error(
      "WebRTC 연결 기능을 찾지 못했습니다. AdGuard 등 확장 프로그램의 WebRTC 차단을 끈 뒤 새로고침해 주세요.",
    );
  }
  return PeerConnection;
}
