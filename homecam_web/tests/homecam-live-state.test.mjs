import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("live dashboard uses rendered media state and exposes one camera control", async () => {
  const [app, dashboard, page, styles] = await Promise.all([
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-dashboard.tsx", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(app, /onMediaReadyChange\?\.\(state === "live"\)/);
  assert.match(app, /onMediaReadyChange=\{setInlineViewerReady\}/);
  assert.match(app, /NEXT_PUBLIC_HOMECAM_UI_DEMO/);
  assert.match(app, /canvas\.captureStream\(15\)/);
  assert.match(app, /LOCAL DEMO · LIVE/);
  assert.match(page, /process\.env\.NODE_ENV !== "production"/);
  assert.match(page, /process\.env\.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1"/);
  assert.match(page, /if \(!localUiDemo\) await requireChatGPTUser\(returnTo\)/);
  assert.match(app, /onReleaseLive=\{closeInlineViewer\}/);
  assert.match(app, /device\?\.id === inlineViewerDevice\.id/);
  assert.match(app, /"playing",[\s\S]*"timeupdate",[\s\S]*"resize"/);
  assert.match(dashboard, /const displayedMediaReady = liveViewer/);
  assert.match(dashboard, /const liveViewerActive = Boolean\(liveViewer\)/);
  assert.match(dashboard, /LOCAL_DEMO_DEVICE_ID = "local-demo-homecam"/);
  assert.match(dashboard, /setDevices\(\[LOCAL_DEMO_DEVICE\]\)/);
  assert.match(dashboard, /const livePipActive = tab !== "live" && liveViewerActive/);
  assert.match(dashboard, /className=\{`homecam-live-view \$\{livePipActive \? "is-pip" : ""\}`\}/);
  assert.match(dashboard, /aria-label="미니 영상 닫기\. 카메라와 영상 저장은 계속 유지됩니다\."/);
  assert.match(dashboard, /onPointerDown=\{livePipActive \? beginLivePipDrag : undefined\}/);
  assert.doesNotMatch(dashboard, /homecam-live-pip-title/);
  assert.doesNotMatch(dashboard, /AUTHORIZED_P2P_VIEWER_REUSE_GRACE_MS/);
  assert.match(
    dashboard,
    /\(tab === "live" \|\| liveViewerActive\)[\s\S]*livePipActive/,
  );
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
  assert.match(styles, /\.homecam-live-view\.is-pip \{[\s\S]*position: fixed;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \{[\s\S]*touch-action: none;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \.homecam-quick-grid \{[\s\S]*display: none !important;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \.homecam-stream-shell\.is-embedded \.homecam-stream-video-frame \{[\s\S]*aspect-ratio: 16 \/ 9;/);
  assert.match(
    styles,
    /\.homecam-stream-placeholder \{[\s\S]*display: flex;[\s\S]*align-items: center;[\s\S]*flex-direction: column;/,
  );
  assert.match(dashboard, /label="카메라 전원"/);
  assert.match(dashboard, /updateSetting\("cameraEnabled", value\)/);
  assert.doesNotMatch(dashboard, /카메라 끄기/);
  assert.doesNotMatch(dashboard, /카메라 켜기/);
});
