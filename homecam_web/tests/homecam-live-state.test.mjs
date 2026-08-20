import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("live dashboard uses rendered media state and exposes one camera control", async () => {
  const [app, dashboard, styles] = await Promise.all([
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-dashboard.tsx", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(app, /onMediaReadyChange\?\.\(state === "live"\)/);
  assert.match(app, /onMediaReadyChange=\{setInlineViewerReady\}/);
  assert.match(app, /"playing",[\s\S]*"timeupdate",[\s\S]*"resize"/);
  assert.match(dashboard, /const displayedMediaReady = liveViewer/);
  assert.match(
    dashboard,
    /const storageReady = Boolean\([\s\S]*storageCanRun[\s\S]*storageHealthy/,
  );
  assert.match(
    dashboard,
    /const storageConnecting = Boolean\([\s\S]*storageSessionActive[\s\S]*storageGraceUntilMs/,
  );
  assert.match(
    dashboard,
    /const storageError = Boolean\([\s\S]*!storageConnecting/,
  );
  assert.match(dashboard, /storageReady[\s\S]*\? "is-good"[\s\S]*storageConnecting[\s\S]*\? "is-pending"[\s\S]*storageError[\s\S]*\? "is-error"/);
  assert.match(dashboard, /"저장 오류"/);
  assert.match(dashboard, /const devicePollIntervalMs = Boolean\(/);
  assert.match(dashboard, /\? 1_000 : 15_000/);
  assert.match(
    dashboard,
    /const detectorReady = Boolean\([\s\S]*monitoringEnabled[\s\S]*detectorHealthy/,
  );
  assert.match(dashboard, /<span>이벤트 감지<\/span>[\s\S]*"움직임만"/);
  assert.match(dashboard, /AI가 사람을 인식한 이벤트/);
  assert.match(
    dashboard,
    /말벗이 정지한 상태에서 확인된 일반 화면 변화/,
  );
  assert.match(dashboard, /requestedView === "live"/);
  assert.match(dashboard, /url\.searchParams\.set\("view", tab\)/);
  assert.match(dashboard, /url\.searchParams\.set\("mapMode", mapEntryMode\)/);
  assert.match(
    styles,
    /theme-light\.tab-live \.homecam-connection-pill\.is-online[\s\S]*color: #1f6641/,
  );
  assert.match(styles, /homecam-live-channel-summary > span\.is-ready/);
  assert.match(styles, /homecam-live-channel-summary > span\.is-error/);
  assert.match(styles, /homecam-live-state-list i\.is-error/);
  assert.match(
    styles,
    /\.homecam-stream-placeholder \{[\s\S]*display: flex;[\s\S]*align-items: center;[\s\S]*flex-direction: column;/,
  );
  assert.match(dashboard, /label="카메라 전원"/);
  assert.match(dashboard, /updateSetting\("cameraEnabled", value\)/);
  assert.doesNotMatch(dashboard, /카메라 끄기/);
  assert.doesNotMatch(dashboard, /카메라 켜기/);
});
