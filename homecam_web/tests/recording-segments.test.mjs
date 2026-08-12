import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadHelpers() {
  const source = await readFile(
    new URL("../app/recording-segments.ts", import.meta.url),
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

test("active recording can be played up to the current time", async () => {
  const { resolveRecordingSegmentWindow } = await loadHelpers();
  const window = resolveRecordingSegmentWindow({
    recordingStartedAt: "2026-07-26T10:00:00.000Z",
    recordingEndedAt: null,
    segment: 0,
    nowMs: Date.parse("2026-07-26T10:10:00.000Z"),
  });
  assert.deepEqual(window, {
    startAt: "2026-07-26T10:00:00.000Z",
    endAt: "2026-07-26T10:10:00.000Z",
    durationSeconds: 600,
    trimmedStartSeconds: 0,
  });
});

test("event after one hour maps to the correct segment and seek offset", async () => {
  const { recordingPlaybackPosition, resolveRecordingSegmentWindow } =
    await loadHelpers();
  assert.deepEqual(recordingPlaybackPosition(3_900_500), {
    recordingSegment: 1,
    playbackOffsetSeconds: 300.5,
  });
  const window = resolveRecordingSegmentWindow({
    recordingStartedAt: "2026-07-26T10:00:00.000Z",
    recordingEndedAt: null,
    segment: 1,
    nowMs: Date.parse("2026-07-26T11:20:00.000Z"),
  });
  assert.equal(window.startAt, "2026-07-26T11:00:00.000Z");
  assert.equal(window.endAt, "2026-07-26T11:20:00.000Z");
  assert.equal(window.trimmedStartSeconds, 0);
});

test("a partially retained first segment exposes its playback trim", async () => {
  const { resolveRecordingSegmentWindow } = await loadHelpers();
  const window = resolveRecordingSegmentWindow({
    recordingStartedAt: "2026-07-19T12:00:00.000Z",
    recordingEndedAt: null,
    segment: 0,
    nowMs: Date.parse("2026-07-26T12:30:00.000Z"),
  });
  assert.equal(window.startAt, "2026-07-19T12:30:00.000Z");
  assert.equal(window.endAt, "2026-07-19T13:00:00.000Z");
  assert.equal(window.trimmedStartSeconds, 1_800);
});
