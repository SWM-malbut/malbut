import {
  getHomecamEvent,
  listHomecamEvents,
  userCanViewDevice,
} from "../../../../../db/homecam";
import { HOME_CAM_EVENT_TYPES } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "이 홈캠의 이벤트를 볼 권한이 없습니다." }, 403);
  }
  const url = new URL(request.url);
  const eventIds = url.searchParams.getAll("event");
  if (eventIds.length > 1) {
    return noStore({ error: "이벤트 ID를 하나만 지정해 주세요." }, 400);
  }
  const eventId = eventIds[0];
  if (eventId) {
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        eventId,
      )
    ) {
      return noStore({ error: "이벤트를 찾을 수 없습니다." }, 404);
    }
    const event = await getHomecamEvent(deviceId, eventId);
    return noStore(
      {
        events: event ? [event] : [],
        nextBefore: null,
        nextBeforeId: null,
      },
      200,
    );
  }
  const eventTypes = url.searchParams
    .getAll("type")
    .flatMap((value) => value.split(","))
    .filter(Boolean);
  if (
    eventTypes.some(
      (eventType) =>
        !HOME_CAM_EVENT_TYPES.includes(
          eventType as (typeof HOME_CAM_EVENT_TYPES)[number],
        ),
    )
  ) {
    return noStore({ error: "이벤트 필터를 확인해 주세요." }, 400);
  }
  const limit = Number(url.searchParams.get("limit") ?? "50");
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    return noStore({ error: "조회 개수는 1~100이어야 합니다." }, 400);
  }
  const before = url.searchParams.get("before") ?? undefined;
  const beforeId = url.searchParams.get("beforeId") ?? undefined;
  if (
    Boolean(before) !== Boolean(beforeId) ||
    (before &&
      (!Number.isFinite(Date.parse(before)) ||
        new Date(Date.parse(before)).toISOString() !== before)) ||
    (beforeId && !isUuid(beforeId))
  ) {
    return noStore(
      { error: "before와 beforeId 커서를 함께 확인해 주세요." },
      400,
    );
  }
  const events = await listHomecamEvents({
    deviceId,
    eventTypes,
    before:
      before && beforeId
        ? { occurredAt: before, id: beforeId }
        : undefined,
    limit,
  });
  const lastEvent = events.length === limit ? events.at(-1) : undefined;
  return noStore(
    {
      events,
      nextBefore: lastEvent?.occurredAt ?? null,
      nextBeforeId: lastEvent?.id ?? null,
    },
    200,
  );
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}
