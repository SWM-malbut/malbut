import { getRuntimeEnvironment } from "./runtime-env";

export type KvsRole = "MASTER" | "VIEWER";
export type KvsChannelMode = "p2p" | "storage";

export type KvsBrokerSession = {
  role: KvsRole;
  region: string;
  channelArn: string;
  channelEndpoint: string;
  signedWssUrl: string;
  iceServers: RTCIceServer[];
  expiresAt: string;
};

export type KvsBrokerJoin = {
  joined: true;
  role: KvsRole;
  channelArn: string;
};

export type KvsBrokerPlayback = {
  playbackUrl: string;
  expiresAt: string;
  streamArn: string;
};

export type KvsBrokerLivePlayback = KvsBrokerPlayback;

export type KvsBrokerEventPlayback = KvsBrokerPlayback & {
  alignedStartAt: string;
};

export type KvsBrokerDeviceCredentials = {
  role: "MASTER";
  region: string;
  channelArn: string;
  streamArn: string | null;
  channelMode: KvsChannelMode;
  credentials: {
    accessKeyId: string;
    secretAccessKey: string;
    sessionToken: string;
    expiresAt: string;
  };
};

type BrokerEnv = {
  KVS_BROKER_URL?: string;
  KVS_BROKER_SECRET?: string;
};

export async function requestBrokerSession(input: {
  deviceId: string;
  role: KvsRole;
  clientId?: string;
  channelMode: KvsChannelMode;
}): Promise<KvsBrokerSession> {
  const payload = (await requestBroker({
    action: "SESSION",
    deviceId: input.deviceId,
    role: input.role,
    ...(input.clientId ? { clientId: input.clientId } : {}),
    channelMode: input.channelMode,
  })) as Partial<KvsBrokerSession>;
  if (
    payload.role !== input.role ||
    typeof payload.region !== "string" ||
    typeof payload.channelArn !== "string" ||
    typeof payload.channelEndpoint !== "string" ||
    typeof payload.signedWssUrl !== "string" ||
    !Array.isArray(payload.iceServers) ||
    typeof payload.expiresAt !== "string"
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }

  return payload as KvsBrokerSession;
}

export async function requestBrokerJoinStorage(input: {
  deviceId: string;
  role: KvsRole;
  clientId?: string;
  channelMode: "storage";
}): Promise<KvsBrokerJoin> {
  const payload = (await requestBroker({
    action: "JOIN_STORAGE",
    deviceId: input.deviceId,
    role: input.role,
    ...(input.clientId ? { clientId: input.clientId } : {}),
    ...(input.channelMode ? { channelMode: input.channelMode } : {}),
  })) as Partial<KvsBrokerJoin>;
  if (
    payload.joined !== true ||
    payload.role !== input.role ||
    typeof payload.channelArn !== "string"
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }
  return payload as KvsBrokerJoin;
}

export async function requestBrokerDeviceCredentials(input: {
  deviceId: string;
  channelMode: KvsChannelMode;
}): Promise<KvsBrokerDeviceCredentials> {
  const payload = (await requestBroker({
    action: "DEVICE_CREDENTIALS",
    deviceId: input.deviceId,
    channelMode: input.channelMode,
  })) as Partial<KvsBrokerDeviceCredentials>;
  const credentials = payload.credentials;
  if (
    payload.role !== "MASTER" ||
    typeof payload.region !== "string" ||
    typeof payload.channelArn !== "string" ||
    payload.channelMode !== input.channelMode ||
    (payload.streamArn !== null && typeof payload.streamArn !== "string") ||
    !credentials ||
    typeof credentials.accessKeyId !== "string" ||
    typeof credentials.secretAccessKey !== "string" ||
    typeof credentials.sessionToken !== "string" ||
    typeof credentials.expiresAt !== "string"
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }
  return payload as KvsBrokerDeviceCredentials;
}

export async function requestBrokerPlayback(input: {
  deviceId: string;
  streamArn: string;
  startAt: string;
  endAt: string;
  expiresSeconds: number;
}): Promise<KvsBrokerPlayback> {
  const payload = (await requestBroker({
    action: "HLS_PLAYBACK",
    deviceId: input.deviceId,
    streamArn: input.streamArn,
    startAt: input.startAt,
    endAt: input.endAt,
    expiresSeconds: input.expiresSeconds,
  })) as Partial<KvsBrokerPlayback>;
  if (
    typeof payload.playbackUrl !== "string" ||
    !payload.playbackUrl.startsWith("https://") ||
    typeof payload.expiresAt !== "string" ||
    payload.streamArn !== input.streamArn
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }
  return payload as KvsBrokerPlayback;
}

export async function requestBrokerEventPlayback(input: {
  deviceId: string;
  streamArn: string;
  startAt: string;
  endAt: string;
  expiresSeconds: number;
}): Promise<KvsBrokerEventPlayback> {
  const payload = (await requestBroker({
    action: "EVENT_PLAYBACK",
    ...input,
  })) as Partial<KvsBrokerEventPlayback>;
  if (
    typeof payload.playbackUrl !== "string" ||
    !payload.playbackUrl.startsWith("https://") ||
    typeof payload.expiresAt !== "string" ||
    payload.streamArn !== input.streamArn ||
    typeof payload.alignedStartAt !== "string" ||
    !Number.isFinite(Date.parse(payload.alignedStartAt))
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }
  return payload as KvsBrokerEventPlayback;
}

export async function requestBrokerLivePlayback(input: {
  deviceId: string;
  streamArn: string;
  expiresSeconds: number;
}): Promise<KvsBrokerLivePlayback> {
  const payload = (await requestBroker({
    action: "LIVE_PLAYBACK",
    deviceId: input.deviceId,
    streamArn: input.streamArn,
    expiresSeconds: input.expiresSeconds,
  })) as Partial<KvsBrokerLivePlayback>;
  if (
    typeof payload.playbackUrl !== "string" ||
    !payload.playbackUrl.startsWith("https://") ||
    typeof payload.expiresAt !== "string" ||
    payload.streamArn !== input.streamArn
  ) {
    throw new Error("KVS_BROKER_RESPONSE_INVALID");
  }
  return payload as KvsBrokerLivePlayback;
}

async function requestBroker(input: object) {
  const runtime = getRuntimeEnvironment() as BrokerEnv;
  const brokerUrl = runtime.KVS_BROKER_URL;
  const secret = runtime.KVS_BROKER_SECRET;
  if (!brokerUrl || !secret) throw new Error("KVS_BROKER_NOT_CONFIGURED");
  if (!brokerUrl.startsWith("https://")) throw new Error("KVS_BROKER_URL_INVALID");

  const body = JSON.stringify(input);
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const signature = await sign(`${timestamp}.${body}`, secret);
  const response = await fetch(brokerUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-petcam-timestamp": timestamp,
      "x-petcam-signature": signature,
    },
    body,
    signal: AbortSignal.timeout(10_000),
  });

  if (!response.ok) throw new Error(`KVS_BROKER_${response.status}`);
  return response.json();
}

async function sign(message: string, secret: string) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const bytes = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)),
  );
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
