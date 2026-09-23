import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { postgresSsl } from "../db/postgres-ssl.mjs";

const projectDirectory = fileURLToPath(new URL("../", import.meta.url));
const seoulCaBundle = path.join(
  projectDirectory,
  "certs/ap-northeast-2-bundle.pem",
);

test("loads the pinned Seoul RDS root CA bundle for verify-full", () => {
  const ssl = postgresSsl({
    DATABASE_SSL_MODE: "verify-full",
    DATABASE_SSL_CA_FILE: seoulCaBundle,
  });

  assert.notEqual(ssl, false);
  assert.equal(ssl.rejectUnauthorized, true);
  assert.equal(
    ssl.ca.match(/-----BEGIN CERTIFICATE-----/g)?.length,
    3,
  );
  assert.equal(
    createHash("sha256").update(ssl.ca).digest("hex"),
    "913fb5b814f17af79d4c1622584a8d0ceddf5b0d76fe353d0c7d1186cdd6b229",
  );
});

test("does not read a CA file when database TLS is disabled", () => {
  assert.equal(postgresSsl({ DATABASE_SSL_MODE: "disable" }), false);
});

test("requires a readable valid CA bundle for verify-full", async () => {
  assert.throws(
    () => postgresSsl({ DATABASE_SSL_MODE: "verify-full" }),
    /DATABASE_SSL_CA_FILE is required/,
  );
  assert.throws(
    () =>
      postgresSsl({
        DATABASE_SSL_MODE: "verify-full",
        DATABASE_SSL_CA_FILE: "/file/that/does/not/exist.pem",
      }),
    /DATABASE_SSL_CA_FILE could not be read/,
  );

  const temporaryDirectory = await mkdtemp(
    path.join(os.tmpdir(), "malbut-rds-ca-"),
  );
  try {
    const invalidBundle = path.join(temporaryDirectory, "invalid.pem");
    await writeFile(invalidBundle, "not a certificate\n", { mode: 0o600 });
    assert.throws(
      () =>
        postgresSsl({
          DATABASE_SSL_MODE: "verify-full",
          DATABASE_SSL_CA_FILE: invalidBundle,
        }),
      /not a valid PEM certificate bundle/,
    );
  } finally {
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});
