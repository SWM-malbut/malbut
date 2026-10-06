import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const SECRET = "k".repeat(40);
const ORIGIN = "https://homecam.example.com";
const ENV = { FALL_KEY_ENCRYPTION_SECRET: SECRET, AUTH_PUBLIC_ORIGIN: ORIGIN };
const OPENAI_KEY = "sk-proj-test-abcdefgh1234";

/** The services, answered in-process; `reply` decides each answer. */
function fakeServices() {
  // By default every service says yes (KMA answers with its "00" header).
  const services = { calls: [], reply: (target) => target.hostname === "apis.data.go.kr"
    ? Response.json({ response: { header: { resultCode: "00" } } }) : Response.json({}) };
  services.fetch = async (url, init) => {
    const target = new URL(String(url));
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    services.calls.push({ url: `${target.origin}${target.pathname}`, query: target.searchParams,
      auth: init?.headers?.authorization ?? null, body });
    return services.reply(target, body);
  };
  return services;
}

async function withKeys(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ENV, getRuntimeValue: (name) => ENV[name] },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(request) { return request.headers.get("x-test-user"); } },
    [path.join(h.root, "app/device-auth.ts")]: { async getRequestDevice(request) {
      return request.headers.get("authorization") === "Bearer device-a" ? { deviceId: "robot-a" } : null;
    } },
  });
  const pg = load("db/postgres.ts");
  const services = fakeServices();
  load("app/service-key-check.ts").setKeyCheckFetchForTest(services.fetch);
  const list = load("app/api/devices/[deviceId]/service-keys/route.ts");
  const one = load("app/api/devices/[deviceId]/service-keys/[service]/route.ts");
  const robotRoute = load("app/api/device/v1/service-keys/route.ts");
  const ask = (userId, method, service, body, origin = ORIGIN) => {
    const request = new Request(`${ORIGIN}/api/devices/robot-a/service-keys${service ? `/${service}` : ""}`, {
      method,
      headers: { ...(userId ? { "x-test-user": userId } : {}), ...(method === "GET" ? {} : { "content-type": "application/json", origin }) },
      ...(method === "GET" ? {} : { body: JSON.stringify(body ?? {}) }),
    });
    return service
      ? one[method](request, { params: Promise.resolve({ deviceId: "robot-a", service }) })
      : list.GET(request, { params: Promise.resolve({ deviceId: "robot-a" }) });
  };
  const robot = (body, { token = "device-a", deviceId = "robot-a" } = {}) => robotRoute.POST(new Request(`${ORIGIN}/api/device/v1/service-keys`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "x-malbut-device-id": deviceId, "content-type": "application/json" },
    body: JSON.stringify(body),
  }));
  const sync = (known = { openai: 0, kma: 0 }, extra = {}) =>
    robot({ known, models: { openai: null }, health: {}, ...extra }).then((response) => response.json());
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, load, services, ask, robot, sync }));
  } finally { await h.db.close(); }
}

test("owners save a key only after the service accepts it; the key is sealed and never shown again", async () => {
  await withKeys(async ({ h, services, ask }) => {
    assert.equal((await ask("u-family", "GET")).status, 403, "guardians do not see this screen");
    assert.equal((await ask("u-family", "PUT", "openai", { apiKey: OPENAI_KEY })).status, 403);
    assert.equal((await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY }, "https://evil.example")).status, 403);
    assert.equal((await ask("u-owner", "PUT", "openai", { apiKey: "has a space" })).status, 400);
    assert.equal((await ask("u-owner", "PUT", "weather", { apiKey: OPENAI_KEY })).status, 404);
    assert.equal(services.calls.length, 0, "nothing reaches the service before the owner and the form are checked");

    const saved = await ask("u-owner", "PUT", "openai", { apiKey: ` ${OPENAI_KEY} ` });
    assert.equal(saved.status, 200);
    const text = await saved.text();
    assert.doesNotMatch(text, /sk-proj/);
    assert.deepEqual(JSON.parse(text), { configured: true, last4: "1234", keyVersion: 1,
      updatedAt: JSON.parse(text).updatedAt, robotHasCurrent: false, robotModel: null });
    // One short response from the model the robot uses (its default until it reports one).
    assert.equal(services.calls[0].url, "https://api.openai.com/v1/responses");
    assert.equal(services.calls[0].auth, `Bearer ${OPENAI_KEY}`);
    assert.deepEqual(services.calls[0].body, { model: "gpt-5.6-luna", input: "1", max_output_tokens: 16 });

    const row = (await h.db.query("SELECT ciphertext, last4, key_version FROM device_service_keys WHERE service='openai'")).rows[0];
    assert.match(row.ciphertext, /^v1\./);
    assert.ok(!row.ciphertext.includes("abcdefgh"));
    const views = await (await ask("u-owner", "GET")).json();
    assert.equal(views.openai.configured, true);
    assert.equal(views.kma.configured, false);
    assert.equal(views.fall.configured, false);
  });
});

