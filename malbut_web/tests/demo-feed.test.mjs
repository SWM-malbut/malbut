import assert from "node:assert/strict";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

test("owner lease keeps chat on refresh, rejects stale uploads and clears chat on exit", async () => {
  const h = await fallDatabase(),
    load = moduleLoader();
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      const { demoLease, viewerDemo, exchangeDemo } = load("db/demo-feed.ts");
      const open = (user = "u-owner") =>
        viewerDemo("robot-a", user, null, false);
      assert.equal(await demoLease("robot-a"), null);
      await assert.rejects(open("u-family"), /FORBIDDEN/);
      const initial = await open(),
        id = initial.session;
      const report = {
        session: id,
        count: 1,
        suspect: true,
        lines: [
          {
            id: "robot:one",
            speaker: "robot",
            text: "괜찮으세요?",
            confirmation: true,
            at: Date.now(),
          },
        ],
      };
      assert.equal((await demoLease("robot-a")).session, id);
      assert.equal(await exchangeDemo("robot-b", report), false);
      assert.equal(await exchangeDemo("robot-a", report), true);
      assert.equal(await exchangeDemo("robot-a", report), true);
      const fresh = await open();
      assert.equal(fresh.session, id);
      assert.equal(fresh.lines.length, 1);
      assert.equal(fresh.count, 1);
      assert.equal(fresh.badge, "음성 확인 중");
      await h.db.exec(
        "UPDATE demo_sessions SET expires_at=now()-interval '1 second'",
      );
      assert.equal(await demoLease("robot-a"), null);
      assert.equal(await exchangeDemo("robot-a", report), false);
      const resumed = await open();
      assert.notEqual(resumed.session, id);
      assert.equal(resumed.lines.length, 1);
      assert.equal(resumed.count, null);
      await assert.rejects(viewerDemo("robot-a", "u-owner", id, true), /ENDED/);
      await viewerDemo("robot-a", "u-owner", resumed.session, true);
      assert.equal(
        await exchangeDemo("robot-a", { ...report, session: resumed.session }),
        false,
      );
      assert.equal((await open()).lines.length, 0);
    });
  } finally {
    await h.db.close();
  }
});

test("a changed owner cannot read the previous owner's lines, and expired lines are purged", async () => {
  const h = await fallDatabase(),
    load = moduleLoader();
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      const { viewerDemo, exchangeDemo, demoLease, purgeExpiredDemoLines } = load("db/demo-feed.ts");
      const { session } = await viewerDemo("robot-a", "u-owner", null, false);
      const line = {
        id: "a",
        speaker: "user",
        text: "안녕",
        confirmation: false,
        at: Date.now(),
      };
      await exchangeDemo("robot-a", {
        session,
        count: null,
        suspect: false,
        lines: [line, { ...line, id: "old", at: Date.now() - 3600001 }],
      });
      await h.db.exec(
        "UPDATE demo_lines SET spoken_at=now()-interval '61 minutes'",
      );
      assert.equal(await purgeExpiredDemoLines(), 1);
      assert.equal(
        (await h.db.query("SELECT * FROM demo_lines")).rows.length,
        0,
      );
      await exchangeDemo("robot-a", {
        session,
        count: null,
        suspect: false,
        lines: [line],
      });
      await h.db.exec(
        "UPDATE device_memberships SET role='family' WHERE user_id='u-owner'; UPDATE device_memberships SET role='owner' WHERE user_id='u-family'",
      );
      assert.equal(await demoLease("robot-a"), null);
      assert.equal(
        (await viewerDemo("robot-a", "u-family", null, false)).lines.length,
        0,
      );
    });
  } finally {
    await h.db.close();
  }
});

test("badges follow confirmation playback, outcomes and expiry", () => {
  const { fallBadge, parseReport } = moduleLoader()(
    "app/demo-feed-contract.ts",
  );
  const now = 1000000,
    incident = {
      opened: now - 15000,
      updated: now,
      state: "verifying",
      answer: null,
    };
  assert.equal(fallBadge(incident, [], false, now), "낙상 의심");
  assert.equal(
    fallBadge(
      incident,
      [{ speaker: "robot", confirmation: true, at: now }],
      false,
      now,
    ),
    "음성 확인 중",
  );
  assert.equal(
    fallBadge({ ...incident, answer: "okay" }, [], false, now + 5000),
    "도움 요청 없음",
  );
  assert.equal(
    fallBadge({ ...incident, answer: "okay" }, [], false, now + 11000),
    null,
  );
  assert.equal(
    fallBadge({ ...incident, answer: "help_request" }, [], false, now + 11000),
    "도움 요청",
  );
  assert.equal(
    fallBadge({ ...incident, answer: "no_response" }, [], false, now + 11000),
    "응답 없음",
  );
  assert.equal(fallBadge(incident, [], false, now + 600001), null);
  assert.equal(
    parseReport({ lines: [], count: 1, suspect: false, session: "bad" }),
    null,
  );
});
