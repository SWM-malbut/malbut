import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

test("provisioning bundle writes a private token without printing it", async () => {
  const output = await mkdtemp(path.join(tmpdir(), "homecam-provisioning-"));
  const result = spawnSync(
    process.execPath,
    [
      "scripts/create-provisioning-bundle.mjs",
      "--device-id",
      "malbut-sim-01",
      "--display-name",
      "MALBUT simulator",
      "--owner-email",
      "OWNER@EXAMPLE.TEST",
      "--output",
      output,
    ],
    { cwd: new URL("..", import.meta.url), encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);

  const token = (await readFile(path.join(output, "device-token"), "utf8")).trim();
  const manifest = JSON.parse(
    await readFile(path.join(output, "manifest.json"), "utf8"),
  );
  const runtime = JSON.parse(
    await readFile(path.join(output, "runtime-values.json"), "utf8"),
  );
  assert.match(token, /^hc1\.[0-9a-f-]{36}\.[0-9a-f]{64}$/);
  assert.equal(result.stdout.includes(token), false);
  assert.equal(manifest.ownerEmail, "owner@example.test");
  assert.equal(
    manifest.credential.tokenDigest,
    createHash("sha256").update(token).digest("hex"),
  );
  assert.equal(
    runtime.DEVICE_PROVISIONING_MANIFEST_SHA256,
    createHash("sha256").update(JSON.stringify(manifest)).digest("hex"),
  );
  assert.equal(
    (await stat(path.join(output, "device-token"))).mode & 0o777,
    0o600,
  );
  assert.equal(
    (await stat(path.join(output, "manifest.json"))).mode & 0o777,
    0o600,
  );
});

test("provisioning bundle refuses to overwrite an existing token", async () => {
  const output = await mkdtemp(path.join(tmpdir(), "homecam-provisioning-"));
  const arguments_ = [
    "scripts/create-provisioning-bundle.mjs",
    "--device-id",
    "malbut-sim-01",
    "--display-name",
    "MALBUT simulator",
    "--owner-email",
    "owner@example.test",
    "--output",
    output,
  ];
  assert.equal(
    spawnSync(process.execPath, arguments_, {
      cwd: new URL("..", import.meta.url),
    }).status,
    0,
  );
  const second = spawnSync(process.execPath, arguments_, {
    cwd: new URL("..", import.meta.url),
    encoding: "utf8",
  });
  assert.notEqual(second.status, 0);
  assert.match(second.stderr, /EEXIST/);
});
