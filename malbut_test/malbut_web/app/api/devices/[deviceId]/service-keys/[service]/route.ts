import { readFallCloudKeyView, setFallCloudKey } from "../../../../../../db/fall-ai-review";
import { isValidFallCloudKey } from "../../../../../fall-cloud-key-crypto";
import { userCanManageDevice } from "../../../../../../db/homecam";
import { consumeRequestRateLimit } from "../../../../../../db/request-rate-limit";
import { isValidServiceKey, readServiceKeyViews, setServiceKey } from "../../../../../../db/service-keys";
import { noStore } from "../../../../../api-response";
import { getRuntimeEnvironment } from "../../../../../runtime-env";
import { sameOriginJsonRequest } from "../../../../../same-origin-request";
import { getRequestUserId } from "../../../../../server-auth";
import {
  checkServiceKey,
  DEFAULT_FALL_MODEL,
  DEFAULT_OPENAI_MODEL,
  keyCheckMessage,
  type KeyCheckService,
} from "../../../../../service-key-check";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ deviceId: string; service: string }> };
const SERVICES: readonly KeyCheckService[] = ["openai", "kma", "fall"];
// Every save asks the service once; this keeps one owner from turning that into many calls.
const SAVES_PER_MINUTE = 10;

async function owner(request: Request, context: Context) {
  const userId = await getRequestUserId(request);
  if (!userId) return { response: noStore({ error: "로그인이 필요합니다." }, 401) };
  if (!sameOriginJsonRequest(request)) return { response: noStore({ error: "요청 출처를 확인해 주세요." }, 403) };
  const { deviceId, service } = await context.params;
  const name = SERVICES.find((value) => value === service);
  if (!name) return { response: noStore({ error: "찾을 수 없습니다." }, 404) };
  if (!(await userCanManageDevice(deviceId, userId))) return { response: noStore({ error: "소유자만 바꿀 수 있어요." }, 403) };
  const secret = getRuntimeEnvironment().FALL_KEY_ENCRYPTION_SECRET ?? "";
  return { userId, deviceId, service: name, secret };
}

async function viewOf(deviceId: string, service: KeyCheckService) {
  return service === "fall" ? readFallCloudKeyView(deviceId) : (await readServiceKeyViews(deviceId))[service];
}

async function save(me: { userId: string; deviceId: string; service: KeyCheckService; secret: string }, apiKey: string | null) {
  if (me.service === "fall") await setFallCloudKey(me.deviceId, me.userId, apiKey, me.secret);
  else await setServiceKey({ deviceId: me.deviceId, userId: me.userId, service: me.service, apiKey, secret: me.secret });
  return noStore(await viewOf(me.deviceId, me.service), 200);
}

/** "확인하고 저장": the key is saved only if the service accepts it for the robot's model. */
export async function PUT(request: Request, context: Context) {
  const me = await owner(request, context);
  if (me.response) return me.response;
  const body = (await request.json().catch(() => null)) as { apiKey?: unknown } | null;
  const apiKey = body && typeof body.apiKey === "string" ? body.apiKey.trim() : null;
  const valid = me.service === "fall" ? isValidFallCloudKey(apiKey) : isValidServiceKey(apiKey);
  if (!body || Object.keys(body).some((key) => key !== "apiKey") || !apiKey || !valid) {
    return noStore({ error: "키 형식을 확인해 주세요. 띄어쓰기 없이 붙여 넣어 주세요." }, 400);
  }
  if (!(await consumeRequestRateLimit({ userId: me.userId, roomCode: me.deviceId, scope: "service-key", limit: SAVES_PER_MINUTE }))) {
    return noStore({ error: "잠시 후 다시 시도해 주세요." }, 429);
  }
  const model = me.service === "kma" ? null
    : (await viewOf(me.deviceId, me.service)).robotModel ?? (me.service === "fall" ? DEFAULT_FALL_MODEL : DEFAULT_OPENAI_MODEL);
  const result = await checkServiceKey({ service: me.service, apiKey, model });
  if (result !== "ok") return noStore({ error: keyCheckMessage(me.service, result, model ?? ""), result }, 422);
  return save(me, apiKey);
}

/** "지우기": the robot removes its copy on the next sync and has no key (no team key fallback). */
export async function DELETE(request: Request, context: Context) {
  const me = await owner(request, context);
  if (me.response) return me.response;
  return save(me, null);
}
