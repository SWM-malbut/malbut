import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function loadContract(relativePath) {
  const source = await readFile(new URL(relativePath, import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const exports = {};
  runInNewContext(javascript, {
    Buffer,
    crypto: globalThis.crypto,
    Date,
    exports,
    JSON,
    module: { exports },
    Object,
    Reflect,
    TextEncoder,
    Uint8Array,
    WeakSet,
  });
  return exports;
}

function robotMap(overrides = {}) {
  return {
    finalized: true,
    revision: "revision-1",
    mapId: "map-1",
    mapRevision: "map-revision-1",
    sourceCreatedAt: null,
    geometry: {
      width: 10,
      height: 10,
      resolution: 0.05,
      originX: 0,
      originY: 0,
      originYaw: 0,
    },
    previewBase64: "iVBORw0KGgo=",
    userMap: { rooms: [] },
    semanticZones: null,
    ...overrides,
  };
}

function nestedObject(depth) {
  let value = "leaf";
  for (let index = 0; index < depth; index += 1) value = { child: value };
  return value;
}

test("semantic map upload bounds depth, node count, size, and JSON shape", async () => {
  const contract = await loadContract("../app/robot-contract.ts");

  assert.ok(contract.parseRobotMap(robotMap({ userMap: nestedObject(32) })));
  assert.equal(
    contract.parseRobotMap(robotMap({ userMap: nestedObject(33) })),
    null,
  );
  assert.equal(
    contract.parseRobotMap(robotMap({
      userMap: { values: new Array(100_000).fill(null) },
    })),
    null,
  );
  assert.equal(
    contract.parseRobotMap(robotMap({
      userMap: { value: "x".repeat(1_500_001) },
    })),
    null,
  );

  const circular = {};
  circular.self = circular;
  assert.doesNotThrow(() => contract.parseRobotMap(robotMap({ userMap: circular })));
  assert.equal(contract.parseRobotMap(robotMap({ userMap: circular })), null);
});

test("Agent canonicalization rejects repository trees that bypass upload", async () => {
  const contract = await loadContract("../app/agent-semantic-contract.ts");
  const binding = await contract.parseAgentSemanticBinding({
    AGENT_SEMANTIC_SECRET: "s".repeat(64),
    AGENT_SEMANTIC_SIGNING_SECRET: "k".repeat(64),
    AGENT_SEMANTIC_AGENT_USER_ID: "local-user",
    AGENT_SEMANTIC_USER_EMAIL: "owner@example.com",
    AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "subject-1",
    AGENT_SEMANTIC_DEVICE_ID: "robot-1",
  });

  await assert.rejects(
    contract.buildAgentSemanticEnvelope(binding, {
      membershipGeneration: "1",
      mapGeneration: "1",
      deviceRevision: "revision-1",
      semantics: nestedObject(33),
    }),
    /AGENT_SEMANTIC_SNAPSHOT_INVALID/,
  );

  const circular = {};
  circular.self = circular;
  assert.throws(
    () => contract.canonicalJson(circular),
    /AGENT_SEMANTIC_JSON_INVALID/,
  );
});

test("Agent and Homecam bindings accept visible ASCII credentials only", async () => {
  const contract = await loadContract("../app/agent-semantic-contract.ts");
  const runtime = {
    AGENT_SEMANTIC_SECRET: "s".repeat(64),
    AGENT_SEMANTIC_SIGNING_SECRET: "k".repeat(64),
    AGENT_SEMANTIC_AGENT_USER_ID: "local-user",
    AGENT_SEMANTIC_USER_EMAIL: "owner@example.com",
    AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "subject-1",
    AGENT_SEMANTIC_DEVICE_ID: "robot-1",
  };
  const invalidBindings = [
    { AGENT_SEMANTIC_SECRET: `${"s".repeat(31)} ${"s".repeat(32)}` },
    { AGENT_SEMANTIC_SECRET: `${"s".repeat(63)}é` },
    { AGENT_SEMANTIC_SIGNING_SECRET: `${"k".repeat(63)}한` },
    { AGENT_SEMANTIC_AGENT_USER_ID: "사용자" },
    { AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "주체-1" },
    { AGENT_SEMANTIC_DEVICE_ID: "로봇-1" },
  ];
  for (const override of invalidBindings) {
    assert.equal(
      await contract.parseAgentSemanticBinding({ ...runtime, ...override }),
      null,
    );
  }
  assert.equal(
    await contract.authorizedAgentSemanticRequest(
      `Bearer ${"s".repeat(63)}é`,
      runtime.AGENT_SEMANTIC_SECRET,
    ),
    false,
  );

  const binding = await contract.parseAgentSemanticBinding(runtime);
  const envelope = await contract.buildAgentSemanticEnvelope(binding, {
    membershipGeneration: "9007199254740993",
    mapGeneration: "9223372036854775807",
    deviceRevision: "revision-1",
    semantics: { userMap: {}, zones: null },
  });
  assert.equal(envelope.authorizationRevision, "auth-9007199254740993");
  assert.equal(envelope.mapGeneration, "9223372036854775807");
});
