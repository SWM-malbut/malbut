import { getRuntimeEnvironment } from "../../../runtime-env";
import { createRegistrationCode } from "../../../../db/registration";
import { noStore } from "../../../api-response";

export const dynamic = "force-dynamic";

type RegistrationCodeEnv = {
  DEVICE_PROVISIONING_SECRET?: string;
  AUTH_SESSION_SECRET?: string;
};

const DEVICE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/** The team makes a registration code for one 말벗 (scripts/create-registration-code.mjs). */
export async function POST(request: Request) {
  const runtime = getRuntimeEnvironment() as RegistrationCodeEnv;
  const secret = runtime.DEVICE_PROVISIONING_SECRET;
  const sessionSecret = runtime.AUTH_SESSION_SECRET?.trim();
  if (!secret || secret.length < 43 || !sessionSecret) {
    return noStore({ error: "찾을 수 없습니다." }, 404);
  }
  if (!(await authorized(request, secret))) {
    return noStore({ error: "유효한 provisioning 인증이 필요합니다." }, 401);
  }
  const payload = (await request.json().catch(() => null)) as { deviceId?: unknown } | null;
  if (
    !payload ||
    typeof payload !== "object" ||
    Object.keys(payload).some((key) => key !== "deviceId") ||
    typeof payload.deviceId !== "string" ||
    !DEVICE_ID_PATTERN.test(payload.deviceId)
  ) {
    return noStore({ error: "요청 형식을 확인해 주세요. 예: {\"deviceId\":\"jetson-homecam\"}" }, 400);
  }
  try {
    const created = await createRegistrationCode({ deviceId: payload.deviceId, sessionSecret });
    return noStore({ deviceId: payload.deviceId, ...created }, 201);
  } catch (error) {
    if (error instanceof Error && error.message === "REGISTRATION_DEVICE_NOT_FOUND") {
      return noStore({ error: "등록된 장치가 아닙니다." }, 404);
    }
    return noStore({ error: "등록 코드를 만들지 못했습니다." }, 500);
  }
}

async function authorized(request: Request, expected: string) {
  const header = request.headers.get("authorization");
  if (!header?.startsWith("Bearer ")) return false;
  const received = header.slice("Bearer ".length);
  if (!received || received.length > 512) return false;
  const [left, right] = await Promise.all([
    sha256(received),
    sha256(expected),
  ]);
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

async function sha256(value: string) {
  return new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(value),
    ),
  );
}
