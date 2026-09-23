"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ShieldCheck } from "@phosphor-icons/react";
import type { FallSettingsView } from "../fall-settings-contract";

export function FallSettingsPanel({ deviceId, isOwner }: { deviceId: string; isOwner: boolean }) {
  const [view, setView] = useState<FallSettingsView | null>(null);
  const [error, setError] = useState("");
  const [saveMessage, setSaveMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(false);
  const saving = useRef(false);
  const generation = useRef(0);
  const reading = useRef<AbortController | null>(null);
  const writing = useRef<AbortController | null>(null);
  const url = `/api/devices/${encodeURIComponent(deviceId)}/fall-settings`;

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    let timedOut = false;
    // HTTP timeout, separate from the six-second robot receipt display.
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "낙상 설정을 불러오지 못했습니다.");
      if (mounted.current && current === generation.current) {
        setView(body as FallSettingsView);
        setError("");
      }
    } catch (caught) {
      if ((timedOut || !controller.signal.aborted) && mounted.current && current === generation.current) {
        setError(timedOut ? "설정 서버에서 응답이 오지 않습니다. 다시 확인 중입니다." :
          caught instanceof Error ? caught.message : "낙상 설정을 불러오지 못했습니다.");
      }
    } finally { clearTimeout(deadline); }
  }, [url]);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!saving.current) await refresh();
      if (active) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      active = false;
      mounted.current = false;
      clearTimeout(timer);
      reading.current?.abort();
      writing.current?.abort();
    };
  }, [refresh]);

  async function update(field: "enabled" | "cloudConsent", value: boolean) {
    if (!view || !isOwner || saving.current) return;
    saving.current = true;
    setBusy(true);
    setSaveMessage("");
    ++generation.current;
    reading.current?.abort();
    const controller = new AbortController();
    writing.current = controller;
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
    try {
      const response = await fetch(url, { method: "PATCH", signal: controller.signal,
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ expectedRevision: view.settings.settingsRevision, [field]: value }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "설정을 저장하지 못했습니다.");
      if (mounted.current) setSaveMessage(`설정 ${body.savedRevision}번을 저장했습니다. 로봇의 적용 회신은 별도로 확인합니다.`);
    } catch (caught) {
      if ((timedOut || !controller.signal.aborted) && mounted.current) {
        setSaveMessage(timedOut ? "저장 여부를 확인하지 못했습니다. 다시 불러온 설정을 확인해 주세요." :
          caught instanceof Error ? caught.message : "저장 여부를 확인하지 못했습니다. 다시 불러온 설정을 확인해 주세요.");
      }
    } finally {
      clearTimeout(deadline);
      if (mounted.current) await refresh();
      if (mounted.current) setBusy(false);
      saving.current = false;
    }
  }

  const disabled = !view || !isOwner || busy || !!error;
  return <section className="homecam-settings-card" aria-labelledby="fall-settings-title">
    <div className="settings-card-heading">
      <span className="settings-heading-icon" aria-hidden="true"><ShieldCheck size={21} /></span>
      <div><h2 id="fall-settings-title">낙상 감지</h2><p>영상 녹화 설정과 별도로 저장합니다. 소유자만 변경할 수 있습니다.</p></div>
    </div>
    <div className="homecam-setting-row">
      <div><strong>낙상 감지</strong><span>로봇 카메라에서 낙상이 의심되는 장면을 찾습니다.</span></div>
      <button type="button" role="switch" aria-label="낙상 감지" aria-checked={view?.settings.enabled ?? false}
        className={`homecam-switch ${view?.settings.enabled ? "is-on" : ""}`} disabled={disabled}
        onClick={() => void update("enabled", !view?.settings.enabled)}><span /></button>
    </div>
    <div className="homecam-setting-row">
      <div><strong>Cloud VLM 전송 동의</strong><span>낙상 확인을 위해 최근 5초의 이미지 최대 12장과 센서 요약을 외부 분석 서비스로 보냅니다.</span></div>
      <button type="button" role="switch" aria-label="Cloud VLM 전송 동의" aria-checked={view?.settings.cloudConsent ?? false}
        className={`homecam-switch ${view?.settings.cloudConsent ? "is-on" : ""}`} disabled={disabled}
        onClick={() => void update("cloudConsent", !view?.settings.cloudConsent)}><span /></button>
    </div>
    {error && <p role="alert">{error}</p>}
    {saveMessage && <p role="status">{saveMessage}</p>}
    {!view && !error && <p>설정을 불러오는 중입니다.</p>}
    {view && <>
      {!view.settings.cameraEnabled && <p>카메라가 꺼져 있습니다. 감지를 시작하려면 카메라를 켜 주세요.</p>}
      <p>서버에 저장된 설정: {view.settings.settingsRevision}번</p>
      <p role="status">{view.receiptState === "waiting" ? "저장됨 · 로봇 적용 회신 대기" :
        view.receiptState === "no_response" ? "회신 없음 · 저장 후 6초가 지났지만 해당 설정의 회신이 없습니다." :
          "적용 회신 이력이 있습니다. 현재 실행 중인 VLM의 회신인지는 아직 확인할 수 없습니다."}</p>
      {view.reports.length > 0 && <details><summary>이 설정의 회신 이력</summary><ul>
        {view.reports.map((report) => <li key={`${report.bridgeRuntimeId}:${report.managerRuntimeId}:${report.sequence}`}>
          {report.applied ? "적용했다고 회신" : "적용하지 못했다고 회신"} · {report.reasonCode}
          <br />서버 수신: {report.receivedAt} · VLM 실행 ID: {report.runtimeId}
        </li>)}
      </ul></details>}
      <p className="homecam-ios-note">설정 회신만으로 현재 감지 실행 여부를 알 수는 없습니다. 실행 상태의 웹 연결은 아직 준비 중입니다.</p>
    </>}
  </section>;
}