test("a key the service turns down is not saved, with a reason the owner can act on", async () => {
  await withKeys(async ({ h, services, ask }) => {
    const answer = (status, body = {}) => () => Response.json(body, { status });
    const put = async (service, apiKey) => {
      const response = await ask("u-owner", "PUT", service, { apiKey });
      return { status: response.status, body: await response.json() };
    };
    for (const [reply, result] of [
      [answer(401), "invalid"],
      [answer(429, { error: { type: "insufficient_quota", code: "insufficient_quota" } }), "quota"],
      [answer(429, { error: { code: "rate_limit_exceeded" } }), "unavailable"],
      [answer(404), "no_model"],
      [answer(400, { error: { code: "model_not_found" } }), "no_model"],
      [answer(503), "unavailable"],
      [() => { throw new Error("offline"); }, "unavailable"],
    ]) {
      services.reply = reply;
      const rejected = await put("openai", OPENAI_KEY);
      assert.equal(rejected.status, 422, result);
      assert.equal(rejected.body.result, result);
      assert.match(rejected.body.error, /저장하지 않았어요/);
    }
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM device_service_keys")).rows[0].n, 0);
    await h.db.exec("DELETE FROM request_rate_limits"); // the next group starts a fresh minute
    // A 400 for any other reason means the key itself was accepted.
    services.reply = answer(400, { error: { code: "invalid_value" } });
    assert.equal((await put("openai", OPENAI_KEY)).status, 200);

    // KMA: key problems come back as XML; a new key that is not active yet is turned down with a hint.
    services.reply = () => new Response("<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>");
    const notYet = await put("kma", "abc%2Bdef%3D%3D1234");
    assert.equal(notYet.body.result, "invalid");
    assert.match(notYet.body.error, /막 받은 키라면/);
    const kmaCall = services.calls.at(-1);
    assert.equal(kmaCall.url, "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst");
    assert.equal(kmaCall.query.get("serviceKey"), "abc+def==1234", "an encoded key is decoded like the robot does");
    assert.match(kmaCall.query.get("base_date"), /^\d{8}$/);
    assert.match(kmaCall.query.get("base_time"), /^\d{2}00$/);
    services.reply = () => Response.json({ response: { header: { resultCode: "22" } } });
    assert.equal((await put("kma", "abcdefgh1234")).body.result, "quota");
    services.reply = () => Response.json({ response: { header: { resultCode: "00" } } });
    assert.equal((await put("kma", "abcdefgh1234")).status, 200);

    // The fall key goes through the same check, with Ollama and the robot's fall model.
    services.reply = answer(401);
    assert.equal((await put("fall", "ollama-key-5678")).body.result, "invalid");
    await h.db.exec(`INSERT INTO fall_cloud_keys(device_id,key_version,updated_by,robot_model) VALUES ('robot-a',0,'robot','gemma4:27b')`);
    services.reply = answer(200, { message: { content: "1" } });
    const fall = await put("fall", "ollama-key-5678");
    assert.equal(fall.status, 200);
    assert.equal(fall.body.last4, "5678");
    assert.deepEqual([services.calls.at(-1).url, services.calls.at(-1).body.model], ["https://ollama.com/api/chat", "gemma4:27b"]);
    assert.equal(services.calls.at(-1).body.options.num_predict, 1);
  });
});

test("saving is limited to ten tries a minute, since each one asks the service", async () => {
  await withKeys(async ({ services, ask }) => {
    services.reply = () => Response.json({}, { status: 401 });
    for (let attempt = 0; attempt < 10; attempt += 1) {
      assert.equal((await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY })).status, 422);
    }
    assert.equal((await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY })).status, 429);
    assert.equal(services.calls.length, 10);
  });
});

