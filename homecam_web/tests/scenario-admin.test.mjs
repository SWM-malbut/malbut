import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("simulation actor controls stay on a separate owner-only admin page", async () => {
  const [page, panel, contract, database, mapPanel] = await Promise.all([
    readFile(new URL("../app/scenario-admin/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/scenario-admin-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/robot-contract.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/robot-map.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(page, /requireChatGPTUser\('\/scenario-admin'\)/);
  assert.match(page, /filter\(\(device\) => device\.role === 'owner'\)/);
  assert.match(panel, /demo_person_show/);
  assert.match(panel, /demo_person_hide/);
  assert.match(panel, /사람 등장/);
  assert.match(panel, /사람 퇴장/);
  assert.doesNotMatch(panel, /start_patrol|start_person_tracking/);
  assert.match(contract, /"demo_person_show" \| "demo_person_hide"/);
  assert.match(database, /userCanManageDevice/);
  assert.match(database, /input\.operation\.startsWith\("demo_person_"\)/);
  assert.match(mapPanel, /\/scenario-admin\?device=/);
});
