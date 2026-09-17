export type DeviceKvsEnvironment = {
  KVS_DEVICE_CHANNELS_JSON?: string;
  PETCAM_DEVICE_ID?: string;
  KVS_CHANNEL_ARN?: string;
  KVS_P2P_CHANNEL_ARN?: string;
  KVS_STORAGE_CHANNEL_ARN?: string;
  KVS_STREAM_ARN?: string;
};

export type DeviceKvsResources = {
  deviceId: string;
  p2pChannelArn: string | null;
  storageChannelArn: string | null;
  streamArn: string | null;
  source: "mapping" | "legacy";
};

type MappedDeviceKvsResources = {
  p2pChannelArn: string;
  storageChannelArn: string;
  streamArn: string;
};

const DEVICE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const KVS_ARN_PATTERN =
  /^arn:(aws(?:-us-gov|-cn)?):kinesisvideo:([a-z0-9-]+):(\d{12}):(channel|stream)\/([A-Za-z0-9_.-]{1,256})\/(\d+)$/;
const MAPPING_KEYS = [
  "p2pChannelArn",
  "storageChannelArn",
  "streamArn",
] as const;

export function resolveDeviceKvsResources(
  runtime: DeviceKvsEnvironment,
  deviceId: string,
): DeviceKvsResources | null {
  if (!DEVICE_ID_PATTERN.test(deviceId)) {
    throw new Error("KVS_DEVICE_ID_INVALID");
  }

  const mapping = parseDeviceKvsMapping(runtime.KVS_DEVICE_CHANNELS_JSON);
  const mapped = mapping.get(deviceId);
  if (mapped) {
    return {
      deviceId,
      ...mapped,
      source: "mapping",
    };
  }

  const legacyDeviceId = runtime.PETCAM_DEVICE_ID?.trim();
  const hasLegacyResource = [
    runtime.KVS_CHANNEL_ARN,
    runtime.KVS_P2P_CHANNEL_ARN,
    runtime.KVS_STORAGE_CHANNEL_ARN,
    runtime.KVS_STREAM_ARN,
  ].some((value) => Boolean(value?.trim()));
  if (!hasLegacyResource) return null;
  if (!legacyDeviceId || !DEVICE_ID_PATTERN.test(legacyDeviceId)) {
    throw new Error("KVS_LEGACY_DEVICE_ID_REQUIRED");
  }

  const legacy = {
    p2pChannelArn: optionalKvsArn(
      runtime.KVS_P2P_CHANNEL_ARN ?? runtime.KVS_CHANNEL_ARN,
      "channel",
    ),
    storageChannelArn: optionalKvsArn(
      runtime.KVS_STORAGE_CHANNEL_ARN ?? runtime.KVS_CHANNEL_ARN,
      "channel",
    ),
    streamArn: optionalKvsArn(runtime.KVS_STREAM_ARN, "stream"),
  };
  assertPartialAwsScope(legacy);
  assertLegacyResourcesAreIsolated(mapping, legacyDeviceId, legacy);
  if (deviceId !== legacyDeviceId) return null;
  return {
    deviceId,
    ...legacy,
    source: "legacy",
  };
}

export function expectedKvsChannel(
  resources: DeviceKvsResources,
  mode: "p2p" | "storage",
): string | null {
  return mode === "storage"
    ? resources.storageChannelArn
    : resources.p2pChannelArn;
}

