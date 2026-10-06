import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../app/components/managed-robot-controls.tsx", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText.replace(/from "(react(?:\/jsx-runtime)?)"/g, (_, name) => `from "${import.meta.resolve(name)}"`);
const { ManagedRobotControls } = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

function render(runtime = {}, servers = {}) {
  return renderToStaticMarkup(createElement(ManagedRobotControls, {
    snapshot: {
      online: true,
      state: {
        nav2: { robot_interface: "malbut_manager_v1" },
        target: {
          runtime: {
            state: "RUNNING", mode: "navigation", ready: false,
            message: "연결 확인 중", localization: { mode: "LOCALIZATION" }, ...runtime,
          },
          servers: { manager: true, autoslam: true, ...servers },
        },
      },
    },
    isOwner: true, busy: false, sendCommand: async () => true, goal: null,
  }));
}

function button(html, label) {
  const tag = html.match(new RegExp(`<button([^>]*)>${label}</button>`));
  assert.ok(tag, `Missing button: ${label}`);
  return tag[1];
}

test("aggregate connection readiness is displayed without gating other features", () => {
  const html = render({ waiting: ["init:malbut_stt"] });
  assert.match(html, /연결 확인 중/);
  assert.match(html, /init:malbut_stt/);
  assert.doesNotMatch(button(html, "자동 지도 만들기"), /disabled/);
  assert.doesNotMatch(button(html, "앞에 보이는 사람 따라가기"), /disabled/);
});

test("AutoSLAM may start with AMCL but still requires its own server", () => {
  assert.doesNotMatch(button(render(), "자동 지도 만들기"), /disabled/);
  assert.match(button(render({}, { autoslam: false }), "자동 지도 만들기"), /disabled/);
  assert.match(button(render({ localization: { mode: "SWITCHING" } }), "자동 지도 만들기"), /disabled/);
});

test("Bringup start is distinct from requesting SLAM", () => {
  assert.match(button(render(), "Bringup 시작"), /disabled/);
  assert.doesNotMatch(button(render({ state: "STOPPED" }), "Bringup 시작"), /disabled/);
});
