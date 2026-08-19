import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import {
  loadDeviceResourceConfiguration,
  resolveConfiguredDevice,
} from "../infra/aws/kvs-broker/device-config.mjs";

const REGION = "ap-northeast-2";
const ACCOUNT = "000000000000";
const arn = (kind, name, timestamp = "0000000000000") =>
  `arn:aws:kinesisvideo:${REGION}:${ACCOUNT}:${kind}/${name}/${timestamp}`;
const resources = (prefix) => ({
  p2pChannelArn: arn("channel", `${prefix}-p2p`),
  storageChannelArn: arn("channel", `${prefix}-storage`),
  streamArn: arn("stream", `${prefix}-archive`),
});

async function loadServerConfigHelper() {
  const source = await readFile(
    new URL("../app/kvs-device-config.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const commonJsModule = { exports: {} };
  runInNewContext(javascript, {
    module: commonJsModule,
    exports: commonJsModule.exports,
    Map,
    Set,
    JSON,
    Object,
    Array,
    Boolean,
    Error,
  });
  return commonJsModule.exports;
}

test("server resolves only the authenticated device mapping", async () => {
  const { resolveDeviceKvsResources, expectedKvsChannel } =
    await loadServerConfigHelper();
  const first = resources("device-a");
  const second = resources("device-b");
  const runtime = {
    KVS_DEVICE_CHANNELS_JSON: JSON.stringify({
      "device-a": first,
      "device-b": second,
    }),
  };

  const resolved = resolveDeviceKvsResources(runtime, "device-a");
  assert.deepEqual(JSON.parse(JSON.stringify(resolved)), {
    deviceId: "device-a",
    ...first,
    source: "mapping",
  });
  assert.equal(expectedKvsChannel(resolved, "p2p"), first.p2pChannelArn);
  assert.equal(
    expectedKvsChannel(resolved, "storage"),
    first.storageChannelArn,
  );
  assert.equal(resolveDeviceKvsResources(runtime, "unknown-device"), null);
});

test("server and Lambda reject shared or malformed device resources", async () => {
  const { resolveDeviceKvsResources } = await loadServerConfigHelper();
  const first = resources("device-a");
  const duplicate = {
    ...resources("device-b"),
    p2pChannelArn: first.p2pChannelArn,
  };
  const duplicateJson = JSON.stringify({
    "device-a": first,
    "device-b": duplicate,
  });

  assert.throws(
    () =>
      resolveDeviceKvsResources(
        { KVS_DEVICE_CHANNELS_JSON: duplicateJson },
        "device-a",
      ),
    /KVS_DEVICE_RESOURCE_SHARED/,
  );
  assert.throws(
    () =>
      resolveDeviceKvsResources(
        {
          KVS_DEVICE_CHANNELS_JSON: JSON.stringify({
            "device-a": {
              ...first,
              storageChannelArn: first.p2pChannelArn,
            },
          }),
        },
        "device-a",
      ),
    /KVS_DEVICE_CHANNELS_JSON_INVALID/,
  );

  const lambdaConfig = loadDeviceResourceConfiguration(
    { KVS_DEVICE_CHANNELS_JSON: duplicateJson },
    REGION,
  );
  assert.equal(lambdaConfig.error, true);
  assert.equal(lambdaConfig.errorCode, "KVS_DEVICE_RESOURCE_SHARED");
  assert.equal(resolveConfiguredDevice(lambdaConfig, "device-a"), null);
});

test("global ARN fallback is limited to the explicit PETCAM_DEVICE_ID", async () => {
  const { resolveDeviceKvsResources } = await loadServerConfigHelper();
  const legacy = resources("legacy");
  const runtime = {
    PETCAM_DEVICE_ID: "legacy-device",
    KVS_P2P_CHANNEL_ARN: legacy.p2pChannelArn,
    KVS_STORAGE_CHANNEL_ARN: legacy.storageChannelArn,
    KVS_STREAM_ARN: legacy.streamArn,
  };

  assert.equal(resolveDeviceKvsResources(runtime, "other-device"), null);
  assert.equal(
    resolveDeviceKvsResources(runtime, "legacy-device").source,
    "legacy",
  );
  assert.throws(
    () =>
      resolveDeviceKvsResources(
        { KVS_P2P_CHANNEL_ARN: legacy.p2pChannelArn },
        "legacy-device",
      ),
    /KVS_LEGACY_DEVICE_ID_REQUIRED/,
  );

  const lambdaConfig = loadDeviceResourceConfiguration(runtime, REGION);
  assert.equal(lambdaConfig.error, false);
  assert.equal(resolveConfiguredDevice(lambdaConfig, "other-device"), null);
  assert.deepEqual(
    resolveConfiguredDevice(lambdaConfig, "legacy-device"),
    legacy,
  );
});

test("all broker actions carry deviceId and validate the selected resource", async () => {
  const brokerClient = await readFile(
    new URL("../app/kvs-broker.ts", import.meta.url),
    "utf8",
  );
  const deviceRoute = await readFile(
    new URL("../app/api/device/v1/session/route.ts", import.meta.url),
    "utf8",
  );
  const viewerRoute = await readFile(
    new URL(
      "../app/api/devices/[deviceId]/live-session/route.ts",
      import.meta.url,
    ),
    "utf8",
  );
  const playbackRoute = await readFile(
    new URL(
      "../app/api/recordings/[recordingId]/playback/route.ts",
      import.meta.url,
    ),
    "utf8",
  );
  const livePlaybackRoute = await readFile(
    new URL(
      "../app/api/devices/[deviceId]/live-playback/route.ts",
      import.meta.url,
    ),
    "utf8",
  );
  const lambda = await readFile(
    new URL("../infra/aws/kvs-broker/index.mjs", import.meta.url),
    "utf8",
  );

  assert.match(brokerClient, /action:\s*"SESSION"[\s\S]*deviceId:\s*input\.deviceId/);
  assert.match(
    brokerClient,
    /action:\s*"JOIN_STORAGE"[\s\S]*deviceId:\s*input\.deviceId/,
  );
  assert.match(
    brokerClient,
    /action:\s*"DEVICE_CREDENTIALS"[\s\S]*deviceId:\s*input\.deviceId/,
  );
  assert.match(
    brokerClient,
    /action:\s*"HLS_PLAYBACK"[\s\S]*deviceId:\s*input\.deviceId/,
  );
  assert.match(
    brokerClient,
    /action:\s*"LIVE_PLAYBACK"[\s\S]*deviceId:\s*input\.deviceId/,
  );
  assert.match(deviceRoute, /resolveDeviceKvsResources\(runtime,\s*device\.deviceId\)/);
  assert.match(viewerRoute, /requestBrokerSession\(\{[\s\S]*deviceId,/);
  assert.doesNotMatch(viewerRoute, /requestBrokerJoinStorage/);
  assert.match(viewerRoute, /channelMode:\s*mode/);
  assert.match(playbackRoute, /deviceId:\s*recording\.deviceId/);
  assert.match(livePlaybackRoute, /userCanViewDevice\(deviceId,\s*userEmail\)/);
  assert.match(livePlaybackRoute, /resolveDeviceKvsResources\(runtime,\s*deviceId\)/);
  assert.match(livePlaybackRoute, /requestBrokerLivePlayback\(\{[\s\S]*deviceId,/);
  assert.match(lambda, /resolveDeviceResources\(\s*deviceResourceConfiguration,/);
  assert.match(lambda, /input\.streamArn !== resources\.streamArn/);
  assert.match(lambda, /selectChannelArn\(resources,\s*input\.channelMode\)/);
  assert.match(lambda, /streamArn:\s*channelMode === "storage" \? streamArn : null/);
  assert.match(
    lambda,
    /"kinesisvideo:DescribeMediaStorageConfiguration"/,
  );
  assert.match(lambda, /"kinesisvideo:GetDataEndpoint"/);
  assert.match(lambda, /"kinesisvideo:DescribeStream"/);
  assert.match(lambda, /"kinesisvideo:PutMedia"/);
  assert.match(
    lambda,
    /Resource:\s*streamArn/,
  );
  assert.doesNotMatch(lambda, /allowedStreamArn|selectChannelArn\(channelMode\)/);
  assert.doesNotMatch(brokerClient, /KVS_DEVICE_CHANNELS_JSON/);
});
