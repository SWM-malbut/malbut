import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);

function screen(file, normalize, raw) {
  const source = readFileSync(new URL(`../app/components/${file}.tsx`, import.meta.url), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  let first = true, state;
  const exports = {};
  new Function("require", "exports", `${js}\nexports.normalize = ${normalize};`)(
    (name) => name === "react" ? {
      useEffect() {}, useCallback: (fn) => fn,
      useState: (initial) => [first ? (first = false, state) : initial, () => {}],
    } : require(name), exports,
  );
  state = exports.normalize(raw);
  return { exports, state };
}

test("payment survives key-card parsing and shows billing action for both team and owner keys", () => {
  for (const keyVersion of [0, 1]) {
    const { exports, state } = screen("service-keys-settings",
      "(raw) => ({ openai: asView(null), kma: asView(null), fall: asView(raw) })",
      { configured: keyVersion > 0, keyVersion, robotHasCurrent: true, problem: "payment" });
    assert.equal(state.fall.problem, "payment");
    const html = renderToStaticMarkup(createElement(exports.ServiceKeysSettings, { deviceId: "robot-a", onBack() {} }));
    assert.match(html, /쓸 수 없음/);
    assert.match(html, /Ollama.*결제.*요금제/);
    assert.doesNotMatch(html, /말벗에 반영됨/);
  }
});

test("payment reaches the home notice for owners and guardians without exposing the code", () => {
  for (const isOwner of [true, false]) {
    const { exports, state } = screen("key-health-notice", "asProblems", [{ service: "fall", problem: "payment" }]);
    assert.equal(state.length, 1);
    const html = renderToStaticMarkup(createElement(exports.KeyHealthNotice, { deviceId: "robot-a", isOwner, onOpenKeys() {} }));
    assert.match(html, /낙상 AI 확인을 할 수 없어요/);
    assert.match(html, /Ollama.*결제.*요금제/);
    assert.equal(html.includes("키 확인하기"), isOwner);
    assert.doesNotMatch(html, /cloud_payment_required/);
  }
});
