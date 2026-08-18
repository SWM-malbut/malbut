import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("live dashboard uses rendered media state and exposes one camera control", async () => {
  const [app, dashboard] = await Promise.all([
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-dashboard.tsx", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(app, /onMediaReadyChange\?\.\(state === "live"\)/);
  assert.match(app, /onMediaReadyChange=\{setInlineViewerReady\}/);
  assert.match(app, /"playing",[\s\S]*"timeupdate",[\s\S]*"resize"/);
  assert.match(dashboard, /const displayedMediaReady = liveViewer/);
  assert.match(dashboard, /label="카메라 전원"/);
  assert.match(dashboard, /updateSetting\("cameraEnabled", value\)/);
  assert.doesNotMatch(dashboard, /카메라 끄기/);
  assert.doesNotMatch(dashboard, /카메라 켜기/);
});
