// Shared by the trusted web server and push broker. No model-written message
// or arbitrary destination URL is accepted here.
import { FALL_CONTRACT } from "./fall-contract.mjs";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
// Levels and which reasons exist come from the robot contract
// (malbut_interfaces/contracts/fall_events.json); the words are the web's.
// 해요체, 첫 줄 = 무슨 일 / 둘째 줄 = 할 일(웹 용어 4번, 목업 37번). 휴대폰이 줄을 나누므로 직접 "\n".
const texts = {
  fall_observed_person_okay: "말벗이 넘어지는 장면을 봤어요.\n넘어진 분은 괜찮다고 답했어요.",
  person_no_response: "말벗이 괜찮은지 물었지만 답이 없어요.\n상태를 확인해 주세요.",
  check_required_not_confirmed_fall: "넘어졌는지 확실하지 않아요.\n상태를 확인해 주세요.",
  help_requested: "말벗에게 도움을 요청했어요.\n지금 바로 확인해 주세요.",
  confirmation_help_required: "말벗이 확인해 보니 도움이 필요해 보여요.\n지금 바로 확인해 주세요.",
  // The robot could not check by itself (목업 28번): blocked for a minute, speech
  // off or a failed question, then what it saw when it went back before asking.
  robot_could_not_confirm: "말벗이 넘어진 것 같은 분을 직접 확인하지 못했어요.\n지금 바로 확인해 주세요.",
  revisit_same_person_off_floor:
    "말벗이 다시 가 보니 처음 본 분으로 보이는 분이 바닥이 아닌 곳에 있었어요.\n괜찮아 보이지만 확인해 주세요.",
  revisit_person_unclear_off_floor:
    "말벗이 다시 가 보니 바닥이 아닌 곳에 누군가 있었어요.\n처음 본 분인지 몰라서, 상태를 확인해 주세요.",
  revisit_nobody_there: "말벗이 다시 가 보니 그 자리에 아무도 없었어요.\n상태를 확인해 주세요.",
  // A Cloud-only scene: someone answered they were fine, but the robot cannot
  // tell them from the person seen down (2026-10-12 16:31, no alert at all).
  scene_answer_unconfirmed: "누군가 괜찮다고 답했지만, 쓰러진 분인지는 모르겠어요.\n상태를 확인해 주세요.",
  // "그만/멈춰" during the check stops it like 수동 조작, without a question (2026-10-13).
  check_stopped_by_user: "말벗이 확인하러 가다가 '그만'이라는 말을 듣고 멈췄어요.\n상태를 확인해 주세요.",
  // The one check for every suspicion (SWM25-261, 목업 29~31번).
  person_may_differ: "말벗이 가 보니 처음 본 분과 다른 분이 있었어요.\n처음 본 분의 상태를 확인해 주세요.",
  left_unseen: "말벗이 가 보니 아무도 없었지만, 떠나는 모습은 못 봤어요.\n상태를 확인해 주세요.",
  could_not_compare: "말벗이 넘어진 것 같은 자리를 볼 수 없었어요. (어둡거나 가려짐)\n상태를 확인해 주세요.",
  person_crawled_away: "말벗이 가 보니 아무도 없었고, 바닥을 기어서 옮긴 것 같아요.\n상태를 확인해 주세요.",
  closed_after_alert: "앞서 긴급 알림을 보낸 일을 말벗이 확인했어요.\n결과는 사건 기록에서 볼 수 있어요.",
  // Web-originated only; robots cannot request it (see app/fall-contract.ts).
  reopened_by_opinion: "처리 완료한 사건에 다른 의견이 달렸어요.\n다시 확인해 주세요.",
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
  const { deviceId, notificationId, incidentId, level, reason, occurredAt, resend, checkRound } = input;
  // The robot's check round (목업 31번); push data values are strings.
  const round = checkRound === undefined ? 1 : Number(checkRound);
  const entry = typeof reason === "string" && Object.hasOwn(messages, reason)
    ? messages[reason] : null;
  if (
    typeof deviceId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(deviceId) ||
    typeof notificationId !== "string" || !uuid.test(notificationId) ||
    typeof incidentId !== "string" || !uuid.test(incidentId) ||
    !entry || typeof entry[1] !== "string" || level !== entry[0] ||
    typeof occurredAt !== "string" || !Number.isFinite(Date.parse(occurredAt)) ||
    new Date(occurredAt).toISOString() !== occurredAt ||
    !(resend === undefined || resend === true || resend === "true") ||
    !(Number.isInteger(round) && round >= 1 && round <= 10000) ||
    (checkRound !== undefined && String(round) !== String(checkRound))
  ) return null;
  // [재확인 · N회차]: the robot checking the incident again, from its second round (목업 31·37번).
  const text = round > 1 ? `[재확인 · ${round}회차] ${entry[1]}` : entry[1];
  return {
    // [재발신]: same level and message as the first notification.
    body: resend ? `[재발신] ${text}` : text,
    data: {
      kind: "fall", deviceId, notificationId, incidentId, level, reason, occurredAt,
      // Push data values are strings; round-trips through isFallNotification.
      ...(resend ? { resend: "true" } : {}),
      ...(round > 1 ? { checkRound: String(round) } : {}),
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
