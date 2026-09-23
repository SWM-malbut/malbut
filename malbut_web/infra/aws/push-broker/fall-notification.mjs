// Shared by the trusted web server and push broker. No model-written message
// or arbitrary destination URL is accepted here.
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const messages = {
  fall_observed_person_okay: ["info", "넘어지는 장면이 감지됐습니다. 대상자는 괜찮다고 답했습니다."],
  person_no_response: ["check", "상태 확인 질문에 답변이 없습니다. 확인이 필요합니다."],
  check_required_not_confirmed_fall: ["check", "낙상 의심 상황을 확인하지 못했습니다. 상태를 확인해 주세요."],
  help_requested: ["urgent", "대상자가 도움을 요청했습니다. 즉시 확인해 주세요."],
};

export function buildFallNotification(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const { deviceId, notificationId, incidentId, level, reason, occurredAt } = input;
  const entry = typeof reason === "string" && Object.hasOwn(messages, reason)
    ? messages[reason] : null;
  if (
    typeof deviceId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(deviceId) ||
    typeof notificationId !== "string" || !uuid.test(notificationId) ||
    typeof incidentId !== "string" || !uuid.test(incidentId) ||
    !entry || level !== entry[0] ||
    typeof occurredAt !== "string" || !Number.isFinite(Date.parse(occurredAt)) ||
    new Date(occurredAt).toISOString() !== occurredAt
  ) return null;
  return {
    body: entry[1],
    data: {
      kind: "fall", deviceId, notificationId, incidentId, level, reason, occurredAt,
      // A fall detail page does not exist yet; open this device's live view.
      url: `/?view=live&device=${encodeURIComponent(deviceId)}`,
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
