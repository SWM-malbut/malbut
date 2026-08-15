import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import {
  GetDataEndpointCommand,
  GetSignalingChannelEndpointCommand,
  KinesisVideoClient,
} from "@aws-sdk/client-kinesis-video";
import {
  GetHLSStreamingSessionURLCommand,
  KinesisVideoArchivedMediaClient,
  ListFragmentsCommand,
} from "@aws-sdk/client-kinesis-video-archived-media";
import {
  GetIceServerConfigCommand,
  KinesisVideoSignalingClient,
} from "@aws-sdk/client-kinesis-video-signaling";
import {
  JoinStorageSessionAsViewerCommand,
  JoinStorageSessionCommand,
  KinesisVideoWebRTCStorageClient,
} from "@aws-sdk/client-kinesis-video-webrtc-storage";
import { AssumeRoleCommand, STSClient } from "@aws-sdk/client-sts";
import {
  loadDeviceResourceConfiguration,
  resolveConfiguredDevice as resolveDeviceResources,
} from "./device-config.mjs";

const region = process.env.AWS_REGION;
const sharedSecret = process.env.BROKER_SHARED_SECRET;
const deviceRoleArn = process.env.KVS_DEVICE_ROLE_ARN;
const deviceRoleExternalId = process.env.KVS_DEVICE_ROLE_EXTERNAL_ID;
const kinesisVideo = new KinesisVideoClient({ region });
const sts = new STSClient({ region });
const clientIdPattern = /^(?!AWS_)[A-Za-z0-9_-]{1,128}$/;
const deviceIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const isoTimestampPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const maxHlsPlaybackRangeMs = 60 * 60 * 1000;
const deviceResourceConfiguration = loadDeviceResourceConfiguration(
  process.env,
  region,
);

export async function handler(event) {
  if (event.requestContext?.http?.method !== "POST") {
    return response(405, { error: "Method not allowed" });
  }
  if (
    !region ||
    !sharedSecret ||
    deviceResourceConfiguration.error ||
    !deviceResourceConfiguration.hasAnyDevice
  ) {
    return response(503, { error: "Broker is not configured" });
  }

  const rawBody = event.isBase64Encoded
    ? Buffer.from(event.body ?? "", "base64").toString("utf8")
    : event.body ?? "";
  if (!verifyRequest(event.headers ?? {}, rawBody, sharedSecret)) {
    return response(401, { error: "Unauthorized" });
  }

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return response(400, { error: "Invalid JSON" });
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return response(400, { error: "Invalid request" });
  }

  const action = payload.action ?? "SESSION";
  if (
    ![
      "SESSION",
      "JOIN_STORAGE",
      "HLS_PLAYBACK",
      "EVENT_PLAYBACK",
      "DEVICE_CREDENTIALS",
    ].includes(
      action,
    )
  ) {
    return response(400, { error: "Invalid action" });
  }

  if (action === "HLS_PLAYBACK" || action === "EVENT_PLAYBACK") {
    const input = validateHlsPlaybackInput(payload);
    if (!input) return response(400, { error: "Invalid HLS playback request" });
    const resources = resolveDeviceResources(
      deviceResourceConfiguration,
      input.deviceId,
    );
    if (!resources) {
      return response(403, { error: "Device is not allowed" });
    }
    if (!resources.streamArn) {
      return response(503, { error: "HLS playback is not configured" });
    }
    if (input.streamArn !== resources.streamArn) {
      return response(403, { error: "Stream is not allowed" });
    }

    try {
      return response(
        200,
        action === "EVENT_PLAYBACK"
          ? await createEventPlayback(input)
          : await createHlsPlayback(input),
      );
    } catch (error) {
      console.error(
        "KVS HLS playback failed",
        error instanceof Error ? error.name : "UnknownError",
      );
      if (error instanceof Error && error.name === "ResourceNotFoundException") {
        return response(404, { error: "No media was found for the requested range" });
      }
      return response(502, { error: "HLS playback could not be created" });
    }
  }

  if (action === "DEVICE_CREDENTIALS") {
    const input = validateDeviceCredentialInput(payload);
    if (!input) return response(400, { error: "Invalid device credential request" });
    if (!deviceRoleArn) {
      return response(503, { error: "Device role is not configured" });
    }
    const resources = resolveDeviceResources(
      deviceResourceConfiguration,
      input.deviceId,
    );
    if (!resources) {
      return response(403, { error: "Device is not allowed" });
    }
    const selectedChannelArn = selectChannelArn(resources, input.channelMode);
    if (!selectedChannelArn) {
      return response(503, { error: "Requested channel is not configured" });
    }
    if (input.channelMode === "storage" && !resources.streamArn) {
      return response(503, { error: "Storage is not configured" });
    }
    try {
      return response(
        200,
        await createDeviceCredentials({
          ...input,
          channelArn: selectedChannelArn,
          streamArn: resources.streamArn,
        }),
      );
    } catch (error) {
      console.error(
        "KVS device credential request failed",
        error instanceof Error ? error.name : "UnknownError",
      );
      return response(502, { error: "Device credentials could not be created" });
    }
  }

  const participant = validateParticipant(payload, payload.action !== undefined);
  if (!participant) return response(400, { error: "Invalid participant" });
  const channelMode = validateChannelMode(payload.channelMode);
  if (!channelMode) {
    return response(400, { error: "Invalid channel mode" });
  }
  const resources = resolveDeviceResources(
    deviceResourceConfiguration,
    participant.deviceId,
  );
  if (!resources) {
    return response(403, { error: "Device is not allowed" });
  }
  const selectedChannelArn = selectChannelArn(resources, channelMode);
  if (!selectedChannelArn) {
    return response(503, { error: "Requested channel is not configured" });
  }

  if (action === "JOIN_STORAGE") {
    if (channelMode !== "storage") {
      return response(400, { error: "Storage join requires storage channel" });
    }
    try {
      await joinStorageSession(
        participant.role,
        participant.clientId,
        selectedChannelArn,
      );
      return response(200, {
        joined: true,
        role: participant.role,
        channelArn: selectedChannelArn,
      });
    } catch (error) {
      console.error(
        "KVS storage join failed",
        error instanceof Error ? error.name : "UnknownError",
      );
      return response(502, { error: "KVS storage session could not be joined" });
    }
  }

  try {
    const session = await createSignalingSession(
      participant.role,
      participant.clientId,
      selectedChannelArn,
    );
    return response(200, session);
  } catch (error) {
    console.error(
      "KVS broker request failed",
      error instanceof Error ? error.name : "UnknownError",
    );
    return response(502, { error: "KVS session could not be created" });
  }
}

