import { getRuntimeEnvironment } from "../../../runtime-env";
import {
  clearRequestRateLimit,
  consumeRequestRateLimit,
  extendAuthorizedMasterSession,
  getAuthorizedMasterSession,
  getPasswordAuthorizedViewerSession,
  type ActiveSession,
} from "../../../../db/petcam";
import { isValidViewerPassword } from "../../../../db/session-secret";
import { requestBrokerSession, type KvsRole } from "../../../kvs-broker";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../kvs-device-config";
import {
  canBroadcastForConfiguredAccount,
  getRequestUserEmail,
} from "../../../server-auth";

export const dynamic = "force-dynamic";

type SessionEnv = DeviceKvsEnvironment & {
  PETCAM_SHARE_SECRET?: string;
};

export async function POST(request: Request) {
  const payload = (await request.json().catch(() => null)) as {
    roomCode?: string;
    role?: string;
    clientId?: string;
    viewerPassword?: string;
  } | null;
  const roomCode = payload?.roomCode?.trim().toUpperCase() ?? "";
  const role = payload?.role as KvsRole | undefined;
  const clientId = payload?.clientId?.trim();
  const viewerPassword = payload?.viewerPassword ?? "";

  if (!/^[A-Z2-9]{6}$/.test(roomCode) || !role || !["MASTER", "VIEWER"].includes(role)) {
    return noStore({ error: "세션 코드와 역할을 확인해 주세요." }, 400);
  }
  if (
    role === "VIEWER" &&
    (!clientId ||
      !/^(?!AWS_)[A-Za-z0-9_-]{1,128}$/.test(clientId) ||
      !isValidViewerPassword(viewerPassword))
  ) {
    return noStore({ error: "세션 코드와 시청 비밀번호를 확인해 주세요." }, 400);
  }

  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "ID 로그인이 필요합니다." }, 401);
  if (role === "MASTER" && !canBroadcastForConfiguredAccount(userEmail)) {
    return noStore({ error: "이 ID에는 영상 공개 권한이 없습니다." }, 403);
  }

  const runtime = getRuntimeEnvironment() as SessionEnv;
  if (!runtime.PETCAM_SHARE_SECRET) {
    return noStore({ error: "시청 비밀번호 보안 설정이 필요합니다." }, 503);
  }

  let session: ActiveSession | null;
  if (role === "MASTER") {
    session = await getAuthorizedMasterSession(userEmail, roomCode);
  } else {
    const canTryPassword = await consumeRequestRateLimit({
      userEmail,
      roomCode,
      scope: "viewer-password",
      limit: 5,
    });
    if (!canTryPassword) return rateLimited();
    session = await getPasswordAuthorizedViewerSession(
      roomCode,
      viewerPassword,
      runtime.PETCAM_SHARE_SECRET,
    );
    if (session) {
      await clearRequestRateLimit({ userEmail, roomCode, scope: "viewer-password" });
    }
  }
  if (!session) {
    const error =
      role === "MASTER"
        ? "세션이 없거나 송출 권한이 없습니다."
        : "세션 코드 또는 시청 비밀번호가 올바르지 않습니다.";
    return noStore({ error }, 403);
  }

  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, session.deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources) {
    return noStore({ error: "이 장치의 AWS 리소스가 설정되지 않았습니다." }, 503);
  }
  const storageMode = Boolean(
    resources.streamArn &&
      resources.storageChannelArn &&
      session.channelArn === resources.storageChannelArn,
  );
  const channelMode = storageMode ? "storage" : "p2p";
  const expectedChannelArn = storageMode
    ? resources.storageChannelArn
    : resources.p2pChannelArn;
  if (!expectedChannelArn || session.channelArn !== expectedChannelArn) {
    return noStore({ error: "AWS 저장 채널 설정이 일치하지 않습니다." }, 503);
  }
  if (
    storageMode &&
    role === "MASTER" &&
    !(await extendAuthorizedMasterSession(userEmail, roomCode))
  ) {
    return noStore({ error: "세션이 없거나 송출 권한이 없습니다." }, 403);
  }

  const canIssueCredentials = await consumeRequestRateLimit({
    userEmail,
    roomCode,
    scope: `${role.toLowerCase()}-credentials`,
    limit: 10,
  });
  if (!canIssueCredentials) return rateLimited();

  try {
    const broker = await requestBrokerSession({
      deviceId: session.deviceId,
      role,
      clientId,
      channelMode,
    });
    if (broker.channelArn !== session.channelArn) {
      return noStore({ error: "AWS 채널 설정이 일치하지 않습니다." }, 503);
    }
    return noStore(
      {
        ...broker,
        roomCode,
        clientId: role === "VIEWER" ? clientId : null,
        storageMode,
      },
      200,
    );
  } catch {
    return noStore({ error: "AWS 실시간 연결 정보를 발급하지 못했습니다." }, 503);
  }
}

function noStore(body: unknown, status: number) {
  return Response.json(body, {
    status,
    headers: { "cache-control": "no-store" },
  });
}

function rateLimited() {
  return Response.json(
    { error: "연결 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." },
    {
      status: 429,
      headers: {
        "cache-control": "no-store",
        "retry-after": "60",
      },
    },
  );
}
