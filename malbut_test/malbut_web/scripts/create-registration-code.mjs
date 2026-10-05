const [backendUrl, deviceId] = process.argv.slice(2);
const secret = process.env.DEVICE_PROVISIONING_SECRET;
if (!backendUrl || !deviceId || !secret) {
  throw new Error(
    "Usage: DEVICE_PROVISIONING_SECRET=... node scripts/create-registration-code.mjs <https-backend-url> <device-id>",
  );
}
if (secret.length < 43) throw new Error("DEVICE_PROVISIONING_SECRET is invalid");

const endpoint = new URL("/api/internal/registration-codes", backendUrl);
if (
  endpoint.protocol !== "https:" &&
  !["127.0.0.1", "localhost", "::1"].includes(endpoint.hostname)
) {
  throw new Error("Registration codes require HTTPS except on loopback development hosts");
}
const response = await fetch(endpoint, {
  method: "POST",
  headers: {
    authorization: `Bearer ${secret}`,
    "content-type": "application/json",
  },
  body: JSON.stringify({ deviceId }),
  redirect: "error",
  signal: AbortSignal.timeout(20_000),
});
const body = await response.text();
if (!response.ok) {
  throw new Error(`Registration code failed (${response.status}): ${body.slice(0, 500)}`);
}
const { code, expiresAt } = JSON.parse(body);
// The server keeps only a digest: this is the one time the code is shown.
process.stdout.write(`등록 코드: ${code}\n유효 기간: ${expiresAt}까지 (한 번만 쓸 수 있어요)\n`);
