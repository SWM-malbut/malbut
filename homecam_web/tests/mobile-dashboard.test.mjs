import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the authenticated mobile shell exposes all five primary tabs", async () => {
  const [header, styles] = await Promise.all([
    readFile(new URL("../app/components/homecam-header.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  for (const tab of ["home", "live", "map", "events", "settings"]) {
    assert.match(header, new RegExp(`onNavigate\\(\\"${tab}\\"\\)`));
  }
  assert.match(header, /homecam-mobile-settings-tab/);
  assert.match(styles, /grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(styles, /\.homecam-mobile-settings-tab\s*\{[\s\S]*display:\s*flex !important/);
  assert.match(styles, /padding:\s*8px 12px calc\(8px \+ env\(safe-area-inset-bottom\)\)/);
  assert.match(styles, /\.homecam-dashboard-shell \.homecam-header\s*\{[\s\S]*position:\s*fixed !important;[\s\S]*inset:\s*auto 0 0 !important/);
});

test("mobile event selection opens a dedicated detail screen with a list return action", async () => {
  const [dashboard, styles] = await Promise.all([
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(dashboard, /mobileEventDetailOpen/);
  assert.match(dashboard, /setMobileEventDetailOpen\(true\)/);
  assert.match(dashboard, /className="homecam-mobile-event-back"/);
  assert.match(dashboard, /이벤트 목록/);
  assert.match(styles, /homecam-events-workspace:not\(\.is-mobile-detail-open\) \.homecam-event-detail/);
  assert.match(styles, /homecam-events-workspace\.is-mobile-detail-open \.homecam-event-list/);
});

test("the mobile event timeline exposes a usable date navigator", async () => {
  const [dashboard, styles] = await Promise.all([
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(dashboard, /aria-label="이벤트 날짜 선택"/);
  assert.match(dashboard, /aria-label="이전 날짜"/);
  assert.match(dashboard, /aria-label="다음 날짜"/);
  assert.match(dashboard, /formatEventDate\(eventDate, todayEventDate\)/);
  assert.match(styles, /tab-events \.homecam-device-bar > \.homecam-event-period\s*\{[\s\S]*display:\s*flex;[\s\S]*width:\s*100%/);
});

test("mobile camera and map retain large task-first work areas", async () => {
  const styles = await readFile(
    new URL("../app/globals.css", import.meta.url),
    "utf8",
  );

  assert.match(styles, /height:\s*clamp\(300px, 50svh, 430px\)/);
  assert.match(styles, /\.homecam-dashboard-shell \.robot-map-card\s*\{[\s\S]*min-height:\s*320px/);
  assert.match(styles, /\.homecam-dashboard-shell \.homecam-settings-nav\s*\{[\s\S]*display:\s*none/);
});
