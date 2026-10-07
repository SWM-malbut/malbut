"use client";

import { useCallback, useEffect, useState } from "react";
import type { VoiceHistory } from "../voice-agent-contract";

const operationNames: Record<string, string> = { homecam_status: "홈캠 상태", homecam_events: "감지 기록",
  homecam_recordings: "저장 영상", homecam_falls: "낙상 기록", homecam_settings: "홈캠 설정", result_publish: "음성 작업" };
const stateNames: Record<string, string> = { pending: "처리 대기", completed: "처리 완료", failed: "실패",
  accepted: "접수", running: "실행 중", succeeded: "완료", canceled: "취소", unknown: "결과 확인 필요" };
type View = { delegation: { enabled: boolean }; requests: VoiceHistory[] };

export function VoiceAgentPanel({ deviceId, isOwner = false, settings = false }: {
  deviceId: string; isOwner?: boolean; settings?: boolean;
}) {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const url = `/api/devices/${encodeURIComponent(deviceId)}/voice-agent`;
  const refresh = useCallback(async (signal: AbortSignal) => {
    try {
      const response = await fetch(url, { cache: "no-store", signal });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "음성 요청을 불러오지 못했습니다.");
      if (!signal.aborted) { setView(body); setError(""); }
    } catch (caught) {
      if (!signal.aborted) setError(caught instanceof Error ? caught.message : "음성 요청을 불러오지 못했습니다.");
    }
  }, [url]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);

  async function update(enabled: boolean) {
    if (!isOwner || busy) return;
    setBusy(true);
    try {
      const response = await fetch(url, { method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "음성 권한을 저장하지 못했습니다.");
      setView((current) => current ? { ...current, delegation: body.delegation } : current);
      setError("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "음성 권한을 저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className={settings ? "homecam-settings-card" : "robot-map-panel-card"}>
    <h3>{settings ? "음성으로 홈캠 사용" : "음성 요청·결과"}</h3>
    {settings ? <>
      <p>로봇 가까이에서 말한 사람이 홈캠 상태·기록을 확인하고 카메라·홈캠 마이크·모니터링·낙상 감지를 켜거나 끌 수 있게 합니다.</p>
      <label><input type="checkbox" role="switch" checked={view?.delegation.enabled ?? false}
        disabled={!view || !isOwner || busy} onChange={(event) => void update(event.target.checked)} /> 음성 홈캠 사용 허용</label>
      <p>소유자가 한 번 허용하면 유지됩니다. 해제하면 대기 중인 홈캠 요청도 취소됩니다. 가족 권한과 Cloud 분석 동의는 웹에서만 변경합니다.</p>
      <small>홈캠 마이크는 보호자에게 보내는 소리입니다. 로봇의 음성 대기는 유지됩니다.</small>
    </> : <>
      {view && !view.requests.length && <p>아직 음성 요청 기록이 없습니다.</p>}
      {view?.requests.map((item) => <VoiceResult key={item.requestId} item={item} />)}
      <small>접수·설정 저장과 로봇 실행·적용은 다릅니다. 낙상 설정의 적용 회신은 설정 화면에서 확인하세요.</small>
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

function VoiceResult({ item }: { item: VoiceHistory }) {
  const result = item.reply?.result ?? {};
  const entries = [result.events, result.recordings, result.incidents].find(Array.isArray) as Array<Record<string, unknown>> | undefined;
  return <article id={`voice-result-${item.requestId}`}>
    <p><strong>{typeof result.title === "string" ? result.title : operationNames[item.operation]} · {stateNames[String(result.state ?? item.state)] ?? item.state}</strong><br />
      <small>{new Date(item.createdAt).toLocaleString("ko-KR")}</small><br />
      {typeof result.summary === "string" ? result.summary : item.reply?.message ?? "처리 결과를 기다리고 있습니다."}</p>
    {item.operation === "homecam_status" && item.reply?.success && <p>
      저장된 설정: 카메라 {result.cameraEnabled ? "켜짐" : "꺼짐"} · 홈캠 마이크 {result.microphoneEnabled ? "켜짐" : "꺼짐"} ·
      모니터링 {result.monitoringEnabled ? "켜짐" : "꺼짐"} · 낙상 감지 {result.fallEnabled ? "켜짐" : "꺼짐"}
    </p>}
    {item.operation === "homecam_status" && item.reply?.success && <>
      <ApplyReceipt label="홈캠" value={result.mediaApplyReceipt} />
      <ApplyReceipt label="낙상" value={result.fallApplyReceipt} />
    </>}
    {entries && (entries.length ? <ul>{entries.map((entry) => <li key={String(entry.id)}>
      {String(entry.eventType ?? entry.state ?? "저장 영상")} · {String(entry.occurredAt ?? entry.startedAt ?? "")}
      {referenceHref(entry.href) && <> · <a href={entry.href}>기록 열기</a></>}
      <small> · {String(entry.id)}</small>
    </li>)}</ul> : <p>해당 기록이 없습니다.</p>)}
    {referenceHref(result.referenceHref) && <a href={result.referenceHref}>관련 기록 열기</a>}
    {typeof result.referenceId === "string" && <small> 기록 ID: {result.referenceId}</small>}
    {result.saved === true && <p>설정 {String(result.savedRevision)}번 저장 · 적용 회신 대기</p>}
  </article>;
}

function referenceHref(value: unknown): value is string {
  return typeof value === "string" && ["/?device=", "/voice-results/", "/api/devices/"].some((prefix) => value.startsWith(prefix));
}

function ApplyReceipt({ label, value }: { label: string; value: unknown }) {
  if (!value || typeof value !== "object") return null;
  const receipt = value as Record<string, unknown>;
  const names: Record<string, string> = { reported_applied: "로봇 적용 회신", reported_failed: "로봇 적용 실패 회신",
    waiting: "적용 회신 대기", no_response: "적용 회신 없음" };
  return <p>{label}: {names[String(receipt.state)] ?? "적용 확인 필요"}
    {typeof receipt.receivedAt === "string" && <> · {new Date(receipt.receivedAt).toLocaleString("ko-KR")}
      {receipt.fresh === false ? " · 오래된 회신" : ""} · 현재 실행 여부는 별도 확인</>}
  </p>;
}
