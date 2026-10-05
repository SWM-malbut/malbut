"use client";

import { useId, useState, type FormEvent } from "react";

const MAX = 20;

/** 이름 입력: 첫 로그인의 "어떻게 불러 드릴까요?"와 설정 › 이름 바꾸기가 같이 쓴다. */
export function DisplayNameForm({
  initialName,
  submitLabel,
  onSaved,
}: {
  initialName: string;
  submitLabel: string;
  onSaved: (name: string) => void;
}) {
  const inputId = useId();
  const [name, setName] = useState(initialName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const length = [...name.trim()].length;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || length < 1 || length > MAX) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/account", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ displayName: name }),
        credentials: "same-origin",
        cache: "no-store",
      });
      const payload = (await response.json().catch(() => ({}))) as { displayName?: unknown; error?: unknown };
      if (!response.ok || typeof payload.displayName !== "string") {
        throw new Error(typeof payload.error === "string" ? payload.error : "이름을 저장하지 못했어요.");
      }
      onSaved(payload.displayName);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이름을 저장하지 못했어요.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ui-name-form" onSubmit={(event) => void submit(event)}>
      <label className="ui-field" htmlFor={inputId}>
        <span>이름</span>
        <input
          id={inputId}
          value={name}
          onChange={(event) => setName(event.target.value)}
          autoComplete="nickname"
          maxLength={40}
          required
        />
      </label>
      <small className={`ui-name-hint ${length > MAX ? "is-error" : ""}`}>
        20자 이내 · 나중에 설정 › 이름 바꾸기에서 바꿀 수 있어요 {length > 0 && `(${length}/${MAX})`}
      </small>
      {error && <p className="ui-name-error" role="alert">{error}</p>}
      <button type="submit" className="ui-button is-strong ui-wide" disabled={busy || length < 1 || length > MAX}>
        {busy ? "저장 중" : submitLabel}
      </button>
    </form>
  );
}
