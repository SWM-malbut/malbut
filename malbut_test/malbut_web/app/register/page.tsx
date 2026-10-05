import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { requireChatGPTUser } from "../chatgpt-auth";
import { userHasHomecam } from "../../db/homecam";
import { RegisterScreen } from "./register-screen";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "말벗 등록 | 말벗 홈캠" };

type RegisterPageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function RegisterPage({ searchParams }: RegisterPageProps) {
  const localUiDemo =
    process.env.NODE_ENV !== "production" &&
    process.env.NEXT_PUBLIC_HOMECAM_UI_DEMO === "1";
  // Local UI demo: no sign-in or database; ?demo=new shows a person without a 말벗.
  if (localUiDemo) return <RegisterScreen hasHomecam={(await searchParams).demo !== "new"} demo />;
  const user = await requireChatGPTUser("/register");
  if (!user.chosenName) redirect(`/auth/name?${new URLSearchParams({ return_to: "/register" })}`);
  return <RegisterScreen hasHomecam={await userHasHomecam(user.userId)} />;
}
