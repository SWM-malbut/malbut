// Shared by the trusted web server and push broker. No model-written message
// or arbitrary destination URL is accepted here.
import { FALL_CONTRACT } from "./fall-contract.mjs";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
// Levels and which reasons exist come from the robot contract
// (malbut_interfaces/contracts/fall_events.json); the words are the web's.
const texts = {
  fall_observed_person_okay: "넘어지는 장면이 감지됐습니다. 대상자는 괜찮다고 답했습니다.",
  person_no_response: "상태 확인 질문에 답변이 없습니다. 확인이 필요합니다.",
  check_required_not_confirmed_fall: "낙상 의심 상황을 확인하지 못했습니다. 상태를 확인해 주세요.",
  help_requested: "대상자가 도움을 요청했습니다. 즉시 확인해 주세요.",
  confirmation_help_required: "상황 확인 결과 도움이 필요한 것으로 판단되었습니다. 즉시 확인해 주세요.",
  // The robot could not check by itself (목업 28번): blocked for a minute, speech
  // off or a failed question, then what it saw when it went back before asking.
  robot_could_not_confirm: "말벗이 낙상 의심 상황을 직접 확인하지 못했습니다. 즉시 확인해 주세요.",
  revisit_same_person_off_floor:
    "말벗이 다시 확인했을 때 같은 분으로 보이는 분이 바닥이 아닌 곳에 있었습니다. 괜찮아 보이지만 확인해 주세요.",
  revisit_person_unclear_off_floor:
    "말벗이 다시 확인했을 때 바닥이 아닌 곳에 있는 분이 있었지만 같은 분인지 알 수 없습니다. 상태를 확인해 주세요.",
  revisit_nobody_there: "말벗이 다시 확인했을 때 그 자리에 사람이 없었습니다. 상태를 확인해 주세요.",
  // A Cloud-only scene: someone answered they were fine, but the robot cannot
  // tell them from the person seen down (2026-10-12 16:31, no alert at all).
  scene_answer_unconfirmed: "누군가 괜찮다고 답했지만, 쓰러진 분인지 확인하지 못했습니다. 상태를 확인해 주세요.",
  // "그만/멈춰" during the check stops it like 직접 움직이기, without a question (2026-10-13).
  check_stopped_by_user: "말벗이 확인하러 가던 중 '그만'이라는 말을 듣고 멈췄습니다. 상태를 확인해 주세요.",
  // Web-originated only; robots cannot request it (see app/fall-contract.ts).
  reopened_by_opinion: "처리 완료된 낙상 사건에 다른 의견이 달렸습니다. 다시 확인해 주세요.",
};
const messages = Object.fromEntries(Object.entries(FALL_CONTRACT.notificationReasons)
  .map(([reason, level]) => [reason, [level, texts[reason]]]));
export const FALL_NOTIFICATION_TEXTS = texts;
export const WEB_ONLY_FALL_REASONS = FALL_CONTRACT.webOnlyNotificationReasons;
// Sent once per reason per incident, outside the "only a higher level" rule of
// the other robot reasons: a later 도움 요청 or revisit result must still go out
// after "직접 확인하지 못함".
export const PER_REASON_FALL_REASONS = FALL_CONTRACT.perReasonNotificationReasons;

export function buildFallNotification(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const { deviceId, notificationId, incidentId, level, reason, occurredAt, resend } = input;
  const entry = typeof reason === "string" && Object.hasOwn(messages, reason)
    ? messages[reason] : null;
  if (
    typeof deviceId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(deviceId) ||
    typeof notificationId !== "string" || !uuid.test(notificationId) ||
    typeof incidentId !== "string" || !uuid.test(incidentId) ||
    !entry || typeof entry[1] !== "string" || level !== entry[0] ||
    typeof occurredAt !== "string" || !Number.isFinite(Date.parse(occurredAt)) ||
    new Date(occurredAt).toISOString() !== occurredAt ||
    !(resend === undefined || resend === true || resend === "true")
  ) return null;
  return {
    // [재발신]: same level and message as the first notification.
    body: resend ? `[재발신] ${entry[1]}` : entry[1],
    data: {
      kind: "fall", deviceId, notificationId, incidentId, level, reason, occurredAt,
      // Push data values are strings; round-trips through isFallNotification.
      ...(resend ? { resend: "true" } : {}),
      // Opens the incident screen (사건) for this device.
      url: `/?view=events&device=${encodeURIComponent(deviceId)}&incident=${encodeURIComponent(incidentId)}`,
    },
  };
}

export function isFallNotification(notification) {
  const data = notification?.data;
  if (!data || data.kind !== "fall" || Array.isArray(data)) return false;
  const expected = buildFallNotification(data);
  if (!expected || notification.body !== expected.body) return false;
  const keys = Object.keys(expected.data);
  return Object.keys(data).length === keys.length &&
    keys.every((key) => data[key] === expected.data[key]);
}