test("the robot gets each key once, keeps its own while none is set, and learns of deletions", async () => {
  await withKeys(async ({ h, services, ask, robot, sync }) => {
    assert.deepEqual(await sync(), {
      openai: { keyVersion: 0, changed: false, apiKey: null }, kma: { keyVersion: 0, changed: false, apiKey: null },
    });
    await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY });
    assert.deepEqual((await sync()).openai, { keyVersion: 1, changed: true, apiKey: OPENAI_KEY });
    assert.deepEqual((await sync({ openai: 1, kma: 0 })).openai, { keyVersion: 1, changed: false, apiKey: null });
    assert.equal((await (await ask("u-owner", "GET")).json()).openai.robotHasCurrent, true);

    // Deleting: the robot removes its copy and has no key (it does not go back to the team key).
    await ask("u-owner", "DELETE", "openai");
    assert.deepEqual((await sync({ openai: 1, kma: 0 })).openai, { keyVersion: 2, changed: true, apiKey: null });

    // The robot reports its OpenAI model and how each key is doing.
    await sync({ openai: 2, kma: 0 }, { models: { openai: "gpt-5.6-mini" },
      health: { openai: { state: "missing", code: null }, fall: { state: "quota", code: "cloud_quota_exhausted" } } });
    const health = (await h.db.query("SELECT service, state, code FROM device_key_health ORDER BY service")).rows;
    assert.deepEqual(health, [{ service: "fall", state: "quota", code: "cloud_quota_exhausted" },
      { service: "openai", state: "missing", code: null }]);
    await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY });
    assert.equal(services.calls.at(-1).body.model, "gpt-5.6-mini", "a new key is checked against the model it will serve");

    const base = { known: { openai: 0, kma: 0 }, models: { openai: null }, health: {} };
    assert.equal((await robot({ ...base, extra: 1 })).status, 400);
    assert.equal((await robot({ ...base, known: { openai: -1, kma: 0 } })).status, 400);
    assert.equal((await robot({ ...base, models: { openai: "bad model" } })).status, 400);
    assert.equal((await robot({ ...base, health: { openai: { state: "broken", code: null } } })).status, 400);
    assert.equal((await robot(base, { deviceId: "robot-b" })).status, 403);
    assert.equal((await robot(base, { token: "nope" })).status, 401);
  });
});

test("re-registering with 지우기 removes the previous household's keys; 남기기 keeps them", async () => {
  await withKeys(async ({ h, load, ask }) => {
    await ask("u-owner", "PUT", "openai", { apiKey: OPENAI_KEY });
    await ask("u-owner", "PUT", "kma", { apiKey: "abcdefgh1234" });
    await h.db.exec(`INSERT INTO device_key_health(device_id,service,state,code,reported_at) VALUES ('robot-a','openai','ok',NULL,now())`);
    await h.db.exec("INSERT INTO users(id) VALUES ('u-next'), ('u-later')");
    const registration = load("db/registration.ts");
    const register = async (userId, history) => {
      const { code } = await registration.createRegistrationCode({ deviceId: "robot-a", sessionSecret: "A".repeat(43) });
      return registration.redeemRegistrationCode({ code: code.replace("-", ""), userId, history, sessionSecret: "A".repeat(43) });
    };
    assert.equal((await register("u-next", "keep")).status, "registered");
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM device_service_keys WHERE ciphertext IS NOT NULL")).rows[0].n, 2);

    assert.equal((await register("u-later", "delete")).status, "registered");
    const keys = (await h.db.query("SELECT service, key_version, ciphertext FROM device_service_keys ORDER BY service")).rows;
    assert.deepEqual(keys, [{ service: "kma", key_version: 2, ciphertext: null }, { service: "openai", key_version: 2, ciphertext: null }]);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM device_key_health")).rows[0].n, 0);
  });
});

test("설정 › AI·서비스 키 follows the mockup; 홈캠 설정 keeps only the consent", async () => {
  const [screen, dashboard, homecam] = await Promise.all([
    readFile(new URL("../app/components/service-keys-settings.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/fall-homecam-settings.tsx", import.meta.url), "utf8"),
  ]);
  for (const text of ["AI·서비스 키", "소유자 화면 · 보호자에게는 보이지 않아요", "대화 · OpenAI", "날씨 · 기상청",
    "낙상 AI 확인 · Ollama", "팀 키 사용 중", "확인하고 저장", "확인하는 중…", "말벗에 반영됨", "말벗에 1분 안에 반영돼요",
    "새로 받은 키는 쓸 수 있게 되기까지 시간이 걸릴 수 있어요", "AI 재확인이 빠져 정확도가 떨어질 수 있어요", "팀 키로 돌아가지 않아요"]) {
    assert.ok(screen.includes(text), text);
  }
  assert.match(dashboard, /\{isOwner && \(\s*<button type="button" onClick=\{\(\) => setSettingsView\("keys"\)\}>/);
  assert.match(homecam, /클라우드 AI 확인 동의/);
  assert.doesNotMatch(homecam, /fall-cloud-key/);
});
