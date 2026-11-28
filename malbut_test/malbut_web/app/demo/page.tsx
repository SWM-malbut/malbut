import { notFound } from "next/navigation";
import { requireChatGPTUser } from "../chatgpt-auth";
import { userCanManageDevice } from "../../db/homecam";
import { DemoScreen } from "./screen";

export const dynamic = "force-dynamic";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ device?: string }>;
}) {
  const { device } = await searchParams;
  const local =
    process.env.NODE_ENV === "development" &&
    process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1" &&
    (!device || device === "local-demo-homecam");
  if (!local) {
    if (!device || typeof device !== "string") notFound();
    const user = await requireChatGPTUser(
      "/demo?device=" + encodeURIComponent(device),
    );
    if (!(await userCanManageDevice(device, user.userId))) notFound();
  }
  return <DemoScreen device={device ?? ""} local={local} />;
}
