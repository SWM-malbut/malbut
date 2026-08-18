import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadHelper() {
  const source = await readFile(
    new URL("../app/lib/storage-viewer-transport.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Date.now()}`
  );
}

test("uses direct storage WebRTC only on supported desktop Chrome", async () => {
  const { storageViewerTransport } = await loadHelper();
  assert.equal(
    storageViewerTransport(
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
    ),
    "webrtc",
  );
  assert.equal(
    storageViewerTransport(
      "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    ),
    "hls",
  );
  assert.equal(
    storageViewerTransport(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
    ),
    "hls",
  );
  assert.equal(
    storageViewerTransport(
      "Mozilla/5.0 AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    ),
    "hls",
  );
});
