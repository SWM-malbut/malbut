import type { Metadata } from "next";
import { requireChatGPTUser } from "../../chatgpt-auth";
import { safeRelativeReturnPath } from "../login/login-flow";
import { NameScreen } from "./name-screen";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "이름 정하기 | 말벗 홈캠" };

type NamePageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function NamePage({ searchParams }: NamePageProps) {
  const params = await searchParams;
  const raw = Array.isArray(params.return_to) ? params.return_to[0] : params.return_to;
  const returnTo = safeRelativeReturnPath(raw);
  const user = await requireChatGPTUser(returnTo);
  return <NameScreen initialName={user.chosenName ?? ""} returnTo={returnTo} />;
}
