import {
  claimFallAiJob, deferFallAiJob, finishFallAiJob, readFallAiContext, readFallAiQuestionContext,
  type FallAiOutcome,
} from "../db/fall-ai-review";
import {
  buildFollowupPayload, buildReviewPayload, parseFollowupReply, parseReviewReply, postOllamaChat,
  REVIEW_FRAME_COUNT, REVIEW_WINDOW_MS, type ReviewFrame,
} from "./fall-ai-prompt";
import { requestBrokerImages } from "./kvs-broker";
import { resolveDeviceKvsResources, type DeviceKvsEnvironment } from "./kvs-device-config";
import { getRuntimeEnvironment } from "./runtime-env";

type WorkerEnv = DeviceKvsEnvironment & { FALL_KEY_ENCRYPTION_SECRET?: string };

// Sample spacing for 12 frames from the moment to +5 s (last at +4.994 s).
export const SAMPLE_INTERVAL_MS = Math.floor(REVIEW_WINDOW_MS / (REVIEW_FRAME_COUNT - 1));
// The archive needs a moment to hold the last fragment.
const ARCHIVE_SETTLE_MS = 15_000;
// Missing stills for a recent moment may still be uploading: retry, do not fail yet.
const LATE_ARCHIVE_MS = 120_000;
const MIN_FRAMES = 6;
// One job per web process at a time: a review holds broker and Cloud calls for up to ~30 s.
let running = 0;
const MAX_RUNNING = 1;

/** Start a job after the response; the result is read back from the incident detail. */
export function startFallAiJob(input: { deviceId?: string; jobId?: string } = {}) {
  if (running >= MAX_RUNNING) return false;
  running += 1;
  void processFallAiJob(input).catch(() => undefined).finally(() => { running -= 1; });
  return true;
}

/** Width and height from a baseline/progressive JPEG SOF marker; null if not a JPEG. */
export function jpegSize(bytes: Uint8Array): { width: number; height: number } | null {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
  let i = 2;
  while (i + 1 < bytes.length) {
    if (bytes[i] !== 0xff) return null;
    while (i + 1 < bytes.length && bytes[i + 1] === 0xff) i += 1; // fill bytes
    const marker = bytes[i + 1];
    // Standalone markers carry no length.
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd8)) { i += 2; continue; }
    if (marker === 0xd9 || i + 3 >= bytes.length) return null;
    const length = (bytes[i + 2] << 8) | bytes[i + 3];
    if (length < 2) return null;
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
      if (i + 8 >= bytes.length) return null;
      const height = (bytes[i + 5] << 8) | bytes[i + 6], width = (bytes[i + 7] << 8) | bytes[i + 8];
      return width && height ? { width, height } : null;
    }
    i += 2 + length;
  }
  return null;
}

async function frames(deviceId: string, momentAt: string, now: number): Promise<ReviewFrame[] | string> {
  const runtime = getRuntimeEnvironment() as WorkerEnv;
  let streamArn: string | null | undefined;
  try { streamArn = resolveDeviceKvsResources(runtime, deviceId)?.streamArn; } catch { return "kvs_unconfigured"; }
  if (!streamArn) return "kvs_unconfigured";
  const start = Date.parse(momentAt);
  let images;
  try {
    images = await requestBrokerImages({ deviceId, streamArn, startAt: momentAt,
      endAt: new Date(start + SAMPLE_INTERVAL_MS * (REVIEW_FRAME_COUNT - 1)).toISOString(),
      count: REVIEW_FRAME_COUNT });
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return now - start < LATE_ARCHIVE_MS ? "frames_unavailable_yet" : "frames_unavailable";
    }
    return "kvs_unavailable";
  }
  const result: ReviewFrame[] = [];
  for (const image of images) {
    if (!image.at || !image.jpegBase64) continue;
    const size = jpegSize(Buffer.from(image.jpegBase64, "base64"));
    const offsetMs = Date.parse(image.at) - start;
    if (!size || offsetMs < 0 || offsetMs > REVIEW_WINDOW_MS) continue;
    result.push({ jpegBase64: image.jpegBase64, offsetMs, ...size });
  }
  result.sort((a, b) => a.offsetMs - b.offsetMs);
  if (result.length >= MIN_FRAMES) return result;
  return now - start < LATE_ARCHIVE_MS ? "frames_unavailable_yet" : "frames_unavailable";
}

/**
 * Process one queued AI review or follow-up question. Photo-only verdicts use
 * the robot's prompt and model; nothing here changes the incident.
 */
export async function processFallAiJob(input: { deviceId?: string; jobId?: string } = {}, now = Date.now()) {
  const job = await claimFallAiJob(input.deviceId, input.jobId, now);
  if (!job) return { processed: false, reason: "not_due_or_robot_busy" };
  if (Date.parse(job.momentAt) + REVIEW_WINDOW_MS + ARCHIVE_SETTLE_MS > now) {
    await deferFallAiJob(job, 20);
    return { processed: false, reason: "recording_not_ready" };
  }
  let outcome: FallAiOutcome;
  try {
    const secret = (getRuntimeEnvironment() as WorkerEnv).FALL_KEY_ENCRYPTION_SECRET ?? "";
    const context = await readFallAiContext(job.deviceId, secret);
    if (!context.consent) outcome = { ok: false, errorCode: "cloud_consent_off" };
    else if (!context.apiKey) outcome = { ok: false, errorCode: "key_missing" };
    else if (!context.model) outcome = { ok: false, errorCode: "model_unknown" };
    else {
      const picked = await frames(job.deviceId, job.momentAt, now);
      if (typeof picked === "string") outcome = { ok: false, errorCode: picked };
      else {
        const historyIncomplete = picked.length < REVIEW_FRAME_COUNT;
        if (job.kind === "review") {
          const reply = parseReviewReply(await postOllamaChat(context.apiKey,
            buildReviewPayload(context.model, picked, REVIEW_WINDOW_MS, historyIncomplete)));
          outcome = { ok: true, ...reply, model: context.model, frameCount: picked.length, historyIncomplete };
        } else {
          // The switch off sends the question and photos only.
          const followup = job.includeContext ? await readFallAiQuestionContext(job.deviceId, job.incidentId)
            : { memos: [], verdicts: [] };
          const answer = parseFollowupReply(await postOllamaChat(context.apiKey,
            buildFollowupPayload(context.model, picked, REVIEW_WINDOW_MS, { ...followup, question: job.question })));
          outcome = { ok: true, answer, model: context.model, frameCount: picked.length, historyIncomplete };
        }
      }
    }
  } catch (error) {
    const code = error instanceof Error && /^(cloud_[a-z_]+|FALL_KEY_[A-Z_]+)$/.test(error.message)
      ? error.message.startsWith("FALL_KEY_") ? "key_unreadable" : error.message : "review_failed";
    outcome = { ok: false, errorCode: code };
  }
  const saved = await finishFallAiJob(job, outcome);
  return { processed: true, ok: outcome.ok && saved, kind: job.kind,
    reason: outcome.ok ? "completed" : outcome.errorCode };
}
