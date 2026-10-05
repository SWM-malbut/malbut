import type { SocialProvider } from "../../../db/web-auth";

const PROVIDERS: Array<{ provider: SocialProvider; label: string }> = [
  { provider: "kakao", label: "카카오로 계속하기" },
  { provider: "naver", label: "네이버로 계속하기" },
  { provider: "google", label: "Google로 계속하기" },
];

const ERROR_COPY: Record<string, string> = {
  cancelled: "로그인을 취소했어요.",
  expired: "로그인 시간이 지났어요. 다시 시도해 주세요.",
  failed: "로그인하지 못했어요. 잠시 뒤 다시 시도해 주세요.",
  unavailable: "이 로그인 방법은 아직 준비 중이에요.",
};

/** 카카오·네이버·Google로 계속하기: the login page and the invite page both use these. */
export function SocialProviderButtons({ returnTo, enabled }: { returnTo: string; enabled: readonly SocialProvider[] }) {
  const query = new URLSearchParams({ return_to: returnTo }).toString();
  return (
    <div className="ui-login-providers">
      {PROVIDERS.map(({ provider, label }) => enabled.includes(provider) ? (
        <a key={provider} className={`ui-login-provider is-${provider}`} href={`/auth/oidc/${provider}?${query}`}>
          {label}
        </a>
      ) : (
        <span key={provider} className={`ui-login-provider is-${provider} is-unavailable`} aria-disabled="true">
          {label}<small>준비 중</small>
        </span>
      ))}
    </div>
  );
}

/** 로그인 화면(목업): 카카오·네이버·Google로 계속하기. */
export function SocialLoginPanel({
  returnTo,
  enabled,
  error,
}: {
  returnTo: string;
  enabled: readonly SocialProvider[];
  error: string | null;
}) {
  const query = new URLSearchParams({ return_to: returnTo }).toString();
  const message = error ? ERROR_COPY[error] ?? ERROR_COPY.failed : "";
  return (
    <main className="ui-login">
      <header className="ui-login-head">
        <span className="ui-login-mark" aria-hidden="true">말</span>
        <h1>말벗 홈캠</h1>
        <p>로그인하고 우리 집 말벗의 상태와 사건을 확인하세요.</p>
      </header>

      {message && <p className="ui-login-error" role="alert">{message}</p>}

      <SocialProviderButtons returnTo={returnTo} enabled={enabled} />

      <div className="ui-login-note">
        <strong>처음 가입한 방법으로 로그인하세요</strong>
        <span>다른 방법으로 들어오면 새 계정이 만들어져서, 지금 보던 말벗이 보이지 않아요.</span>
      </div>

      <footer className="ui-login-foot">
        <span>영상과 음성은 허용된 계정에만 연결됩니다.</span>
        <a href={`/auth/login?${query}&method=email`}>기존 이메일로 로그인</a>
      </footer>
    </main>
  );
}
