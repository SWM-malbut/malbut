import { getRobotMapPreview } from "../../../../../../db/robot-map";
import { getRequestUserEmail } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return new Response("로그인이 필요합니다.", { status: 401 });
  const { deviceId } = await context.params;
  const map = await getRobotMapPreview(deviceId, userEmail);
  if (!map) return new Response("지도를 찾을 수 없습니다.", { status: 404 });
  const bytes = Buffer.from(map.preview_base64, "base64");
  return new Response(bytes, {
    status: 200,
    headers: {
      "cache-control": "private, max-age=60, immutable",
      "content-type": "image/png",
      etag: `\"${map.revision}\"`,
      "x-content-type-options": "nosniff",
    },
  });
}
