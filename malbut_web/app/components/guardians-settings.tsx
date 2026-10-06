"use client";

import { useCallback, useEffect, useState } from "react";

export type FamilyMember = {
  id: string;
  name: string;
  role: "owner" | "family";
  provider: string | null;
  joinedAt: string;
  viaInvite: boolean;
};

type InviteLink = { path: string; expiresAt: string; joined: number };

const PROVIDER_NAME: Record<string, string> = { kakao: "카카오톡", naver: "네이버", google: "구글", email: "이메일" };

function joinedDay(value: string, now: number) {
  const day = new Date(value), today = new Date(now);
  if (Number.isNaN(day.getTime())) return "";
  return day.toDateString() === today.toDateString() ? "오늘" : `${day.getMonth() + 1}월 ${day.getDate()}일`;
}

/** "나 · 카카오톡", "구글 · 10월 3일 링크로 들어옴" (목업 Guardians). */
export function memberDetail(member: FamilyMember, myUserId: string | null, now = Date.now()) {
  const how = PROVIDER_NAME[member.provider ?? ""] ?? "";
  if (member.id === myUserId) return ["나", how].filter(Boolean).join(" · ");
  if (member.role === "owner") return how;
  const day = joinedDay(member.joinedAt, now);
  return [how, day && `${day} ${member.viaInvite ? "링크로 들어옴" : "초대됨"}`].filter(Boolean).join(" · ");
}

