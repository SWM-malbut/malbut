import { exchangeDemo } from "../../../../../db/demo-feed";
import { getRequestDevice } from "../../../../device-auth";
import { noStore } from "../../../../api-response";
import { parseReport } from "../../../../demo-feed-contract";
import { readRobotJson } from "../../../../robot-contract";

export async function POST(req: Request) {
  const device = await getRequestDevice(req);
  if (!device) return noStore({ accepted: false }, 401);
  const report = parseReport(
    await readRobotJson(req, "state").catch(() => null),
  );
  if (!report) return noStore({ accepted: false }, 400);
  try {
    const accepted = await exchangeDemo(device.deviceId, report);
    return noStore({ accepted });
  } catch {
    return noStore({ accepted: false }, 503);
  }
}
