import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("family revocation blocks new grants and closes the cooperative live client", async () => {
  const [database, liveRoute, hlsRoute, page] = await Promise.all([
    readFile(new URL("../db/homecam.ts", import.meta.url), "utf8"),
    readFile(
      new URL(
        "../app/api/devices/[deviceId]/live-session/route.ts",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL(
        "../app/api/recordings/[recordingId]/hls/[playbackId]/[resource]/route.ts",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(database, /DELETE FROM device_memberships/);
  assert.match(database, /UPDATE push_subscriptions SET revoked_at/);
  assert.match(liveRoute, /export async function GET/);
  assert.match(liveRoute, /userCanViewDevice/);
  assert.match(hlsRoute, /getAuthorizedRecordingSession/);
  assert.match(page, /window\.setInterval\(\(\) => void verifyAccess\(\), 5_000\)/);
  assert.match(page, /홈캠 접근 권한이 해제되었습니다/);
});
