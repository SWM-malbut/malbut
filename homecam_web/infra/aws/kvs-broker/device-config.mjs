const deviceIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const kvsArnPattern =
  /^arn:(aws(?:-us-gov|-cn)?):kinesisvideo:([a-z0-9-]+):(\d{12}):(channel|stream)\/([A-Za-z0-9_.-]{1,256})\/(\d+)$/;

export function loadDeviceResourceConfiguration(environment, region) {
  try {
    const mapping = parseDeviceResourceMapping(
      environment.KVS_DEVICE_CHANNELS_JSON,
      region,
    );
    const legacy = parseLegacyDeviceResources(environment, region);
    if (legacy) {
      assertLegacyResourcesAreIsolated(
        mapping,
        legacy.deviceId,
        legacy.resources,
      );
    }
    return {
      error: false,
      mapping,
      legacyDeviceId: legacy?.deviceId ?? null,
      legacyResources: legacy?.resources ?? null,
      hasAnyDevice: mapping.size > 0 || Boolean(legacy),
    };
  } catch (error) {
    return {
      error: true,
      errorCode: error instanceof Error ? error.message : "UnknownError",
      mapping: new Map(),
      legacyDeviceId: null,
      legacyResources: null,
      hasAnyDevice: false,
    };
  }
}

export function resolveConfiguredDevice(configuration, deviceId) {
  if (!deviceIdPattern.test(deviceId)) return null;
  const mapped = configuration.mapping.get(deviceId);
  if (mapped) return mapped;
  if (deviceId === configuration.legacyDeviceId) {
    return configuration.legacyResources;
  }
  return null;
}

function parseDeviceResourceMapping(source, region) {
  if (!source?.trim()) return new Map();
  let value;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  if (!isRecord(value)) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }

  const mapping = new Map();
  const channelOwners = new Map();
  const streamOwners = new Map();
  const allowedKeys = [
    "p2pChannelArn",
    "storageChannelArn",
    "streamArn",
  ];
  for (const [deviceId, rawResources] of Object.entries(value)) {
    if (!deviceIdPattern.test(deviceId) || !isRecord(rawResources)) {
      throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
    }
    const keys = Object.keys(rawResources);
    if (
      keys.length !== allowedKeys.length ||
      keys.some((key) => !allowedKeys.includes(key))
    ) {
      throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
    }
    const resources = {
      p2pChannelArn: requiredKvsArn(
        rawResources.p2pChannelArn,
        "channel",
        region,
      ),
      storageChannelArn: requiredKvsArn(
        rawResources.storageChannelArn,
        "channel",
        region,
      ),
      streamArn: requiredKvsArn(rawResources.streamArn, "stream", region),
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

function parseLegacyDeviceResources(environment, region) {
  const legacyChannelArn = environment.KVS_CHANNEL_ARN;
  const values = [
    legacyChannelArn,
    environment.KVS_P2P_CHANNEL_ARN,
    environment.KVS_STORAGE_CHANNEL_ARN,
    environment.KVS_STREAM_ARN,
  ];
  if (!values.some((value) => Boolean(value?.trim()))) return null;

  const deviceId = environment.PETCAM_DEVICE_ID?.trim();
  if (!deviceId || !deviceIdPattern.test(deviceId)) {
    throw new Error("KVS_LEGACY_DEVICE_ID_REQUIRED");
  }
  const resources = {
    p2pChannelArn: optionalKvsArn(
      environment.KVS_P2P_CHANNEL_ARN ?? legacyChannelArn,
      "channel",
      region,
    ),
    storageChannelArn: optionalKvsArn(
      environment.KVS_STORAGE_CHANNEL_ARN ?? legacyChannelArn,
      "channel",
      region,
    ),
    streamArn: optionalKvsArn(environment.KVS_STREAM_ARN, "stream", region),
  };
  assertPartialAwsScope(resources);
  return { deviceId, resources };
}

function requiredKvsArn(value, expectedKind, region) {
  if (typeof value !== "string" || value.trim() !== value) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  const parsed = parseKvsArn(value);
  if (!parsed || parsed.kind !== expectedKind || parsed.region !== region) {
    throw new Error("KVS_DEVICE_CHANNELS_JSON_INVALID");
  }
  return value;
}

function optionalKvsArn(value, expectedKind, region) {
  if (!value?.trim()) return null;
  if (value.trim() !== value) throw new Error("KVS_LEGACY_ARN_INVALID");
  const parsed = parseKvsArn(value);
  if (!parsed || parsed.kind !== expectedKind || parsed.region !== region) {
    throw new Error("KVS_LEGACY_ARN_INVALID");
  }
  return value;
}

function parseKvsArn(value) {
  const match = kvsArnPattern.exec(value);
  if (!match) return null;
  return {
    partition: match[1],
    region: match[2],
    accountId: match[3],
    kind: match[4],
  };
}

function assertSameAwsScope(resources) {
  assertAwsScope([
    resources.p2pChannelArn,
    resources.storageChannelArn,
    resources.streamArn,
  ]);
}

function assertPartialAwsScope(resources) {
  assertAwsScope(
    [
      resources.p2pChannelArn,
      resources.storageChannelArn,
      resources.streamArn,
    ].filter(Boolean),
  );
}

function assertAwsScope(arns) {
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

function claimResource(owners, arn, deviceId) {
  const owner = owners.get(arn);
  if (owner && owner !== deviceId) {
    throw new Error("KVS_DEVICE_RESOURCE_SHARED");
  }
  owners.set(arn, deviceId);
}

function assertLegacyResourcesAreIsolated(mapping, legacyDeviceId, legacy) {
  for (const [mappedDeviceId, resources] of mapping) {
    if (mappedDeviceId === legacyDeviceId) continue;
    const channels = new Set([
      resources.p2pChannelArn,
      resources.storageChannelArn,
    ]);
    if (
      (legacy.p2pChannelArn && channels.has(legacy.p2pChannelArn)) ||
      (legacy.storageChannelArn && channels.has(legacy.storageChannelArn)) ||
      (legacy.streamArn && legacy.streamArn === resources.streamArn)
    ) {
      throw new Error("KVS_DEVICE_RESOURCE_SHARED");
    }
  }
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
