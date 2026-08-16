"use client";

import { useId, useState, type FormEvent } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowRight,
  Eye,
  EyeSlash,
  LockKey,
} from "@phosphor-icons/react";
import {
  loginApiPath,
  responseLoginStep,
  responseMessage,
  successfulLoginRedirect,
  type LoginResponse,
  type LoginStep,
} from "./login-flow";

type LoginPanelProps = {
  returnTo: string;
};

const STEP_COPY: Record<
  LoginStep,
  { eyebrow: string; title: string; description: string; submit: string }
> = {
  credentials: {
    eyebrow: "MEMBER ACCESS",
    title: "홈캠에 로그인",
    description: "초대받은 계정으로 우리 집 홈캠에 안전하게 연결하세요.",
    submit: "로그인",
  },
  new_password: {
    eyebrow: "FIRST SIGN IN",
    title: "새 비밀번호 설정",
    description: "처음 로그인했습니다. 앞으로 사용할 비밀번호를 정해 주세요.",
    submit: "비밀번호 변경",
  },
  mfa: {
    eyebrow: "SECURITY CHECK",
    title: "인증 코드 입력",
    description: "인증 앱에 표시된 6자리 코드를 입력해 주세요.",
    submit: "확인하고 로그인",
  },
};

export function LoginPanel({ returnTo }: LoginPanelProps) {
  const emailId = useId();
  const passwordId = useId();
  const newPasswordId = useId();
  const confirmPasswordId = useId();
  const mfaCodeId = useId();
  const [step, setStep] = useState<LoginStep>("credentials");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"error" | "info">("error");

  const copy = STEP_COPY[step];

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;

    if (step === "new_password" && newPassword !== confirmPassword) {
      setMessageTone("error");
      setMessage("새 비밀번호가 서로 일치하지 않습니다.");
      return;
    }
    if (step === "mfa" && !/^\d{6}$/.test(mfaCode)) {
      setMessageTone("error");
      setMessage("인증 앱의 6자리 숫자를 입력해 주세요.");
      return;
    }

    setBusy(true);
    setMessage("");
    const body =
      step === "credentials"
        ? { email: email.trim(), password }
        : step === "new_password"
          ? { newPassword }
          : { mfaCode };

    try {
      const response = await fetch(loginApiPath(returnTo), {
        method: "POST",
        headers: {
          "content-type": "application/json",
        },
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify(body),
      });
      const payload = (await response.json().catch(() => ({}))) as LoginResponse;
      const redirectTo = successfulLoginRedirect(payload, returnTo);
      if (response.ok && redirectTo) {
        window.location.replace(redirectTo);
        return;
      }

      const nextStep = responseLoginStep(payload);
      if (nextStep) {
        setStep(nextStep);
        setMessageTone("info");
        setMessage(
          responseMessage(
            payload,
            nextStep === "new_password"
              ? "새 비밀번호를 설정해 주세요."
              : "인증 앱의 코드를 확인해 주세요.",
          ),
        );
        return;
      }

      setMessageTone("error");
      setMessage(
        responseMessage(
          payload,
          response.status === 429
            ? "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요."
            : "이메일 또는 비밀번호를 확인해 주세요.",
        ),
      );
    } catch {
      setMessageTone("error");
      setMessage("서버에 연결하지 못했습니다. 네트워크를 확인한 뒤 다시 시도해 주세요.");
    } finally {
      setBusy(false);
    }
  };

  const restart = () => {
    setStep("credentials");
    setPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setMfaCode("");
    setMessage("");
    setMessageTone("error");
  };

  return (
    <main className="homecam-login-shell">
      <section className="homecam-login-panel" aria-labelledby="homecam-login-title">
        <header className="homecam-login-header">
          <Link className="homecam-login-brand" href="/" aria-label="MALBUT 홈캠">
            <strong>/MALBUT</strong>
            <small>HOME CAMERA</small>
          </Link>
          <span>PRIVATE ACCESS</span>
        </header>

        <div className="homecam-login-body">
          <div className="homecam-login-copy">
            <span>{copy.eyebrow}</span>
            <h1 id="homecam-login-title">{copy.title}</h1>
            <p>{copy.description}</p>
          </div>

          <form className="homecam-login-form" onSubmit={submit}>
            {step === "credentials" && (
              <>
                <label htmlFor={emailId}>이메일</label>
                <input
                  id={emailId}
                  name="email"
                  type="email"
                  inputMode="email"
                  autoComplete="username"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="name@example.com"
                  required
                  disabled={busy}
                  autoFocus
                />

                <label htmlFor={passwordId}>비밀번호</label>
                <div className="homecam-password-field">
                  <input
                    id={passwordId}
                    name="password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="비밀번호 입력"
                    required
                    disabled={busy}
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? "비밀번호 숨기기" : "비밀번호 보기"}
                    aria-pressed={showPassword}
                    onClick={() => setShowPassword((current) => !current)}
                    disabled={busy}
                  >
                    {showPassword ? <EyeSlash size={18} /> : <Eye size={18} />}
                  </button>
                </div>
              </>
            )}

            {step === "new_password" && (
              <>
                <label htmlFor={newPasswordId}>새 비밀번호</label>
                <input
                  id={newPasswordId}
                  name="newPassword"
                  type="password"
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="새 비밀번호 입력"
                  minLength={12}
                  required
                  disabled={busy}
                  autoFocus
                />

                <label htmlFor={confirmPasswordId}>새 비밀번호 확인</label>
                <input
                  id={confirmPasswordId}
                  name="confirmPassword"
                  type="password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  placeholder="한 번 더 입력"
                  minLength={12}
                  required
                  disabled={busy}
                />
                <p className="homecam-login-hint">
                  12자 이상이며 영문 대·소문자, 숫자, 특수문자를 각각 포함해 주세요.
                </p>
              </>
            )}

            {step === "mfa" && (
              <>
                <label htmlFor={mfaCodeId}>6자리 인증 코드</label>
                <input
                  id={mfaCodeId}
                  className="homecam-mfa-input"
                  name="mfaCode"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  value={mfaCode}
                  onChange={(event) =>
                    setMfaCode(event.target.value.replace(/\D/g, "").slice(0, 6))
                  }
                  placeholder="000000"
                  required
                  disabled={busy}
                  autoFocus
                />
              </>
            )}

            <div
              className={`homecam-login-message is-${messageTone} ${message ? "is-visible" : ""}`}
              role={message && messageTone === "error" ? "alert" : "status"}
              aria-live={messageTone === "error" ? "assertive" : "polite"}
            >
              {message || " "}
            </div>

            <button className="homecam-login-submit" type="submit" disabled={busy}>
              <span>{busy ? "확인 중" : copy.submit}</span>
              {step === "credentials" ? (
                <ArrowRight size={17} weight="bold" aria-hidden="true" />
              ) : (
                <LockKey size={17} weight="bold" aria-hidden="true" />
              )}
            </button>

            {step !== "credentials" && (
              <button
                className="homecam-login-restart"
                type="button"
                onClick={restart}
                disabled={busy}
              >
                <ArrowLeft size={15} weight="bold" aria-hidden="true" />
                다른 계정으로 로그인
              </button>
            )}
          </form>
        </div>

        <footer className="homecam-login-footer">
          <span>초대받은 소유자와 가족만 이용할 수 있습니다.</span>
          <span>영상과 음성은 허용된 계정에만 연결됩니다.</span>
        </footer>
      </section>
    </main>
  );
}
