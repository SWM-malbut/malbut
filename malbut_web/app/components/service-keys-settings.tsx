"use client";

import { useCallback, useEffect, useState } from "react";

type Service = "openai" | "kma" | "fall";
type KeyView = {
  configured: boolean;
  last4: string | null;
  keyVersion: number;
  updatedAt: string | null;
  robotHasCurrent: boolean;
};
type Tone = "gray" | "ok" | "info" | "bad";

const CARDS: Array<{
  service: Service; title: string; desc: string; placeholder: string;
  link: { label: string; href: string }; without: string; note?: string;
}> = [
  {
    service: "openai", title: "대화 · OpenAI", desc: "말벗이 알아듣고 대답하고, 목소리를 낼 때 써요.",
    placeholder: "sk-로 시작하는 키", link: { label: "OpenAI에서 키 만들기 ›", href: "https://platform.openai.com/api-keys" },
    without: "말벗이 지금 대화를 할 수 없어요. 말을 걸면 대화를 할 수 없다고 안내해요.",
  },
  {
    service: "kma", title: "날씨 · 기상청",
    desc: "말벗이 날씨를 알려 줄 때 써요. 공공데이터포털에서 무료로 받을 수 있어요. 새로 받은 키는 쓸 수 있게 되기까지 시간이 걸릴 수 있어요. 그 전에는 저장되지 않아요.",
    placeholder: "공공데이터포털 일반 인증키", link: { label: "공공데이터포털에서 키 받기 ›", href: "https://www.data.go.kr/data/15084084/openapi.do" },
    without: "날씨를 물으면 확인할 수 없다고 말해요. 대화는 그대로 해요.",
  },
  {
    service: "fall", title: "낙상 AI 확인 · Ollama", desc: "낙상이 의심될 때 장면을 AI에게 한 번 더 보여 줘요.",
    placeholder: "Ollama 키", link: { label: "Ollama에서 키 만들기 ›", href: "https://ollama.com/settings/keys" },
    without: "낙상 감지와 알림은 계속하지만, AI 재확인이 빠져 정확도가 떨어질 수 있어요.",
    note: "AI 확인을 쓸지는 설정 › 홈캠 설정의 \"클라우드 AI 확인 동의\"에서 정해요.",
  },
];

const EMPTY: KeyView = { configured: false, last4: null, keyVersion: 0, updatedAt: null, robotHasCurrent: false };

function day(value: string | null) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? `${date.getMonth() + 1}월 ${date.getDate()}일` : "";
}

const asView = (value: unknown): KeyView => {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  return {
    configured: raw.configured === true,
    last4: typeof raw.last4 === "string" ? raw.last4 : null,
    keyVersion: typeof raw.keyVersion === "number" ? raw.keyVersion : 0,
    updatedAt: typeof raw.updatedAt === "string" ? raw.updatedAt : null,
    robotHasCurrent: raw.robotHasCurrent === true,
  };
};

