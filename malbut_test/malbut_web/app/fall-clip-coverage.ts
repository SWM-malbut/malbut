import { clipPlaybackState, type ClipCheck, type ClipPlaybackState } from "../db/fall-review";
import { requestBrokerEventCoverage } from "./kvs-broker";
import { resolveDeviceKvsResources, type DeviceKvsEnvironment } from "./kvs-device-config";
import { getRuntimeEnvironment } from "./runtime-env";

// 2026-10-09: a clip recorded while 연속 녹화 was off showed "영상 재생 가능" because
// the recording-session records said so; KVS held nothing. A settled clip that the
// records call playable is checked against the fragments KVS really holds.

// Fragments are listed a little after ingestion; only then is a result final.
const FINAL_AFTER_MS = 2 * 60_000;
const CACHE_LIMIT = 2000;
const final = new Map<string, ClipPlaybackState>();

export function clipCoverageCheck(deviceId: string): ClipCheck | undefined {
  let streamArn: string | null | undefined;
  try {
    streamArn = resolveDeviceKvsResources(getRuntimeEnvironment() as DeviceKvsEnvironment,
      deviceId)?.streamArn;
  } catch {
    return undefined;
  }
  if (!streamArn) return undefined;
  const stream = streamArn;
  return async (startAt, endAt, recorded) => {
    if (recorded !== "available" && recorded !== "partial") return recorded;
    const key = `${deviceId}|${stream}|${startAt}|${endAt}`;
    const known = final.get(key);
    if (known) return known;
    let ranges;
    try {
      ranges = await requestBrokerEventCoverage({ deviceId, streamArn: stream, startAt, endAt });
    } catch {
      return recorded; // Cannot ask KVS now: keep what the records say.
    }
    const state = clipPlaybackState(startAt, endAt, ranges.map((range) => ({
      start: Date.parse(range.startAt), end: Date.parse(range.endAt), streamArn: stream,
    })));
    if (Date.now() > Date.parse(endAt) + FINAL_AFTER_MS) {
      final.set(key, state);
      if (final.size > CACHE_LIMIT) final.delete(final.keys().next().value!);
    }
    return state;
  };
}
