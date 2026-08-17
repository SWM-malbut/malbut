import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function loadContract() {
  const source = await readFile(
    new URL("../app/agent-semantic-contract.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const exports = {};
  runInNewContext(javascript, {
    crypto: globalThis.crypto,
    Date,
    exports,
    JSON,
    module: { exports },
    Object,
    TextEncoder,
    Uint8Array,
  });
  return exports;
}

const serviceSecret = "s".repeat(64);
const signingSecret = "k".repeat(64);

test("Agent semantic binding stays server-side and envelope omits raw identity", async () => {
  const contract = await loadContract();
  const binding = await contract.parseAgentSemanticBinding({
    AGENT_SEMANTIC_SECRET: serviceSecret,
    AGENT_SEMANTIC_SIGNING_SECRET: signingSecret,
    AGENT_SEMANTIC_AGENT_USER_ID: "local-user",
    AGENT_SEMANTIC_USER_EMAIL: "Owner@Example.com",
    AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "cognito-subject-0001",
    AGENT_SEMANTIC_DEVICE_ID: "malbut-sim-01",
  });
  assert.equal(binding.userEmail, "owner@example.com");
  assert.equal(binding.principalSubject, "cognito-subject-0001");
  assert.match(binding.principalSubjectDigest, /^[0-9a-f]{64}$/);
  assert.equal(
    await contract.authorizedAgentSemanticRequest(
      `Bearer ${serviceSecret}`,
      binding.serviceSecret,
    ),
    true,
  );
  assert.equal(
    await contract.authorizedAgentSemanticRequest(
      `Bearer ${"x".repeat(64)}`,
      binding.serviceSecret,
    ),
    false,
  );
  const request = {
    schemaVersion: 1,
    agentUserId: binding.agentUserId,
    principalSubjectDigest: binding.principalSubjectDigest,
    deviceId: binding.deviceId,
  };
  assert.equal(contract.validAgentSemanticRequest(request, binding), true);
  assert.equal(
    contract.validAgentSemanticRequest(
      { ...request, userEmail: binding.userEmail },
      binding,
    ),
    false,
  );

  const envelope = await contract.buildAgentSemanticEnvelope(
    binding,
    {
      membershipGeneration: "4",
      mapGeneration: "9",
      deviceRevision: "device-revision-a",
      semantics: {
        revision: "device-revision-a",
        mapId: "map-1",
        mapRevision: "map-revision-1",
        userMap: { format: "malbut-user-map-v1" },
        zones: null,
      },
    },
    1_000_000,
  );
  assert.equal(envelope.issuer, "malbut-homecam-web");
  assert.equal(envelope.audience, "malbut-agent-semantic-v1");
  assert.equal(envelope.authorizationRevision, "auth-4");
  assert.equal(envelope.mapGeneration, "9");
  assert.equal(envelope.expiresAtMs - envelope.issuedAtMs, 5_000);
  assert.match(
    JSON.parse(envelope.semanticsJson).revision,
    /^srv-9-[0-9a-f]{16}$/,
  );
  assert.doesNotMatch(JSON.stringify(envelope), /owner@example\.com/);
  assert.doesNotMatch(JSON.stringify(envelope), /cognito-subject-0001/);
  assert.doesNotMatch(JSON.stringify(envelope), new RegExp(serviceSecret));
  assert.doesNotMatch(JSON.stringify(envelope), new RegExp(signingSecret));
  assert.equal("principalSubject" in envelope, false);
  assert.equal("serviceSecret" in envelope, false);

  const signedFields = { ...envelope };
  delete signedFields.signature;
  delete signedFields.semanticsJson;
  const expected = createHmac("sha256", signingSecret)
    .update(contract.canonicalJson(signedFields))
    .digest("hex");
  assert.equal(envelope.signature, expected);

  assert.equal(await contract.parseAgentSemanticBinding({
    AGENT_SEMANTIC_SECRET: serviceSecret,
    AGENT_SEMANTIC_SIGNING_SECRET: serviceSecret,
    AGENT_SEMANTIC_AGENT_USER_ID: "local-user",
    AGENT_SEMANTIC_USER_EMAIL: "owner@example.com",
    AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "subject-1",
    AGENT_SEMANTIC_DEVICE_ID: "malbut-sim-01",
  }), null);
});

test("server map generation distinguishes an A to B to A restoration", async () => {
  const contract = await loadContract();
  const binding = await contract.parseAgentSemanticBinding({
    AGENT_SEMANTIC_SECRET: serviceSecret,
    AGENT_SEMANTIC_SIGNING_SECRET: signingSecret,
    AGENT_SEMANTIC_AGENT_USER_ID: "local-user",
    AGENT_SEMANTIC_USER_EMAIL: "owner@example.com",
    AGENT_SEMANTIC_PRINCIPAL_SUBJECT: "subject-1",
    AGENT_SEMANTIC_DEVICE_ID: "robot-1",
  });
  const snapshot = {
    membershipGeneration: "1",
    deviceRevision: "same-device-revision",
    semantics: {
      revision: "same-device-revision",
      mapId: "map-a",
      mapRevision: "map-a-revision",
      userMap: {},
      zones: null,
    },
  };
  const first = await contract.buildAgentSemanticEnvelope(
    binding,
    { ...snapshot, mapGeneration: "10" },
    2_000_000,
  );
  const restored = await contract.buildAgentSemanticEnvelope(
    binding,
    { ...snapshot, mapGeneration: "12" },
    2_000_001,
  );
  assert.notEqual(
    JSON.parse(first.semanticsJson).revision,
    JSON.parse(restored.semanticsJson).revision,
  );
  assert.notEqual(first.signature, restored.signature);
});

test("the internal route binds fixed identity through an active web session", async () => {
  const [route, contract, migration, repository] = await Promise.all([
    readFile(
      new URL("../app/api/internal/agent/semantic/route.ts", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/agent-semantic-contract.ts", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../db/migrations/0005_agent_semantic_binding.sql", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../db/robot-map.ts", import.meta.url), "utf8"),
  ]);
  assert.match(route, /parseAgentSemanticBinding\(getRuntimeEnvironment\(\)\)/);
  assert.match(contract, /AGENT_SEMANTIC_SECRET/);
  assert.match(route, /getAgentRobotMapSemantics/);
  assert.match(route, /binding\.principalSubject/);
  assert.doesNotMatch(route, /getRequestUserEmail|cookie/i);
  assert.match(repository, /memberships\.role = 'owner'/);
  assert.match(repository, /INNER JOIN robot_maps/);
  assert.match(repository, /EXISTS \([\s\S]*FROM web_auth_sessions AS sessions/);
  assert.match(repository, /sessions\.cognito_sub = \?/);
  assert.match(repository, /sessions\.user_email = \?/);
  assert.match(repository, /sessions\.revoked_at IS NULL/);
  assert.match(repository, /sessions\.expires_at > CURRENT_TIMESTAMP/);
  assert.doesNotMatch(repository, /token_digest/);
  assert.match(migration, /device_membership_generation_seq/);
  assert.match(migration, /robot_map_generation_seq/);
});
