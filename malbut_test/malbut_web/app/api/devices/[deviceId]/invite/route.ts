import {
  createInviteLink,
  currentInviteLink,
  revokeInviteLink,
} from "../../../../../db/guardians";
import { userCanManageDevice } from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { getRuntimeEnvironment } from "../../../../runtime-env";
import { sameOriginJsonRequest } from "../../../../same-origin-request";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ deviceId: string }> };

const view = (invite: { token: string; expiresAt: string; joined: number } | null) =>
  invite && { path: `/invite/${invite.token}`, expiresAt: invite.expiresAt, joined: invite.joined };

/** 설정 › 보호자 › 보호자 초대 링크 (소유자만). */
async function owner(request: Request, context: Context, mutation: boolean) {
  const userId = await getRequestUserId(request);
  if (!userId) return { response: noStore({ error: "로그인이 필요합니다." }, 401) };
  if (mutation && !sameOriginJsonRequest(request)) return { response: noStore({ error: "요청 출처를 확인해 주세요." }, 403) };
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, userId))) {
    return { response: noStore({ error: "소유자만 초대 링크를 관리할 수 있어요." }, 403) };
  }
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (!sessionSecret) return { response: noStore({ error: "지금은 초대 링크를 만들 수 없어요." }, 503) };
  return { userId, deviceId, sessionSecret };
}

export async function GET(request: Request, context: Context) {
  const me = await owner(request, context, false);
  if (me.response) return me.response;
  return noStore({ invite: view(await currentInviteLink(me.deviceId, me.sessionSecret)) }, 200);
}

export async function POST(request: Request, context: Context) {
  const me = await owner(request, context, true);
  if (me.response) return me.response;
  const invite = await createInviteLink({ deviceId: me.deviceId, ownerUserId: me.userId, sessionSecret: me.sessionSecret });
  return noStore({ invite: view(invite) }, 201);
}

export async function DELETE(request: Request, context: Context) {
  const me = await owner(request, context, true);
  if (me.response) return me.response;
  return noStore({ revoked: await revokeInviteLink({ deviceId: me.deviceId, ownerUserId: me.userId }) }, 200);
}
