import { authenticateDeviceToken, type DeviceIdentity } from "../db/homecam";
import { getBearerToken } from "../db/homecam-security";

export async function getRequestDevice(
  request: Request,
): Promise<DeviceIdentity | null> {
  const token = getBearerToken(request.headers.get("authorization"));
  if (!token) return null;
  return authenticateDeviceToken(token);
}
