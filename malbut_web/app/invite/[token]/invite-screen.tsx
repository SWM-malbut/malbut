"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { SocialProvider } from "../../../db/web-auth";
import { SocialProviderButtons } from "../../auth/login/social-login-panel";
import { subscribeFallPush } from "../../lib/fall-push";

export type InviteDemo = "login" | "done" | "already" | "owner" | "expired";
type Result = "done" | "already" | "owner" | "expired";

const RESULT_OF: Record<string, Result> = {
  joined: "done", already_family: "already", owner: "owner", unusable: "expired",
};

/** 보호자 초대 (목업 Invite): 로그인 전 → (로그인하면 바로) 등록 완료 / 이미 보호자 / 쓸 수 없는 링크. */
export function InviteScreen({ token, returnTo, signedIn, invite, enabled, demo }: {
  token: string;
  returnTo: string;
  signedIn: boolean;
  invite: { ownerName: string | null; deviceName: string } | null;
  enabled: readonly SocialProvider[];
  demo?: InviteDemo;
}) {
  const demoResult = demo && demo !== "login" ? demo : null;
  const [result, setResult] = useState<Result | null>(!invite ? "expired" : demoResult);
  const [deviceId, setDeviceId] = useState(demo ? "demo" : "");
  const [error, setError] = useState("");
  const [push, setPush] = useState<"idle" | "busy" | "on">("idle");
  const [pushError, setPushError] = useState("");
  const sent = useRef(false);

  const accept = async () => {
    setError("");
    try {
      const response = await fetch("/api/invites/accept", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token }),
        credentials: "same-origin",
        cache: "no-store",
      });
      const payload = (await response.json().catch(() => ({}))) as { status?: unknown; deviceId?: unknown; error?: unknown };
      const next = typeof payload.status === "string" ? RESULT_OF[payload.status] : undefined;
      if (!next) throw new Error(typeof payload.error === "string" ? payload.error : "초대를 확인하지 못했어요.");
      if (typeof payload.deviceId === "string") setDeviceId(payload.deviceId);
      setResult(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "초대를 확인하지 못했어요.");
    }
  };

  // Signed in through the link: accept once, without another button.
  useEffect(() => {
    if (!signedIn || !invite || demo || sent.current) return;
    sent.current = true;
    window.queueMicrotask(() => void accept());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const enablePush = async () => {
    if (push !== "idle" || !deviceId) return;
    setPush("busy");
    setPushError("");
    try {
      if (!demo) await subscribeFallPush(deviceId);
      setPush("on");
    } catch (reason) {
      setPush("idle");
      setPushError(reason instanceof Error ? reason.message : "알림을 켜지 못했어요.");
    }
  };

  const who = invite?.ownerName ? `${invite.ownerName} 님이 ` : "";

  return (
    <div className="ui-register">
      <header className="ui-register-top">
        <h1>보호자 초대</h1>
      </header>
      <main className="ui-register-body">
        {invite && !signedIn && (
          <>
            <section className="ui-card ui-invite-card">
              <strong>{who}&apos;{invite.deviceName}&apos;의 보호자로 초대했어요</strong>
              <p>로그인하면 바로 보호자로 등록돼요. 실시간 영상과 사건을 함께 보고, 낙상 알림을 받을 수 있어요.</p>
            </section>
            <SocialProviderButtons returnTo={returnTo} enabled={enabled} />
            <p className="ui-invite-note">이미 말벗을 쓰고 계시면 처음 가입한 방법으로 로그인하세요.</p>
          </>
        )}

        {invite && signedIn && !result && (
          error ? (
            <section className="ui-card ui-invite-card" role="alert">
              <strong>초대를 확인하지 못했어요</strong>
              <p>{error}</p>
              <button type="button" className="ui-button is-strong" onClick={() => void accept()}>다시 시도</button>
            </section>
          ) : (
            <div className="ui-loading" role="status"><span aria-hidden="true" />초대를 확인하고 있어요.</div>
          )
        )}

        {result === "done" && (
          <section className="ui-card ui-register-done" role="status">
            <span className="ui-badge is-ok">등록 완료</span>
            <strong>보호자로 등록됐어요</strong>
            <p>이제 {invite?.deviceName ?? "말벗"}의 실시간 영상과 사건을 볼 수 있어요. 설정은 소유자만 바꿀 수 있어요.</p>
            <button type="button" className="ui-button is-emphasis" onClick={() => void enablePush()}
              disabled={push !== "idle"}>
              {push === "on" ? "이 휴대폰에서 낙상 알림을 받아요" : push === "busy" ? "알림 켜는 중" : "이 휴대폰에서 낙상 알림 받기"}
            </button>
            {pushError && <span className="ui-register-error" role="alert">{pushError}</span>}
            <Link className="ui-button is-strong ui-register-main" href="/">홈으로</Link>
          </section>
        )}

        {(result === "already" || result === "owner") && (
          <section className="ui-card ui-invite-card">
            <strong>{result === "owner" ? "이미 이 말벗의 소유자예요" : "이미 이 말벗의 보호자예요"}</strong>
            <p>다시 등록할 필요 없어요.</p>
            <Link className="ui-button is-strong ui-register-main" href="/">홈으로</Link>
          </section>
        )}

        {result === "expired" && (
          <section className="ui-card ui-invite-unusable">
            <span className="ui-badge is-warn">쓸 수 없는 링크</span>
            <strong>초대 링크를 쓸 수 없어요</strong>
            <p>링크는 만든 뒤 24시간 동안만 쓸 수 있고, 소유자가 취소했을 수도 있어요. 소유자에게 새 링크를 요청해 주세요.</p>
          </section>
        )}
      </main>
    </div>
  );
}
