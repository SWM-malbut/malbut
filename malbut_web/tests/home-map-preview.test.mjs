// 홈 › 지도 카드: rooms, zones and the robot marker sit on the drawn map, not the whole card.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the home map card keeps the overlay in the map's own proportions", async () => {
  const [dashboard, styles] = await Promise.all([
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);
  // The image and the overlay share one box sized from the map geometry.
  assert.match(dashboard, /<span className="homecam-home-map-frame" style=\{mapFrameStyle\(robotSnapshot\)\}>\s*<Image[\s\S]*?<RobotMapSummaryOverlay snapshot=\{robotSnapshot\} semantics=\{semantics\} \/>\s*<\/span>/);
  assert.match(dashboard, /geometry\.width \/ geometry\.height/);
  // That box is the largest one with those proportions inside the card (contain).
  assert.match(styles, /\.homecam-home-map-preview \{ container-type: size; \}/);
  assert.match(styles, /\.homecam-home-map-frame\[style\*="--map-ratio"\] \{\s*width: min\(100cqw, 100cqh \* var\(--map-ratio\)\);\s*height: auto;\s*aspect-ratio: var\(--map-ratio\);/);
});

test("the map-making card says fall detection rests while mapping", async () => {
  const manager = await readFile(new URL("../app/components/real-robot-map-manager.tsx", import.meta.url), "utf8");
  assert.match(manager, /지도를 만드는 동안 낙상 감지는 잠시 꺼져요\. 다 만들거나 멈추면 다시 켜져요\./);
});
