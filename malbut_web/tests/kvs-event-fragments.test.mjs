import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { coveredRanges, eventStartFragment } from "../infra/aws/kvs-broker/event-fragments.mjs";

const T0 = Date.parse("2026-10-09T07:18:10Z");
const frag = (offsetS, lengthS = 2) => ({
  ServerTimestamp: new Date(T0 + offsetS * 1000), FragmentLengthInMilliseconds: lengthS * 1000,
});
const iso = (offsetS) => new Date(T0 + offsetS * 1000).toISOString();

test("a clip with fragments only before it has no start, so it plays nothing", () => {
  // 16:18: recording stopped before the clip; the broker used to start at the last one.
  assert.equal(eventStartFragment([frag(-15), frag(-13)], T0, T0 + 30_000), null);
  assert.equal(eventStartFragment([], T0, T0 + 30_000), null);
});

test("playback starts at the fragment holding the clip start, else the first inside", () => {
  assert.equal(eventStartFragment([frag(-3), frag(-1), frag(1)], T0, T0 + 30_000).ServerTimestamp
    .toISOString(), iso(-1));
  assert.equal(eventStartFragment([frag(-15), frag(12), frag(14)], T0, T0 + 30_000).ServerTimestamp
    .toISOString(), iso(12));
  assert.equal(eventStartFragment([frag(31)], T0, T0 + 30_000), null);
});

test("coverage merges touching fragments, keeps gaps and stays inside the clip", () => {
  const fragments = [frag(-1), frag(1), frag(3, 1.5), frag(10), frag(28, 4)].reverse();
  assert.deepEqual(coveredRanges(fragments, T0, T0 + 30_000), [
    { startAt: iso(0), endAt: iso(4.5) },
    { startAt: iso(10), endAt: iso(12) },
    { startAt: iso(28), endAt: iso(30) },
  ]);
  assert.deepEqual(coveredRanges([frag(-15)], T0, T0 + 30_000), []);
});

test("the broker serves coverage and no longer falls back to an earlier fragment", async () => {
  const broker = await readFile(new URL("../infra/aws/kvs-broker/index.mjs", import.meta.url), "utf8");
  assert.match(broker, /"EVENT_COVERAGE"/);
  assert.match(broker, /eventStartFragment\(/);
  assert.doesNotMatch(broker, /fragments\.at\(-1\)/);
});
