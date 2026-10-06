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
  assert.match(page, /if \(!localUiDemo\) \{\s*const user = await requireChatGPTUser\(returnTo\)/);
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
  // The general event detector was removed; fall status is shown elsewhere.
  assert.doesNotMatch(dashboard, /detectorReady|<span>이벤트 감지<\/span>|"움직임만"/);
  // General person/pet/motion events were replaced by fall incidents (사건).
  assert.doesNotMatch(dashboard, /AI가 사람을 인식한 이벤트|일반 화면 변화/);
  assert.match(dashboard, /url\.searchParams\.set\("incident", focusedIncidentId\)/);
  assert.match(dashboard, /requestedView === "live"/);
  assert.match(dashboard, /url\.searchParams\.set\("view", tab\)/);
  assert.match(dashboard, /url\.searchParams\.set\("mapMode", mapEntryMode\)/);
  // 새 디자인의 홈캠 "현재 상태" 줄: 저장 상태를 색으로 구분한다.
  assert.match(styles, /\.ui-rows strong\.is-good \{[^}]*color: var\(--ui-ok\)/);
  assert.match(styles, /\.ui-rows strong\.is-pending \{[^}]*color: var\(--ui-warn-text\)/);
  assert.match(styles, /\.ui-rows strong\.is-error \{[^}]*color: var\(--ui-danger\)/);
  assert.match(styles, /\.homecam-live-view\.is-pip \{[\s\S]*position: fixed;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \{[\s\S]*touch-action: none;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \.homecam-quick-grid \{[\s\S]*display: none !important;/);
  assert.match(styles, /\.homecam-live-view\.is-pip \.homecam-stream-shell\.is-embedded \.homecam-stream-video-frame \{[\s\S]*aspect-ratio: 16 \/ 9;/);
  assert.match(
    styles,
    /\.homecam-stream-placeholder \{[\s\S]*display: flex;[\s\S]*align-items: center;[\s\S]*flex-direction: column;/,
  );
  // 현재 상태: 말벗 카메라·마이크(소유자 스위치)와 이 기기의 스피커. 영상 아래 따로 버튼 줄은 없다.
  assert.match(dashboard, /label="카메라"/);
  assert.match(dashboard, /updateSetting\("cameraEnabled", value\)/);
  assert.match(dashboard, /label="마이크"/);
  assert.match(dashboard, /updateSetting\("microphoneEnabled", value\)/);
  assert.match(dashboard, /label="스피커"[\s\S]*onChange=\{\(\) => liveSpeaker\?\.toggle\(\)\}/);
  assert.doesNotMatch(dashboard, /보호자 마이크|label="카메라 전원"/);
  // 연속 녹화는 설정 › 홈캠 설정에서만 바꾼다.
  assert.doesNotMatch(dashboard, /label="연속 녹화"/);
  assert.doesNotMatch(dashboard, /카메라 끄기/);
  assert.doesNotMatch(dashboard, /카메라 켜기/);
});