async function createSignalingSession(role, clientId, channelArn) {
  const endpoints = await getChannelEndpoints(role, ["WSS", "HTTPS"], channelArn);
  if (!endpoints.WSS || !endpoints.HTTPS) throw new Error("Missing KVS endpoint");

  const signaling = new KinesisVideoSignalingClient({
    region,
    endpoint: endpoints.HTTPS,
  });
  const iceResult = await signaling.send(
    new GetIceServerConfigCommand({ ChannelARN: channelArn }),
  );
  const iceServers = [
    { urls: "stun:stun.kinesisvideo." + region + ".amazonaws.com:443" },
    ...(iceResult.IceServerList ?? []).map((server) => ({
      urls: server.Uris ?? [],
      username: server.Username,
      credential: server.Password,
    })),
  ];

  const credentials = await kinesisVideo.config.credentials();
  const queryParams = { "X-Amz-ChannelARN": channelArn };
  if (role === "VIEWER") queryParams["X-Amz-ClientId"] = clientId;
  const signedWssUrl = signKvsWebSocketUrl(
    endpoints.WSS,
    queryParams,
    credentials,
    region,
  );

  return {
    role,
    region,
    channelArn,
    channelEndpoint: endpoints.WSS,
    signedWssUrl,
    iceServers,
    expiresAt: new Date(Date.now() + 299_000).toISOString(),
  };
}

async function joinStorageSession(role, clientId, channelArn) {
  const endpoints = await getChannelEndpoints(role, ["WEBRTC"], channelArn);
  if (!endpoints.WEBRTC) throw new Error("Missing KVS WebRTC endpoint");

  const storage = new KinesisVideoWebRTCStorageClient({
    region,
    endpoint: endpoints.WEBRTC,
  });
  if (role === "MASTER") {
    await storage.send(new JoinStorageSessionCommand({ channelArn }));
  } else {
    await storage.send(
      new JoinStorageSessionAsViewerCommand({ channelArn, clientId }),
    );
  }
}

