import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getChatGPTUser } from "../../chatgpt-auth";
import { describeInvite } from "../../../db/guardians";
import { getRuntimeEnvironment } from "../../runtime-env";
import { enabledSocialProviders } from "../../social-auth";
import { InviteScreen, type InviteDemo } from "./invite-screen";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "보호자 초대 | 말벗 홈캠" };

type InvitePageProps = {
  params: Promise<{ token: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

const DEMOS: readonly InviteDemo[] = ["login", "done", "already", "owner", "expired"];

export default async function InvitePage({ params, searchParams }: InvitePageProps) {
  const { token } = await params;
  const returnTo = `/invite/${encodeURIComponent(token)}`;
  const localUiDemo =
    process.env.NODE_ENV !== "production" &&
    process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1";
  if (localUiDemo) {
    // Local UI demo: no sign-in or database; ?demo=login|done|already|owner|expired.
    const asked = (await searchParams).demo;
    const demo = DEMOS.find((value) => value === asked) ?? "login";
    return (
      <InviteScreen token={token} returnTo={returnTo} signedIn={demo !== "login"} demo={demo}
        invite={demo === "expired" ? null : { ownerName: "김말벗", deviceName: "우리 집 말벗" }}
        enabled={["kakao", "naver", "google"]} />
    );
  }
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  const invite = sessionSecret ? await describeInvite(token, sessionSecret) : null;
  const user = await getChatGPTUser();
  // Name first, then the invite is accepted on this page.
  if (user && invite && !user.chosenName) redirect(`/auth/name?${new URLSearchParams({ return_to: returnTo })}`);
  return (
    <InviteScreen token={token} returnTo={returnTo} signedIn={Boolean(user)} invite={invite}
      enabled={enabledSocialProviders()} />
  );
}