function remainingText(expiresAt: string, now: number) {
  const minutes = Math.max(0, Math.floor((Date.parse(expiresAt) - now) / 60_000));
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}시간 ${minutes % 60}분 남음` : `${minutes}분 남음`;
}

async function call(url: string, method: string) {
  const response = await fetch(url, {
    method,
    headers: method === "GET" ? undefined : { "content-type": "application/json" },
    body: method === "GET" ? undefined : "{}",
    credentials: "same-origin",
    cache: "no-store",
  });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) throw new Error(typeof payload.error === "string" ? payload.error : "요청을 처리하지 못했어요.");
  return payload;
}

const asInvite = (value: unknown): InviteLink | null => {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : null;
  return raw && typeof raw.path === "string" && typeof raw.expiresAt === "string"
    ? { path: raw.path, expiresAt: raw.expiresAt, joined: typeof raw.joined === "number" ? raw.joined : 0 }
    : null;
};

/** 설정 › 보호자 (목업 Guardians): 함께 보는 사람, 내보내기, 보호자 초대 링크. */
export function GuardiansSettings({ deviceId, isOwner, myUserId, family, loading, busy, onBack, onRemove, demo = false }: {
  deviceId: string;
  isOwner: boolean;
  myUserId: string | null;
  family: FamilyMember[];
  loading: boolean;
  busy: boolean;
  onBack: () => void;
  onRemove: (member: FamilyMember) => Promise<boolean>;
  demo?: boolean;
}) {
  const [confirm, setConfirm] = useState<FamilyMember | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [link, setLink] = useState<InviteLink | null>(null);
  const [linkBusy, setLinkBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const base = `/api/devices/${encodeURIComponent(deviceId)}/invite`;

  useEffect(() => {
    if (!isOwner || demo) return;
    let active = true;
    void call(base, "GET").then((payload) => { if (active) setLink(asInvite(payload.invite)); }).catch(() => undefined);
    return () => { active = false; };
  }, [base, demo, isOwner]);

  // The countdown, and the link goes away by itself once its 24 hours are up.
  useEffect(() => {
    if (!link) return;
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (Date.parse(link.expiresAt) <= current) setLink(null);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [link]);

  const url = link ? `${window.location.origin}${link.path}` : "";

  const act = useCallback(async (work: () => Promise<string>) => {
    setLinkBusy(true);
    setError("");
    setNotice("");
    try { setNotice(await work()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "요청을 처리하지 못했어요."); }
    finally { setLinkBusy(false); }
  }, []);

  const makeLink = () => act(async () => {
    const next = demo
      ? { path: "/invite/demo-link", expiresAt: new Date(Date.now() + 86_400_000).toISOString(), joined: 0 }
      : asInvite((await call(base, "POST")).invite);
    if (!next) throw new Error("초대 링크를 만들지 못했어요.");
    setNow(Date.now());
    setLink(next);
    return "";
  });

  const cancelLink = () => act(async () => {
    if (!demo) await call(base, "DELETE");
    setLink(null);
    return "링크를 취소했어요. 이제 이 링크로는 들어올 수 없어요.";
  });

  const copyLink = () => act(async () => {
    try { await navigator.clipboard.writeText(url); }
    catch { throw new Error("복사하지 못했어요. 링크를 길게 눌러 복사해 주세요."); }
    return "링크를 복사했어요.";
  });

  const shareLink = () => act(async () => {
    if (!navigator.share) {
      await navigator.clipboard.writeText(url).catch(() => undefined);
      return "공유하기를 쓸 수 없는 브라우저라 링크를 복사했어요.";
    }
    try { await navigator.share({ title: "말벗 보호자 초대", url }); }
    catch (reason) { if (reason instanceof Error && reason.name === "AbortError") return ""; throw reason; }
    return "";
  });

  const remove = async (member: FamilyMember) => {
    setConfirm(null);
    setNotice("");
    if (await onRemove(member)) setNotice(`${member.name} 님을 내보냈어요.`);
  };

  return (
    <section className="ui-screen ui-settings-sub" aria-label="보호자">
      <div className="ui-subhead">
        <button type="button" className="ui-back" onClick={onBack}>‹ 설정</button>
        <h1>보호자</h1>
        <span className="ui-subhead-note">{isOwner ? "소유자 화면 · 보호자는 목록만 볼 수 있어요" : "보호자는 목록만 볼 수 있어요"}</span>
      </div>
      {notice && <p className="ui-success" role="status">{notice}</p>}

      <article className="ui-card ui-people" aria-busy={loading}>
        <h2>함께 보는 사람</h2>
        {loading && family.length === 0 && <p className="ui-hint">보호자 목록을 불러오는 중이에요…</p>}
        {!loading && family.length === 0 && <p className="ui-hint">아직 함께 보는 보호자가 없어요.</p>}
        {family.map((member) => (
          <div key={member.id} className="ui-person">
            <span className="ui-person-avatar" aria-hidden="true">{[...member.name][0]?.toUpperCase()}</span>
            <span className="ui-person-text">
              <strong>{member.name} <span className={`ui-badge ${member.role === "owner" ? "is-accent" : ""}`}>{member.role === "owner" ? "소유자" : "보호자"}</span></strong>
              <small>{memberDetail(member, myUserId, now)}</small>
            </span>
            {isOwner && member.role !== "owner" && (
              <button type="button" className="ui-button is-danger-line ui-small"
                onClick={() => { setNotice(""); setConfirm(member); }} disabled={busy}>
                내보내기
              </button>
            )}
          </div>
        ))}
        {confirm && (
          <div className="ui-confirm">
            <span>{confirm.name} 님을 내보낼까요? 이 말벗의 영상과 사건을 더 볼 수 없어요.</span>
            <div className="ui-two-buttons">
              <button type="button" className="ui-button" onClick={() => setConfirm(null)}>취소</button>
              <button type="button" className="ui-button is-danger" onClick={() => void remove(confirm)}>내보내기</button>
            </div>
          </div>
        )}
      </article>

      {isOwner && (
        <article className="ui-card ui-invite-link">
          <h2>보호자 초대 링크</h2>
          <p className="ui-hint ui-long">링크를 받은 사람은 카카오톡·네이버·구글 중 아무 계정으로 로그인하면 이 말벗의 보호자로 등록돼요. 링크 하나로 여러 명이 들어올 수 있고, 만든 뒤 24시간 동안 쓸 수 있어요.</p>
          {!link ? (
            <button type="button" className="ui-button is-strong ui-invite-make" onClick={() => void makeLink()} disabled={linkBusy}>
              {linkBusy ? "만드는 중" : "초대 링크 만들기"}
            </button>
          ) : (
            <>
              <div className="ui-invite-url">{url}</div>
              <div className="ui-invite-meta"><span>{remainingText(link.expiresAt, now)}</span><span>이 링크로 {link.joined}명 들어옴</span></div>
              <div className="ui-two-buttons">
                <button type="button" className="ui-button is-emphasis" onClick={() => void copyLink()} disabled={linkBusy}>링크 복사</button>
                <button type="button" className="ui-button is-strong" onClick={() => void shareLink()} disabled={linkBusy}>공유하기</button>
              </div>
              <button type="button" className="ui-button is-danger-line ui-invite-cancel" onClick={() => void cancelLink()} disabled={linkBusy}>링크 취소</button>
              <small className="ui-note">단톡방 밖으로 링크가 퍼졌다면 바로 취소하세요. 이미 들어온 보호자는 그대로 남아요.</small>
            </>
          )}
          {error && <p className="ui-register-error" role="alert">{error}</p>}
        </article>
      )}
    </section>
  );
}

/** 설정 › 소유자 넘기기 (목업 Owner): 보호자 고르기 → 확인 → 넘김. */
export function OwnerTransferCard({ deviceId, family, onTransferred, demo = false }: {
  deviceId: string;
  family: FamilyMember[];
  onTransferred: (member: FamilyMember) => void;
  demo?: boolean;
}) {
  const [pick, setPick] = useState<FamilyMember | null>(null);
  const [step, setStep] = useState<"pick" | "confirm" | "done">("pick");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const guardians = family.filter((member) => member.role === "family");

  const transfer = async () => {
    if (!pick || busy) return;
    setBusy(true);
    setError("");
    try {
      if (!demo) {
        const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/owner`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ userId: pick.id }),
          credentials: "same-origin",
          cache: "no-store",
        });
        const payload = (await response.json().catch(() => ({}))) as { error?: unknown };
        if (!response.ok) throw new Error(typeof payload.error === "string" ? payload.error : "소유자를 넘기지 못했어요.");
      }
      setStep("done");
      onTransferred(pick);
    } catch (reason) {
      setStep("pick");
      setError(reason instanceof Error ? reason.message : "소유자를 넘기지 못했어요.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="ui-card ui-owner-transfer">
      <h2>소유자 넘기기</h2>
      <p className="ui-hint ui-long">보호자 중 한 명을 이 말벗의 소유자로 바꿔요. 넘기면 나는 보호자가 되고, 설정은 새 소유자만 바꿀 수 있어요.</p>
      {step === "pick" && (guardians.length === 0 ? (
        <p className="ui-hint ui-long">넘길 보호자가 없어요. 먼저 설정 › 보호자에서 초대 링크로 보호자를 초대해 주세요.</p>
      ) : (
        <>
          <div className="ui-radio-list" role="radiogroup" aria-label="새 소유자">
            {guardians.map((member) => (
              <button key={member.id} type="button" role="radio" aria-checked={pick?.id === member.id}
                className={`ui-radio-option ${pick?.id === member.id ? "is-active" : ""}`} onClick={() => setPick(member)}>
                <span className="ui-radio-dot" aria-hidden="true" />
                <span><strong>{member.name}</strong><small>{["보호자", PROVIDER_NAME[member.provider ?? ""]].filter(Boolean).join(" · ")}</small></span>
              </button>
            ))}
          </div>
          <button type="button" className="ui-button is-strong ui-owner-ask" disabled={!pick}
            onClick={() => { if (pick) setStep("confirm"); }}>
            소유자 넘기기
          </button>
        </>
      ))}
      {step === "confirm" && pick && (
        <div className="ui-confirm">
          <span>{pick.name} 님에게 소유자를 넘길까요? 넘긴 뒤에는 내가 설정을 바꿀 수 없어요.</span>
          <div className="ui-two-buttons">
            <button type="button" className="ui-button" onClick={() => setStep("pick")} disabled={busy}>취소</button>
            <button type="button" className="ui-button is-danger" onClick={() => void transfer()} disabled={busy}>
              {busy ? "넘기는 중" : "넘기기"}
            </button>
          </div>
        </div>
      )}
      {step === "done" && pick && (
        <p className="ui-success" role="status">{pick.name} 님이 이제 소유자예요. 나는 보호자로 남았어요.</p>
      )}
      {error && <p className="ui-register-error" role="alert">{error}</p>}
    </article>
  );
}
