import { readFile } from "node:fs/promises";

const [backendUrl, manifestPath] = process.argv.slice(2);
const secret = process.env.DEVICE_PROVISIONING_SECRET;
if (!backendUrl || !manifestPath || !secret) {
  throw new Error(
    "Usage: DEVICE_PROVISIONING_SECRET=... node scripts/apply-provisioning.mjs <https-backend-url> <manifest.json>",
  );
}
if (secret.length < 43) throw new Error("DEVICE_PROVISIONING_SECRET is invalid");

const endpoint = new URL("/api/internal/device-provisioning", backendUrl);
if (
  endpoint.protocol !== "https:" &&
  !["127.0.0.1", "localhost", "::1"].includes(endpoint.hostname)
) {
  throw new Error("Provisioning requires HTTPS except on loopback development hosts");
}
const manifest = await readFile(manifestPath, "utf8");
const response = await fetch(endpoint, {
  method: "POST",
  headers: {
    authorization: `Bearer ${secret}`,
    "content-type": "application/json",
  },
  body: manifest,
  redirect: "error",
  signal: AbortSignal.timeout(20_000),
});
const body = await response.text();
if (!response.ok) {
  throw new Error(`Provisioning failed (${response.status}): ${body.slice(0, 500)}`);
}
process.stdout.write(`${body}\n`);
