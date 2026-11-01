"use client";

import { useCallback, useEffect, useState } from "react";
import type { CompanionCategory, CompanionView } from "../companion-contract";

export function CompanionSettings({ deviceId, onBack }: { deviceId: string; onBack: () => void }) {
  const [view, setView] = useState<CompanionView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState({ session: "", self: false, alone: false });
  const self = checks.session === view?.sessionId && checks.self;
  const alone = checks.session === view?.sessionId && checks.alone;
  const [erase, setErase] = useState(false);
  const url = `/api/devices/${encodeURIComponent(deviceId)}/companion`;
  const refresh = useCallback(async (signal: AbortSignal) => {
    try {
      const response = await fetch(url, { cache: "no-store", signal });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "기억 설정을 불러오지 못했어요.");
      if (!signal.aborted) { setView(body); setError(""); }
    } catch (caught) {
      if (!signal.aborted) { setView(null); setError(caught instanceof Error ? caught.message : "연결을 확인해 주세요."); }
    }
  }, [url]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await refresh(controller.signal); if (!controller.signal.aborted) timer = setTimeout(poll, 2000); };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);
  async function change(payload: Record<string, unknown>) {
    if (!view || busy) return;
    setBusy(true);
    try {
      const response = await fetch(url, { method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...payload, expectedRevision: view.revision }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "설정을 저장하지 못했어요.");
      setView(body); setChecks({ session: "", self: false, alone: false }); setErase(false); setError("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "설정을 저장하지 못했어요."); }
    finally { setBusy(false); }
  }
  function consent(category: CompanionCategory, enabled: boolean) {
    if (!view) return;
    const categories = category === "daily" ? (enabled ? ["daily"] : [])
      : enabled ? [...view.categories, category] : view.categories.filter((c) => c !== category);
    void change({ action: "consent", categories, externalProcessing: categories.length > 0 });
  }
  return <section className="ui-screen ui-settings-sub" aria-label="일상 기억과 본인 확인">
    <button type="button" onClick={onBack}>설정으로 돌아가기</button>
    <h2>일상 기억과 본인 확인</h2>
    <article className="ui-card ui-setting">
      <h3>기억할 이야기</h3>
      <p>동의한 일상 이야기는 기존 대화 AI로 요약하고, 짧은 요약만 로봇에 3개월 보관해요.
        사진·음성·대화 원문은 이 일상 기억에 저장하지 않아요. 건강과 가족 이야기는 각각 선택해 주세요.</p>
      <p>동의를 끄면 해당 요약도 삭제해요. 로봇이 연결되어야 적용되며, 이 설정은 기존 일반 음성 명령의 대화 기록과 별개예요.</p>
      {([["daily", "일상 이야기 요약·외부 AI 처리에 동의"], ["health", "건강 이야기 기억"], ["family", "가족 이야기 기억"]] as const).map(([category, label]) =>
        <label className="ui-setting-row" key={category}>
          <span>{label}</span>
          <input type="checkbox" checked={view?.categories.includes(category) ?? false}
            disabled={!view || busy || category !== "daily" && !view.categories.includes("daily")}
            onChange={(event) => consent(category, event.target.checked)} />
        </label>)}
      {view && <p role="status">{view.online && view.appliedRevision === view.revision
        ? "로봇에서 설정 적용을 확인했어요." : "설정을 저장했어요. 로봇 적용을 기다리고 있어요."}</p>}
    </article>
    <article className="ui-card ui-setting">
      <h3>지금 대화 상대 확인</h3>
      <p>로봇이 먼저 말을 걸면 현재 대화만 확인할 수 있어요. 확인은 최대 5분이며,
        대화 종료·자리 이탈·다른 사람 감지 시 해제돼요. 손님이 함께 있으면 확인하지 마세요.</p>
      <p>이 계정의 본인 기억을 사용해요. 보호자가 대신 접속한 상태라면 본인으로 확인하지 마세요.</p>
      <p role="status">{view?.confirmed ? "현재 대화 상대를 확인했어요." : view?.sessionId
        ? "로봇이 대화 중이에요. 아래 항목을 확인해 주세요." : "확인할 대화가 없어요."}</p>
      <label><input type="checkbox" checked={self} onChange={(e) => setChecks({ session: view?.sessionId ?? "", self: e.target.checked, alone })} />지금 로봇과 대화하는 사람은 나예요.</label><br />
      <label><input type="checkbox" checked={alone} onChange={(e) => setChecks({ session: view?.sessionId ?? "", self, alone: e.target.checked })} />지금 혼자 있고 개인 기억을 사용해도 돼요.</label><br />
      <button type="button" disabled={busy || !self || !alone || !view?.sessionId || !view.categories.includes("daily")}
        onClick={() => void change({ action: "confirm", sessionId: view?.sessionId, self: true, alone: true })}>현재 대화 확인</button>
      <button type="button" disabled={busy || !view?.confirmed} onClick={() => void change({ action: "revoke" })}>확인 해제</button>
    </article>
    <article className="ui-card ui-setting">
      <h3>일상 기억 삭제</h3>
      <p>현재 계정의 일상 요약을 모두 삭제해요. 삭제 후 복원할 수 없어요. 순찰에서 본 물체 정보는 유지돼요.</p>
      <label><input type="checkbox" checked={erase} onChange={(e) => setErase(e.target.checked)} />모든 일상 요약 삭제를 확인했어요.</label>
      <button type="button" disabled={busy || !view || !erase} onClick={() => void change({ action: "erase" })}>일상 요약 모두 삭제</button>
    </article>
    {error && <p role="alert">{error}</p>}
  </section>;
}
