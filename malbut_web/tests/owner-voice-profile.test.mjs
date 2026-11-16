import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const { parseVoiceProfile, defaultVoiceProfile } = moduleLoader()("app/voice-profile.ts");
const custom = { endpointMinSilenceS: 1.2, silenceTimeoutS: 3, piperSpeed: 1.1, speechVolumePercent: 55 };

test("voice controls accept the timing and output limits and reject incomplete or crossed values", () => {
  for (const valid of [defaultVoiceProfile,
    { endpointMinSilenceS: 0.4, silenceTimeoutS: 1.2, piperSpeed: 0.5, speechVolumePercent: 0 },
    { endpointMinSilenceS: 2, silenceTimeoutS: 5, piperSpeed: 2, speechVolumePercent: 100 },
  ]) assert.deepEqual(parseVoiceProfile(valid), valid);
  for (const value of [null, {}, { ...custom, name: "another owner" }, { ...custom, piperSpeed: false },
    { ...custom, piperSpeed: 2.1 }, { ...custom, endpointMinSilenceS: 2, silenceTimeoutS: 1.9 },
    { ...custom, endpointMinSilenceS: 0.3 }, { ...custom, silenceTimeoutS: 5.1 },
    { ...custom, speechVolumePercent: 0.1 }, { ...custom, speechVolumePercent: -1 },
  ]) assert.equal(parseVoiceProfile(value), null);
});

test("authenticated owner settings follow registration across devices and only a current report confirms them", async () => {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ({}) },
    [path.join(h.root, "app/server-auth.ts")]: { getRequestUserId: async (r) => r.headers.get("x-user") },
    [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async (r) => {
      const deviceId = r.headers.get("x-device");
      return deviceId ? { deviceId } : null;
    } },
  });
  const saveRoute = load("app/api/devices/[deviceId]/voice-profile/route.ts");
  const stateRoute = load("app/api/device/v1/robot/state/route.ts");
  const robot = load("db/robot-map.ts");
  const deviceId = "robot-a";
  const owner = "u-owner", family = "u-family";
  const save = (user, body = custom, origin = "https://test") => saveRoute.PUT(new Request("https://test/api", {
    method: "PUT", headers: { "content-type": "application/json", origin, ...(user && { "x-user": user }) },
    body: JSON.stringify(body),
  }), { params: Promise.resolve({ deviceId }) });
  const beat = async (device, extra = {}) => {
    const response = await stateRoute.POST(new Request("https://test/api", {
      method: "POST", headers: { "content-type": "application/json", ...(device && { "x-device": device }) },
      body: JSON.stringify({ state: "idle", message: "준비", pose: null, nav2: {}, target: null,
        localization: { state: "unknown", tfAgeS: null }, mapRevision: 0, observedAt: new Date().toISOString(), ...extra }),
    }));
    assert.equal(response.status, device ? 202 : 401);
    return response.json();
  };
  const view = async (user = owner) => (await robot.getRobotSnapshot(deviceId, user)).voiceSettings;
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      await h.db.query("INSERT INTO device_memberships(device_id,user_id,role) VALUES('robot-b',$1,'owner')", [owner]);
      await beat(null);
      assert.equal("voiceProfile" in await beat(deviceId), false);
      assert.equal((await view()).applied, null);
      assert.equal((await save(null)).status, 401);
      assert.equal((await save(family)).status, 403);
      assert.equal((await save(owner, custom, "https://other")).status, 403);
      assert.equal((await save(owner, { ...custom, silenceTimeoutS: 1 })).status, 400);
      assert.equal((await save(owner)).status, 200);
      assert.equal(await view(family), undefined);
      assert.deepEqual((await view()).profile, custom);
      assert.equal((await view()).applied, false);
      assert.deepEqual((await beat(deviceId)).voiceProfile, custom);
      assert.deepEqual((await beat("robot-b")).voiceProfile, custom);
      await beat(deviceId, { appliedVoiceProfile: custom });
      assert.equal((await view()).applied, true);
      assert.equal((await robot.getRobotSnapshot("robot-b", owner)).voiceSettings.applied, false);
      await h.db.query("UPDATE robot_runtime_state SET observed_at=CURRENT_TIMESTAMP-INTERVAL '20 seconds' WHERE device_id=$1", [deviceId]);
      assert.equal((await view()).applied, false);
      for (const extra of [{}, { appliedVoiceProfile: null }, { appliedVoiceProfile: { ...custom, piperSpeed: 7 } }]) {
        await beat(deviceId, { appliedVoiceProfile: custom });
        await beat(deviceId, extra);
        assert.equal((await view()).applied, false);
      }
      await h.db.query("DELETE FROM device_memberships WHERE device_id=$1 AND user_id=$2", [deviceId, owner]);
      await h.db.query("UPDATE device_memberships SET role='owner' WHERE device_id=$1 AND user_id=$2", [deviceId, family]);
      assert.equal((await save(owner)).status, 403);
      assert.deepEqual((await beat(deviceId, { appliedVoiceProfile: custom })).voiceProfile, defaultVoiceProfile);
      assert.equal((await view(family)).applied, false);
      await beat(deviceId, { appliedVoiceProfile: defaultVoiceProfile });
      assert.equal((await view(family)).applied, true);
      assert.deepEqual((await beat("robot-b")).voiceProfile, custom);
    });
  } finally { await h.db.close(); }
});
