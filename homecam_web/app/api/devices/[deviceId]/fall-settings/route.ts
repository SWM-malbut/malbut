import { userCanManageDevice, userCanViewDevice } from "../../../../../db/homecam";
import { readFallSettingsView, saveFallSettings } from "../../../../../db/fall-settings";
import { parseFallSettingsPatch } from "../../../../fall-settings-contract";
import { getRequestUserEmail } from "../../../../server-auth";
import { noStore } from "../../../../api-response";
import { getRuntimeEnvironment } from "../../../../runtime-env";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string }> };

function failure(error: unknown) {
  const code = error instanceof Error ? error.message : "";
  if (code === "FALL_SETTINGS_MIGRATION_REQUIRED") return noStore({ error: "낙상 설정 DB 준비가 필요합니다." }, 503);
  if (code === "FALL_SETTINGS_FORBIDDEN") return noStore({ error: "소유자만 낙상 설정을 변경할 수 있습니다." }, 403);
  if (code === "FALL_SETTINGS_REVISION_CONFLICT") return noStore({ error: "다른 곳에서 설정이 바뀌었습니다. 최신 설정을 확인한 뒤 다시 변경해 주세요." }, 409);
  return noStore({ error: "낙상 설정을 확인하지 못했습니다." }, 500);
}

export async function GET(request: Request, context: Context) {
  const email = await getRequestUserEmail(request);
  if (!email) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, email))) return noStore({ error: "로봇을 볼 권한이 없습니다." }, 403);
  try { return noStore(await readFallSettingsView(deviceId)); }
  catch (error) { return failure(error); }
}

export async function PATCH(request: Request, context: Context) {
  const email = await getRequestUserEmail(request);
  if (!email) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, email))) return noStore({ error: "소유자만 낙상 설정을 변경할 수 있습니다." }, 403);
  const patch = parseFallSettingsPatch(await request.json().catch(() => null));
  if (!patch) return noStore({ error: "낙상 설정 형식을 확인해 주세요." }, 400);
  try {
    // A save receipt is NOT a robot apply receipt. Return the exact saved revision.
    const saved = await saveFallSettings(deviceId, email, patch);
    return noStore({ saved: true, savedRevision: saved.settingsRevision });
  } catch (error) { return failure(error); }
}

function sameOriginJsonRequest(request: Request) {
  if (request.headers.get("sec-fetch-site")?.toLowerCase() === "cross-site") return false;
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") return false;
  const configured = getRuntimeEnvironment().AUTH_PUBLIC_ORIGIN?.trim();
  let expected = new URL(request.url).origin;
  if (configured) {
    try {
      const url = new URL(configured);
      if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash || url.username || url.password) return false;
      expected = url.origin;
    } catch { return false; }
  }
  return request.headers.get("origin") === expected;
}
