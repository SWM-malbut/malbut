"use client";

import Link from "next/link";
import { useId, useState, type FormEvent } from "react";
import { formatRegistrationCodeInput } from "./code-input";

type Step = "form" | "taken" | "done";
type History = "keep" | "delete";

const HISTORY_CHOICES: Array<{ value: History; label: string; detail: string }> = [
  { value: "delete", label: "지우기", detail: "말벗을 다른 집으로 옮길 때. 새 소유자는 이전 기록을 볼 수 없어요." },
  { value: "keep", label: "남기기", detail: "소유자 계정만 잃어버렸을 때. 새 소유자가 이전 기록을 이어서 봐요." },
];

// Local UI demo only: TEST-NEW2 registers at once, TEST-TAKE belongs to someone else.
function demoReply(code: string, choice?: History) {
  const compact = code.replace(/[\s-]/g, "").toUpperCase();
  if (compact === "TESTNEW2" || (compact === "TESTTAKE" && choice)) return { ok: true, payload: { status: "registered" } };
  if (compact === "TESTTAKE") return { ok: false, payload: { status: "needs_confirmation" } };
  return { ok: false, payload: { status: "invalid", error: "코드를 찾을 수 없어요. 받은 코드를 다시 확인해 주세요." } };
}

/** 말벗 등록 (목업 Register): 코드 입력 → (이미 소유자가 있으면) 다시 등록 확인 → 등록 완료. */
export function RegisterScreen({ hasHomecam, demo = false }: { hasHomecam: boolean; demo?: boolean }) {
  const inputId = useId();
  const [code, setCode] = useState("");
  const [step, setStep] = useState<Step>("form");
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const register = async (choice?: History) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const { ok, payload } = demo ? demoReply(code, choice) : await (async () => {
        const response = await fetch("/api/registrations", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(choice ? { code, history: choice } : { code }),
          credentials: "same-origin",
          cache: "no-store",
        });
        return { ok: response.ok, payload: (await response.json().catch(() => ({}))) as { status?: unknown; error?: unknown } };
      })();
      if (ok && payload.status === "registered") {
        setStep("done");
      } else if (payload.status === "needs_confirmation") {
        setHistory(null);
        setStep("taken");
      } else {
        setStep("form");
        setError(typeof payload.error === "string" ? payload.error : "등록하지 못했어요. 잠시 후 다시 시도해 주세요.");
      }
    } catch {
      setError("등록하지 못했어요. 인터넷 연결을 확인해 주세요.");
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void register();
  };

  return (
    <div className="ui-register">
      <header className="ui-register-top">
        {hasHomecam && step !== "done" && <Link className="ui-back" href="/?view=settings">‹ 설정</Link>}
        <h1>말벗 등록</h1>
      </header>
      <main className="ui-register-body">
        {step === "form" && (
          <>
            <div className="ui-register-intro">
              <h2>{hasHomecam ? "새 등록 코드를 입력해 주세요" : "아직 연결된 말벗이 없어요"}</h2>
              <p>등록 코드를 입력하면 이 계정이 말벗의 소유자가 돼요. 소유자는 설정을 바꾸고 보호자를 초대할 수 있어요.</p>
            </div>
            <form className="ui-card ui-register-form" onSubmit={submit}>
              <label className="ui-field" htmlFor={inputId}>
                <span>등록 코드</span>
                <input
                  id={inputId}
                  value={code}
                  onChange={(event) => setCode(formatRegistrationCodeInput(event.target.value))}
                  placeholder="예: 7Q2K-9XHM"
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  maxLength={9}
                  aria-invalid={Boolean(error)}
                  aria-describedby={error ? `${inputId}-error` : undefined}
                />
              </label>
              {error && <span id={`${inputId}-error`} className="ui-register-error" role="alert">{error}</span>}
              <button type="submit" className="ui-button is-strong ui-register-main" disabled={busy}>
                {busy ? "확인 중" : "등록하기"}
              </button>
              <small className="ui-note">등록 코드는 말벗 팀에게 받을 수 있어요.</small>
            </form>
          </>
        )}

        {step === "taken" && (
          <section className="ui-card ui-register-taken" aria-labelledby={`${inputId}-taken`}>
            <span className="ui-badge is-danger">이미 등록된 말벗</span>
            <strong id={`${inputId}-taken`}>다시 등록할까요?</strong>
            <p>이 말벗에는 이미 소유자가 있어요. 다시 등록하면 지금의 소유자와 보호자가 모두 지워지고, 내가 새 소유자가 돼요.</p>
            <p className="is-muted">소유자 계정을 쓸 수 없게 됐거나 말벗을 다른 집으로 옮길 때만 다시 등록하세요.</p>
            <div className="ui-register-history">
              <strong>지난 사건 기록과 의견은 어떻게 할까요?</strong>
              <div className="ui-radio-list" role="radiogroup" aria-label="지난 기록">
                {HISTORY_CHOICES.map((choice) => (
                  <button
                    key={choice.value}
                    type="button"
                    role="radio"
                    aria-checked={history === choice.value}
                    className={`ui-radio-option ${history === choice.value ? "is-active" : ""}`}
                    onClick={() => setHistory(choice.value)}
                  >
                    <span className="ui-radio-dot" aria-hidden="true" />
                    <span><strong>{choice.label}</strong><small>{choice.detail}</small></span>
                  </button>
                ))}
              </div>
              <small className="ui-note">녹화 영상은 고른 것과 상관없이 7일이 지나면 자동으로 지워져요.</small>
            </div>
            <div className="ui-two-buttons">
              <button type="button" className="ui-button" onClick={() => setStep("form")} disabled={busy}>취소</button>
              <button type="button" className="ui-button is-danger"
                onClick={() => history && void register(history)} disabled={!history || busy}>
                {busy ? "등록 중" : "다시 등록하기"}
              </button>
            </div>
          </section>
        )}

        {step === "done" && (
          <section className="ui-card ui-register-done" role="status">
            <span className="ui-badge is-ok">등록 완료</span>
            <strong>우리 집 말벗의 소유자가 됐어요</strong>
            <p>함께 볼 사람이 있으면 설정 › 보호자 관리에서 초대 링크를 만들어 보내세요.</p>
            <Link className="ui-button is-strong ui-register-main" href="/">홈으로</Link>
          </section>
        )}

        <p className="ui-info ui-register-invite">보호자로 초대받으셨나요? 소유자에게 받은 초대 링크를 다시 열어 주세요.</p>
      </main>
    </div>
  );
}