/** 설정 › AI·서비스 키 (목업 13, 소유자만): 대화·날씨·낙상 AI 키를 넣고 바꾸고 지운다. */
export function ServiceKeysSettings({ deviceId, onBack, demo = false }: {
  deviceId: string;
  onBack: () => void;
  demo?: boolean;
}) {
  const base = `/api/devices/${encodeURIComponent(deviceId)}/service-keys`;
  const [views, setViews] = useState<Record<Service, KeyView> | null>(
    demo ? { openai: EMPTY, kma: EMPTY, fall: EMPTY } : null);
  const [loadError, setLoadError] = useState("");
  const [drafts, setDrafts] = useState<Record<Service, string>>({ openai: "", kma: "", fall: "" });
  const [editing, setEditing] = useState<Service | null>(null);
  const [checking, setChecking] = useState<Service | null>(null);
  const [errors, setErrors] = useState<Partial<Record<Service, string>>>({});
  const [confirmDelete, setConfirmDelete] = useState<Service | null>(null);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (demo) return;
    try {
      const response = await fetch(base, { cache: "no-store", credentials: "same-origin" });
      const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
      if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : "키를 불러오지 못했어요.");
      setViews({ openai: asView(body.openai), kma: asView(body.kma), fall: asView(body.fall) });
      setLoadError("");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "키를 불러오지 못했어요.");
    }
  }, [base, demo]);

  // "반영 중" turns into "말벗에 반영됨" once the robot has fetched the key (about a minute).
  useEffect(() => {
    window.queueMicrotask(() => void load());
    const timer = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const send = async (service: Service, apiKey: string | null) => {
    if (demo) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      if (apiKey !== null && /bad/i.test(apiKey)) {
        return { ok: false, error: "쓸 수 없는 키예요. 저장하지 않았어요. 키를 다시 확인해 주세요." };
      }
      return { ok: true, view: apiKey === null
        ? { ...EMPTY, keyVersion: (views?.[service].keyVersion ?? 0) + 1 }
        : { configured: true, last4: apiKey.slice(-4), keyVersion: (views?.[service].keyVersion ?? 0) + 1,
          updatedAt: new Date().toISOString(), robotHasCurrent: false } };
    }
    const response = await fetch(`${base}/${service}`, {
      method: apiKey === null ? "DELETE" : "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(apiKey === null ? {} : { apiKey }),
      credentials: "same-origin",
      cache: "no-store",
    });
    const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) return { ok: false, error: typeof body.error === "string" ? body.error : "키를 저장하지 못했어요." };
    return { ok: true, view: asView(body) };
  };

  const save = async (service: Service) => {
    const apiKey = drafts[service].trim();
    if (checking) return;
    if (!apiKey) {
      setErrors((current) => ({ ...current, [service]: "키를 붙여 넣어 주세요." }));
      return;
    }
    setChecking(service);
    setErrors((current) => ({ ...current, [service]: undefined }));
    setNotice("");
    try {
      const result = await send(service, apiKey);
      if (!result.ok || !result.view) {
        setErrors((current) => ({ ...current, [service]: result.error }));
        return;
      }
      setViews((current) => current && { ...current, [service]: result.view });
      setDrafts((current) => ({ ...current, [service]: "" }));
      setEditing(null);
      setNotice("저장했어요. 말벗에 1분 안에 반영돼요.");
    } catch {
      setErrors((current) => ({ ...current, [service]: "키를 저장하지 못했어요. 인터넷 연결을 확인해 주세요." }));
    } finally {
      setChecking(null);
    }
  };

  const remove = async (service: Service) => {
    setConfirmDelete(null);
    setNotice("");
    try {
      const result = await send(service, null);
      if (!result.ok || !result.view) throw new Error(result.error);
      setViews((current) => current && { ...current, [service]: result.view });
      setNotice("키를 지웠어요. 말벗도 1분 안에 이 키를 지워요.");
    } catch (reason) {
      setErrors((current) => ({ ...current, [service]: reason instanceof Error ? reason.message : "키를 지우지 못했어요." }));
    }
  };

  return (
    <section className="ui-screen ui-settings-sub" aria-label="AI·서비스 키">
      <div className="ui-subhead">
        <button type="button" className="ui-back" onClick={onBack}>‹ 설정</button>
        <h1>AI·서비스 키</h1>
        <span className="ui-subhead-note">소유자 화면 · 보호자에게는 보이지 않아요</span>
      </div>
      <p className="ui-info ui-long">말벗이 대화하고, 날씨를 알려 주고, 낙상을 한 번 더 확인할 때 쓰는 키예요. 요금은 키를 만든 계정으로 나가요. 여기 넣은 키는 이 말벗에만 쓰여요.</p>
      {notice && <p className="ui-success" role="status">{notice}</p>}
      {loadError && <p className="ui-register-error" role="alert">{loadError}</p>}
      {!views && !loadError && <p className="ui-hint">키를 불러오는 중이에요…</p>}

      {views && CARDS.map((card) => {
        const view = views[card.service];
        const managed = view.keyVersion > 0;
        const isChecking = checking === card.service;
        const error = errors[card.service];
        let badge = "팀 키 사용 중", badgeTone: Tone = "gray", state = "", tone: Tone = "gray";
        let showInput = false, showActions = false;
        if (isChecking) {
          state = "저장하기 전에 이 키를 쓸 수 있는지 확인하고 있어요."; tone = "info"; showInput = true;
          if (view.configured) { badge = "사용 중"; badgeTone = "ok"; }
        } else if (!managed) {
          state = "아직 키를 넣지 않아서 말벗에 미리 들어 있는 팀 키를 쓰고 있어요."; showInput = true;
        } else if (view.configured) {
          badge = "사용 중"; badgeTone = "ok"; showActions = editing !== card.service; showInput = editing === card.service;
          state = view.robotHasCurrent
            ? `끝 네 자리 ${view.last4} · ${day(view.updatedAt)} 저장 · 말벗에 반영됨`
            : `저장했어요. 말벗에 1분 안에 반영돼요. · 끝 네 자리 ${view.last4}`;
          tone = view.robotHasCurrent ? "ok" : "info";
        } else {
          badge = "키 없음"; badgeTone = "bad"; state = `${card.without} 키를 넣어 주세요.`; tone = "bad"; showInput = true;
        }
        if (error && !isChecking) showInput = true;
        const inputId = `service-key-${card.service}`;
        return (
          <article key={card.service} className="ui-card ui-key-card">
            <div className="ui-card-head">
              <h2>{card.title}</h2>
              <span className={`ui-badge ${badgeTone === "ok" ? "is-ok" : badgeTone === "bad" ? "is-danger" : ""}`}>{badge}</span>
            </div>
            <p className="ui-hint ui-long">{card.desc}</p>
            {state && <p className={`ui-key-state is-${tone}`}>{state}</p>}
            {showInput && (
              <>
                <label className="ui-field" htmlFor={inputId}>
                  <span>{managed && view.configured ? "새 키" : "키 넣기"}</span>
                  <input id={inputId} type="password" autoComplete="off" spellCheck={false}
                    placeholder={card.placeholder} value={drafts[card.service]} disabled={isChecking}
                    aria-invalid={Boolean(error)}
                    onChange={(event) => setDrafts((current) => ({ ...current, [card.service]: event.target.value }))} />
                </label>
                {error && <span className="ui-register-error" role="alert">{error}</span>}
                <div className={editing === card.service ? "ui-two-buttons" : ""}>
                  {editing === card.service && (
                    <button type="button" className="ui-button" disabled={isChecking}
                      onClick={() => { setEditing(null); setErrors((current) => ({ ...current, [card.service]: undefined })); }}>
                      취소
                    </button>
                  )}
                  <button type="button" className="ui-button is-strong ui-wide" disabled={isChecking}
                    onClick={() => void save(card.service)}>
                    {isChecking ? "확인하는 중…" : "확인하고 저장"}
                  </button>
                </div>
              </>
            )}
            {showActions && (
              <div className="ui-two-buttons">
                <button type="button" className="ui-button is-emphasis" onClick={() => { setNotice(""); setEditing(card.service); }}>바꾸기</button>
                <button type="button" className="ui-button is-danger-line" onClick={() => { setNotice(""); setConfirmDelete(card.service); }}>지우기</button>
              </div>
            )}
            {confirmDelete === card.service && (
              <div className="ui-confirm">
                <span>이 키를 지울까요? {card.without} 팀 키로 돌아가지 않아요.</span>
                <div className="ui-two-buttons">
                  <button type="button" className="ui-button" onClick={() => setConfirmDelete(null)}>취소</button>
                  <button type="button" className="ui-button is-danger" onClick={() => void remove(card.service)}>지우기</button>
                </div>
              </div>
            )}
            <a className="ui-key-link" href={card.link.href} target="_blank" rel="noopener noreferrer">{card.link.label}</a>
            {card.note && <small className="ui-note">{card.note}</small>}
          </article>
        );
      })}
    </section>
  );
}
