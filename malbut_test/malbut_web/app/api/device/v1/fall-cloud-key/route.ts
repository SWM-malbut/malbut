import { syncFallCloudKeyForDevice } from "../../../../../db/fall-ai-review";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { readBoundedJson } from "../../../../fall-contract";
import { getRuntimeEnvironment } from "../../../../runtime-env";

export const dynamic = "force-dynamic";

/**
 * The fall node's key sync: `{"knownVersion":2,"model":"gemma4:31b"}` →
 * `{"keyVersion":3,"changed":true,"apiKey":"…"|null}`. The key is sent only
 * when the robot's copy is stale; keyVersion 0 means "keep your own file".
 */
export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  if (request.headers.get("x-malbut-device-id") !== device.deviceId) {
    return noStore({ error: "기록의 장치와 인증된 장치가 다릅니다." }, 403);
  }
  const body = await readBoundedJson(request, 1024) as Record<string, unknown> | undefined;
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      Object.keys(body).length !== 2 || !Number.isSafeInteger(body.knownVersion) ||
      (body.knownVersion as number) < 0 ||
      !(body.model === null || (typeof body.model === "string" && /^[A-Za-z0-9_.:-]{1,100}$/.test(body.model)))) {
    return noStore({ error: "키 동기화 형식을 확인해 주세요." }, 400);
  }
  try {
    const secret = getRuntimeEnvironment().FALL_KEY_ENCRYPTION_SECRET ?? "";
    return noStore(await syncFallCloudKeyForDevice(device.deviceId, body.knownVersion as number,
      body.model as string | null, secret));
  } catch (error) {
    if (error instanceof Error && error.message === "FALL_MODEL_INVALID") {
      return noStore({ error: "모델 이름을 확인해 주세요." }, 400);
    }
    return noStore({ error: "키를 읽지 못했습니다." }, 503);
  }
}
