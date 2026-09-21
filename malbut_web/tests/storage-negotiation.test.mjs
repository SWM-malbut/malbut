import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadStorageNegotiation() {
  const source = await readFile(
    new URL("../app/lib/storage-negotiation.ts", import.meta.url),
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

const validViewerSdp = [
  "v=0",
  "m=video 9 UDP/TLS/RTP/SAVPF 102 121",
  "a=recvonly",
  "a=rtpmap:102 H264/90000",
  "a=rtpmap:121 rtx/90000",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111",
  "a=recvonly",
  "a=rtpmap:111 opus/48000/2",
  "",
].join("\r\n");

test("storage participant directions match the AWS media contract", async () => {
  const negotiation = await loadStorageNegotiation();

  assert.equal(negotiation.storageTransceiverDirection("MASTER", "video", true), "sendonly");
  assert.equal(negotiation.storageTransceiverDirection("MASTER", "audio", true), "sendrecv");
  assert.equal(negotiation.storageTransceiverDirection("VIEWER", "video", false), "recvonly");
  assert.equal(negotiation.storageTransceiverDirection("VIEWER", "audio", false), "recvonly");
  assert.equal(negotiation.storageTransceiverDirection("VIEWER", "audio", true), "sendrecv");
});

test("storage viewer answer accepts H264/Opus with recvonly tracks", async () => {
  const negotiation = await loadStorageNegotiation();
  assert.doesNotThrow(() =>
    negotiation.assertStorageAnswerSdp(validViewerSdp, "VIEWER", false),
  );
});

test("storage viewer answer rejects VP8 and inactive video", async () => {
  const negotiation = await loadStorageNegotiation();
  const vp8 = validViewerSdp
    .replace("102 121", "96")
    .replace("a=rtpmap:102 H264/90000\r\na=rtpmap:121 rtx/90000", "a=rtpmap:96 VP8/90000");
  const inactive = validViewerSdp.replace("a=recvonly", "a=inactive");

  assert.throws(
    () => negotiation.assertStorageAnswerSdp(vp8, "VIEWER", false),
    /H264/,
  );
  assert.throws(
    () => negotiation.assertStorageAnswerSdp(inactive, "VIEWER", false),
    /recvonly.*inactive/,
  );
});
