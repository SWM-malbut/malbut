import {
  HEALTH_SERVICES,
  HEALTH_STATES,
  SERVICE_NAMES,
  syncServiceKeysForDevice,
  type HealthReport,
} from "../../../../../db/service-keys";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { readBoundedJson } from "../../../../fall-contract";
import { getRuntimeEnvironment } from "../../../../runtime-env";

export const dynamic = "force-dynamic";

const MODEL = /^[A-Za-z0-9_.:-]{1,100}$/;
const CODE = /^[a-z0-9_]{1,64}$/;
const record = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);
const exactly = (value: Record<string, unknown>, keys: readonly string[]) =>
  Object.keys(value).every((key) => keys.includes(key));

/**
 * The robot's key sync (키 받아 오기, every minute):
 * `{"known":{"openai":2,"kma":0},"models":{"openai":"gpt-5.6-luna"},"health":{"openai":{"state":"ok","code":null}}}`
 * → `{"openai":{"keyVersion":3,"changed":true,"apiKey":"…"|null},"kma":{"keyVersion":0,"changed":false,"apiKey":null}}`.
 * keyVersion 0 means "keep your own key"; apiKey null with changed=true means "the owner deleted it".
 */
export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  if (request.headers.get("x-malbut-device-id") !== device.deviceId) {
    return noStore({ error: "기록의 장치와 인증된 장치가 다릅니다." }, 403);
  }
  const body = await readBoundedJson(request, 2048);
  const known = record(body) ? body.known : null;
  const models = record(body) ? body.models : null;
  const health = record(body) ? body.health ?? {} : null;
  const knownOk = record(known) && exactly(known, SERVICE_NAMES) &&
    SERVICE_NAMES.every((name) => Number.isSafeInteger(known[name]) && (known[name] as number) >= 0);
  const modelsOk = record(models) && exactly(models, ["openai"]) &&
    (models.openai === null || (typeof models.openai === "string" && MODEL.test(models.openai)));
  const healthOk = record(health) && exactly(health, HEALTH_SERVICES) && Object.values(health).every((value) =>
    record(value) && exactly(value, ["state", "code"]) && (HEALTH_STATES as readonly unknown[]).includes(value.state) &&
    (value.code === null || (typeof value.code === "string" && CODE.test(value.code))));
  if (!record(body) || !exactly(body, ["known", "models", "health"]) || !knownOk || !modelsOk || !healthOk) {
    return noStore({ error: "키 동기화 형식을 확인해 주세요." }, 400);
  }
  try {
    return noStore(await syncServiceKeysForDevice({
      deviceId: device.deviceId,
      known: known as Record<(typeof SERVICE_NAMES)[number], number>,
      models: { openai: models.openai as string | null },
      health: health as HealthReport,
      secret: getRuntimeEnvironment().FALL_KEY_ENCRYPTION_SECRET ?? "",
    }));
  } catch {
    return noStore({ error: "키를 읽지 못했습니다." }, 503);
  }
}