async function createDeviceCredentials({
  deviceId,
  channelMode,
  channelArn,
  streamArn,
}) {
  const sessionName = `homecam-${createHash("sha256")
    .update(deviceId)
    .digest("hex")
    .slice(0, 24)}`;
  const actions = [
    "kinesisvideo:DescribeSignalingChannel",
    "kinesisvideo:GetSignalingChannelEndpoint",
    "kinesisvideo:GetIceServerConfig",
    "kinesisvideo:ConnectAsMaster",
  ];
  if (channelMode === "storage") {
    actions.push(
      "kinesisvideo:DescribeMediaStorageConfiguration",
      "kinesisvideo:JoinStorageSession",
    );
  }
  const statements = [
    {
      Effect: "Allow",
      Action: actions,
      Resource: channelArn,
    },
  ];
  if (channelMode === "storage") {
    statements.push({
      Effect: "Allow",
      Action: [
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:DescribeStream",
        "kinesisvideo:PutMedia",
      ],
      Resource: streamArn,
    });
  }
  const policy = JSON.stringify({
    Version: "2012-10-17",
    Statement: statements,
  });
  const result = await sts.send(
    new AssumeRoleCommand({
      RoleArn: deviceRoleArn,
      RoleSessionName: sessionName,
      DurationSeconds: 900,
      Policy: policy,
      ...(deviceRoleExternalId ? { ExternalId: deviceRoleExternalId } : {}),
    }),
  );
  const credentials = result.Credentials;
  if (
    !credentials?.AccessKeyId ||
    !credentials.SecretAccessKey ||
    !credentials.SessionToken ||
    !credentials.Expiration
  ) {
    throw new Error("Missing STS credentials");
  }
  return {
    role: "MASTER",
    region,
    channelArn,
    streamArn: channelMode === "storage" ? streamArn : null,
    channelMode,
    credentials: {
      accessKeyId: credentials.AccessKeyId,
      secretAccessKey: credentials.SecretAccessKey,
      sessionToken: credentials.SessionToken,
      expiresAt: credentials.Expiration.toISOString(),
    },
  };
}

async function createHlsPlayback({ streamArn, startAt, endAt, expiresSeconds }) {
  const endpointResult = await kinesisVideo.send(
    new GetDataEndpointCommand({
      APIName: "GET_HLS_STREAMING_SESSION_URL",
      StreamARN: streamArn,
    }),
  );
  if (!endpointResult.DataEndpoint) {
    throw new Error("Missing KVS archived media endpoint");
  }

  const endpoint = new URL(endpointResult.DataEndpoint);
  if (endpoint.protocol !== "https:" || endpoint.search || endpoint.hash) {
    throw new Error("Invalid KVS archived media endpoint");
  }

  const archivedMedia = new KinesisVideoArchivedMediaClient({
    region,
    endpoint: endpointResult.DataEndpoint,
  });
  const expiresAt = new Date(Date.now() + expiresSeconds * 1000).toISOString();
  const playback = await archivedMedia.send(
    new GetHLSStreamingSessionURLCommand({
      StreamARN: streamArn,
      PlaybackMode: "ON_DEMAND",
      HLSFragmentSelector: {
        FragmentSelectorType: "SERVER_TIMESTAMP",
        TimestampRange: {
          StartTimestamp: new Date(startAt),
          EndTimestamp: new Date(endAt),
        },
      },
      ContainerFormat: "FRAGMENTED_MP4",
      Expires: expiresSeconds,
      MaxMediaPlaylistFragmentResults: 5000,
    }),
  );
  if (!playback.HLSStreamingSessionURL) {
    throw new Error("Missing HLS playback URL");
  }

  return {
    playbackUrl: playback.HLSStreamingSessionURL,
    expiresAt,
    streamArn,
  };
}

