import type { Metadata } from "next";
import { enabledSocialProviders } from "../../social-auth";
import { LoginPanel } from "./login-panel";
import { safeRelativeReturnPath } from "./login-flow";
import { SocialLoginPanel } from "./social-login-panel";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "로그인 | 말벗 홈캠",
  description: "말벗 홈캠 소유자와 보호자를 위한 로그인",
};

type LoginPageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

const first = (value: string | string[] | undefined) => (Array.isArray(value) ? value[0] : value);

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const params = await searchParams;
  const returnTo = safeRelativeReturnPath(first(params.return_to));
  // Email login stays reachable until registration codes replace it (SWM25-229).
  if (first(params.method) === "email") return <LoginPanel returnTo={returnTo} />;
  return (
    <SocialLoginPanel
      returnTo={returnTo}
      enabled={enabledSocialProviders()}
      error={first(params.error) ?? null}
    />
  );
}
