import { readFallCloudKeyView, setFallCloudKey } from "../../../../../db/fall-ai-review";
import { userCanViewDevice } from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { fallAiFailure } from "../../../../fall-ai-route";
import { getRuntimeEnvironment } from "../../../../runtime-env";
import { sameOriginJsonRequest } from "../../../../same-origin-request";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string }> };

async function member(request: Request, deviceId: string) {
  const email = await getRequestUserEmail(request);
  if (!email) return { response: noStore({ error: "로그인이 필요합니다." }, 401) };
  if (!(await userCanViewDevice(deviceId, email))) return { response: noStore({ error: "말벗을 찾을 수 없습니다." }, 404) };
  return { email };
}

/** Members see whether a key exists and its last 4 characters, never the key. */
export async function GET(request: Request, context: Context) {
  const { deviceId } = await context.params;
  const user = await member(request, deviceId);
  if (user.response) return user.response;
  try { return noStore(await readFallCloudKeyView(deviceId)); } catch (error) { return fallAiFailure(error); }
}

async function write(request: Request, context: Context, apiKey: string | null) {
  const { deviceId } = await context.params;
  const user = await member(request, deviceId);
  if (user.response) return user.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  try {
    const secret = getRuntimeEnvironment().FALL_KEY_ENCRYPTION_SECRET ?? "";
    await setFallCloudKey(deviceId, user.email, apiKey, secret);
    return noStore(await readFallCloudKeyView(deviceId));
  } catch (error) { return fallAiFailure(error); }
}

export async function PUT(request: Request, context: Context) {
  const body = await request.clone().json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 1 ||
      typeof body.apiKey !== "string") {
    return noStore({ error: "키 형식을 확인해 주세요." }, 400);
  }
  return write(request, context, body.apiKey);
}

export async function DELETE(request: Request, context: Context) {
  return write(request, context, null);
}