async function createEventPlayback(input) {
  const listEndpoint = await kinesisVideo.send(
    new GetDataEndpointCommand({
      APIName: "LIST_FRAGMENTS",
      StreamARN: input.streamArn,
    }),
  );
  if (!listEndpoint.DataEndpoint) {
    throw new Error("Missing KVS fragment endpoint");
  }
  const endpoint = new URL(listEndpoint.DataEndpoint);
  if (endpoint.protocol !== "https:" || endpoint.search || endpoint.hash) {
    throw new Error("Invalid KVS fragment endpoint");
  }
  const archivedMedia = new KinesisVideoArchivedMediaClient({
    region,
    endpoint: listEndpoint.DataEndpoint,
  });
  const requestedStartMs = Date.parse(input.startAt);
  const result = await archivedMedia.send(
    new ListFragmentsCommand({
      StreamARN: input.streamArn,
      FragmentSelector: {
        FragmentSelectorType: "SERVER_TIMESTAMP",
        TimestampRange: {
          StartTimestamp: new Date(requestedStartMs - 20_000),
          EndTimestamp: new Date(input.endAt),
        },
      },
      MaxResults: 1000,
    }),
  );
  const fragments = (result.Fragments ?? [])
    .filter((fragment) => fragment.ServerTimestamp instanceof Date)
    .sort(
      (left, right) =>
        left.ServerTimestamp.getTime() - right.ServerTimestamp.getTime(),
    );
  if (fragments.length === 0) {
    const error = new Error("No fragments found for event");
    error.name = "ResourceNotFoundException";
    throw error;
  }
  const containing = [...fragments].reverse().find((fragment) => {
    const fragmentStart = fragment.ServerTimestamp.getTime();
    const duration = Number(fragment.FragmentLengthInMilliseconds ?? 0);
    return fragmentStart <= requestedStartMs && fragmentStart + duration >= requestedStartMs;
  });
  const firstAfter = fragments.find(
    (fragment) => fragment.ServerTimestamp.getTime() >= requestedStartMs,
  );
  const aligned = containing ?? firstAfter ?? fragments.at(-1);
  const alignedStartAt = aligned.ServerTimestamp.toISOString();
  const playback = await createHlsPlayback({ ...input, startAt: alignedStartAt });
  return { ...playback, alignedStartAt };
}

async function getChannelEndpoints(role, protocols, channelArn) {
  const endpointResult = await kinesisVideo.send(
    new GetSignalingChannelEndpointCommand({
      ChannelARN: channelArn,
      SingleMasterChannelEndpointConfiguration: {
        Protocols: protocols,
        Role: role,
      },
    }),
  );
  return Object.fromEntries(
    (endpointResult.ResourceEndpointList ?? [])
      .filter((entry) => entry.Protocol && entry.ResourceEndpoint)
      .map((entry) => [entry.Protocol, entry.ResourceEndpoint]),
  );
}

function validateParticipant(payload, strict) {
  if (
    strict &&
    !hasOnlyKeys(payload, [
      "action",
      "deviceId",
      "role",
      "clientId",
      "channelMode",
    ])
  ) {
    return null;
  }
  if (
    typeof payload.deviceId !== "string" ||
    !deviceIdPattern.test(payload.deviceId)
  ) {
    return null;
  }
  const role = payload.role;
  if (!["MASTER", "VIEWER"].includes(role)) return null;

  if (role === "MASTER") {
    if (strict && Object.hasOwn(payload, "clientId")) return null;
    return { deviceId: payload.deviceId, role, clientId: undefined };
  }

  if (typeof payload.clientId !== "string") return null;
  const clientId = payload.clientId.trim();
  if ((strict && clientId !== payload.clientId) || !clientIdPattern.test(clientId)) {
    return null;
  }
  return { deviceId: payload.deviceId, role, clientId };
}

function validateDeviceCredentialInput(payload) {
  if (
    !hasOnlyKeys(payload, ["action", "deviceId", "channelMode"]) ||
    typeof payload.deviceId !== "string" ||
    !deviceIdPattern.test(payload.deviceId)
  ) {
    return null;
  }
  const channelMode = validateChannelMode(payload.channelMode);
  if (!channelMode) return null;
  return { deviceId: payload.deviceId, channelMode };
}

function validateChannelMode(value) {
  if (value === undefined) return null;
  return value === "p2p" || value === "storage" ? value : null;
}

function selectChannelArn(resources, channelMode) {
  if (channelMode === "p2p") return resources.p2pChannelArn;
  if (channelMode === "storage") return resources.storageChannelArn;
  return null;
}

