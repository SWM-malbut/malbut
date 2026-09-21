import type { Metadata } from "next";
import { LoginPanel } from "./login-panel";
import { safeRelativeReturnPath } from "./login-flow";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "로그인 | MALBUT 홈캠",
  description: "MALBUT 홈캠 소유자와 가족을 위한 보안 로그인",
};

type LoginPageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const params = await searchParams;
  const requestedReturnTo = Array.isArray(params.return_to)
    ? params.return_to[0]
    : params.return_to;

  return <LoginPanel returnTo={safeRelativeReturnPath(requestedReturnTo)} />;
}