function parseDeviceKvsMapping(
  source: string | undefined,
): Map<string, MappedDeviceKvsResources> {
  if (!source?.trim()) return new Map();

  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  if (!isRecord(value)) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }

  const mapping = new Map<string, MappedDeviceKvsResources>();
  const channelOwners = new Map<string, string>();
  const streamOwners = new Map<string, string>();
  for (const [deviceId, rawResources] of Object.entries(value)) {
    if (!DEVICE_ID_PATTERN.test(deviceId) || !isRecord(rawResources)) {
      throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
    }
    const keys = Object.keys(rawResources);
    if (
      keys.length !== MAPPING_KEYS.length ||
      keys.some(
        (key) => !MAPPING_KEYS.includes(key as (typeof MAPPING_KEYS)[number]),
      )
    ) {
      throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
    }

    const resources = {
      p2pChannelArn: requiredKvsArn(rawResources.p2pChannelArn, "channel"),
      storageChannelArn: requiredKvsArn(
        rawResources.storageChannelArn,
        "channel",
      ),
      streamArn: requiredKvsArn(rawResources.streamArn, "stream"),
    };
    if (resources.p2pChannelArn === resources.storageChannelArn) {
      throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
    }
    assertSameAwsScope(resources);
    claimResource(channelOwners, resources.p2pChannelArn, deviceId);
    claimResource(channelOwners, resources.storageChannelArn, deviceId);
    claimResource(streamOwners, resources.streamArn, deviceId);
    mapping.set(deviceId, resources);
  }
  return mapping;
}

function assertLegacyResourcesAreIsolated(
  mapping: Map<string, MappedDeviceKvsResources>,
  legacyDeviceId: string,
  legacy: Omit<DeviceKvsResources, "deviceId" | "source">,
) {
  for (const [mappedDeviceId, resources] of mapping) {
    if (mappedDeviceId === legacyDeviceId) continue;
    const mappedChannels = new Set([
      resources.p2pChannelArn,
      resources.storageChannelArn,
    ]);
    if (
      (legacy.p2pChannelArn &&
        mappedChannels.has(legacy.p2pChannelArn)) ||
      (legacy.storageChannelArn &&
        mappedChannels.has(legacy.storageChannelArn)) ||
      (legacy.streamArn && legacy.streamArn === resources.streamArn)
    ) {
      throw new Error("KVS_DEVICE_RESOURCE_SHARED");
    }
  }
}

function requiredKvsArn(value: unknown, expectedKind: "channel" | "stream") {
  if (typeof value !== "string" || value.trim() !== value) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  const parsed = parseKvsArn(value);
  if (!parsed || parsed.kind !== expectedKind) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  return value;
}

function optionalKvsArn(
  value: string | undefined,
  expectedKind: "channel" | "stream",
) {
  if (!value?.trim()) return null;
  if (value.trim() !== value) throw new Error("KVS_LEGACY_ARN_INVALID");
  const parsed = parseKvsArn(value);
  if (!parsed || parsed.kind !== expectedKind) {
    throw new Error("KVS_LEGACY_ARN_INVALID");
  }
  return value;
}

function parseKvsArn(value: string) {
  const match = KVS_ARN_PATTERN.exec(value);
  if (!match) return null;
  return {
    partition: match[1],
    region: match[2],
    accountId: match[3],
    kind: match[4] as "channel" | "stream",
  };
}

function assertSameAwsScope(resources: MappedDeviceKvsResources) {
  assertAwsScope([
    resources.p2pChannelArn,
    resources.storageChannelArn,
    resources.streamArn,
  ]);
}

function assertPartialAwsScope(
  resources: Omit<DeviceKvsResources, "deviceId" | "source">,
) {
  assertAwsScope(
    [
      resources.p2pChannelArn,
      resources.storageChannelArn,
      resources.streamArn,
    ].filter((arn): arn is string => Boolean(arn)),
  );
}

function assertAwsScope(arns: string[]) {
  const scopes = arns.map(parseKvsArn);
  const [first] = scopes;
  if (
    first &&
    scopes.some(
      (scope) =>
        !scope ||
        scope.partition !== first.partition ||
        scope.region !== first.region ||
        scope.accountId !== first.accountId,
    )
  ) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
}

function claimResource(
  owners: Map<string, string>,
  arn: string,
  deviceId: string,
) {
  const owner = owners.get(arn);
  if (owner && owner !== deviceId) {
    throw new Error("KVS_DEVICE_RESOURCE_SHARED");
  }
  owners.set(arn, deviceId);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