function validateHlsPlaybackInput(payload) {
  if (
    !hasOnlyKeys(payload, [
      "action",
      "deviceId",
      "streamArn",
      "startAt",
      "endAt",
      "expiresSeconds",
    ])
  ) {
    return null;
  }
  if (
    typeof payload.deviceId !== "string" ||
    !deviceIdPattern.test(payload.deviceId) ||
    typeof payload.streamArn !== "string" ||
    typeof payload.startAt !== "string" ||
    typeof payload.endAt !== "string" ||
    !Number.isInteger(payload.expiresSeconds) ||
    payload.expiresSeconds < 300 ||
    payload.expiresSeconds > 43_200 ||
    !isCanonicalTimestamp(payload.startAt) ||
    !isCanonicalTimestamp(payload.endAt)
  ) {
    return null;
  }

  const start = Date.parse(payload.startAt);
  const end = Date.parse(payload.endAt);
  if (end <= start || end - start > maxHlsPlaybackRangeMs) return null;
  return {
    deviceId: payload.deviceId,
    streamArn: payload.streamArn,
    startAt: payload.startAt,
    endAt: payload.endAt,
    expiresSeconds: payload.expiresSeconds,
  };
}

function isCanonicalTimestamp(value) {
  if (!isoTimestampPattern.test(value)) return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString() === value;
}

function hasOnlyKeys(value, allowedKeys) {
  return Object.keys(value).every((key) => allowedKeys.includes(key));
}

function signKvsWebSocketUrl(endpoint, queryParams, credentials, signingRegion) {
  const url = new URL(endpoint);
  if (url.protocol !== "wss:" || url.search) throw new Error("Invalid KVS WebSocket endpoint");

  const now = new Date();
  const dateTime = now.toISOString().replace(/\.\d{3}Z$/, "Z").replace(/[:-]/g, "");
  const date = dateTime.slice(0, 8);
  const scope = `${date}/${signingRegion}/kinesisvideo/aws4_request`;
  const signedHeaders = "host";
  const canonical = {
    ...queryParams,
    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
    "X-Amz-Credential": `${credentials.accessKeyId}/${scope}`,
    "X-Amz-Date": dateTime,
    "X-Amz-Expires": "299",
    "X-Amz-SignedHeaders": signedHeaders,
  };
  if (credentials.sessionToken) canonical["X-Amz-Security-Token"] = credentials.sessionToken;

  const canonicalQuery = encodeQuery(canonical);
  const payloadHash = createHash("sha256").update("").digest("hex");
  const canonicalRequest = [
    "GET",
    url.pathname || "/",
    canonicalQuery,
    `host:${url.host}\n`,
    signedHeaders,
    payloadHash,
  ].join("\n");
  const stringToSign = [
    "AWS4-HMAC-SHA256",
    dateTime,
    scope,
    createHash("sha256").update(canonicalRequest).digest("hex"),
  ].join("\n");

  const kDate = hmac(`AWS4${credentials.secretAccessKey}`, date);
  const kRegion = hmac(kDate, signingRegion);
  const kService = hmac(kRegion, "kinesisvideo");
  const kSigning = hmac(kService, "aws4_request");
  const signature = createHmac("sha256", kSigning).update(stringToSign).digest("hex");
  return `wss://${url.host}${url.pathname || "/"}?${encodeQuery({ ...canonical, "X-Amz-Signature": signature })}`;
}

function hmac(key, value) {
  return createHmac("sha256", key).update(value).digest();
}

function encodeQuery(values) {
  return Object.keys(values)
    .sort()
    .map((key) => `${key}=${encodeURIComponent(values[key])}`)
    .join("&");
}

function verifyRequest(headers, rawBody, secret) {
  const timestamp = headers["x-petcam-timestamp"];
  const signature = headers["x-petcam-signature"];
  if (!timestamp || !signature || !/^\d{10}$/.test(timestamp) || !/^[a-f0-9]{64}$/.test(signature)) {
    return false;
  }
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 30) return false;

  const expected = createHmac("sha256", secret).update(`${timestamp}.${rawBody}`).digest();
  const received = Buffer.from(signature, "hex");
  return received.length === expected.length && timingSafeEqual(received, expected);
}

function response(statusCode, body) {
  return {
    statusCode,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
    body: JSON.stringify(body),
  };
}
