"use client";

import { useCallback, useEffect, useState } from "react";
import { demoIncidentFetch } from "./fall-incidents-demo";

// 홈캠 설정 block from the reviewed mockup (Settings.dc.html). Only the owner
// changes settings; shared users see the current state.

type Request = (url: string, init?: RequestInit) => Promise<Response>;
type FallView = {
  settings: { settingsRevision: string; enabled: boolean; cameraEnabled: boolean; cloudConsent: boolean };
  receiptState: "waiting" | "no_response" | "reported";
};

function Switch({ checked, disabled, label, onChange }: {
  checked: boolean; disabled: boolean; label: string; onChange: () => void;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled}
      className={`fall-switch ${checked ? "is-on" : ""}`} onClick={onChange}><span /></button>
  );
}

export function FallHomecamSettings({ deviceId, isOwner, cameraEnabled, recordingEnabled, microphoneEnabled,
  settingBusy, onUpdateSetting, demo = false }: {
  deviceId: string;
  isOwner: boolean;
  cameraEnabled: boolean;
  recordingEnabled: boolean;
  microphoneEnabled: boolean;
  settingBusy: boolean;
  onUpdateSetting: (key: "cameraEnabled" | "monitoringEnabled" | "microphoneEnabled", value: boolean) => void;
  demo?: boolean;
}) {
  const base = `/api/devices/${encodeURIComponent(deviceId)}`;
  const request = useCallback<Request>((url, init) =>
    demo ? demoIncidentFetch(url, init) : fetch(url, init), [demo]);
  const [fall, setFall] = useState<FallView | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const fallResponse = await request(`${base}/fall-settings`, { cache: "no-store" }).catch(() => null);
    if (fallResponse?.ok) setFall(await fallResponse.json());
  }, [base, request]);

  useEffect(() => {
    window.queueMicrotask(() => void load());
    const timer = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const updateFall = async (field: "enabled" | "cloudConsent", value: boolean) => {
    if (!fall) return;
    setBusy(field);
    setMessage("");
    try {
      const response = await request(`${base}/fall-settings`, {
        method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ expectedRevision: fall.settings.settingsRevision, [field]: value }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error ?? "설정을 저장하지 못했습니다.");
      setMessage("저장했어요. 말벗이 적용하면 상태가 바뀌어요.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "설정을 저장하지 못했습니다.");
    } finally {
      await load();
      setBusy("");
    }
  };

  const rows: Array<{ title: string; desc: string; checked: boolean; needsCamera: boolean; onChange: () => void; busy: boolean }> = [
    { title: "카메라 사용", desc: "끄면 실시간 보기·연속 녹화·영상 분석이 모두 멈춰요.", checked: cameraEnabled,
      needsCamera: false, busy: settingBusy, onChange: () => onUpdateSetting("cameraEnabled", !cameraEnabled) },
    { title: "연속 녹화", desc: "집 안 영상을 계속 저장해 7일 동안 다시 볼 수 있어요. 끄면 새 녹화만 멈춰요.",
      checked: recordingEnabled, needsCamera: true, busy: settingBusy,
      onChange: () => onUpdateSetting("monitoringEnabled", !recordingEnabled) },
    { title: "낙상 감지", desc: "말벗이 낙상을 살펴요. 끄면 녹화와 실시간 보기는 그대로예요.",
      checked: fall?.settings.enabled ?? false, needsCamera: true, busy: !fall || busy === "enabled",
      onChange: () => void updateFall("enabled", !fall?.settings.enabled) },
    { title: "클라우드 AI 확인 동의", desc: "낙상이 의심될 때, 그리고 사용자가 신고한 순간을 외부 AI로 보내 한 번 더 확인해요.",
      checked: fall?.settings.cloudConsent ?? false, needsCamera: true, busy: !fall || busy === "cloudConsent",
      onChange: () => void updateFall("cloudConsent", !fall?.settings.cloudConsent) },
    // Not in the mockup: the existing live microphone switch is kept so the feature does not disappear.
    { title: "말벗 마이크", desc: "실시간 보기에서 집 안의 소리를 보호자에게 전해요.", checked: microphoneEnabled,
      needsCamera: false, busy: settingBusy, onChange: () => onUpdateSetting("microphoneEnabled", !microphoneEnabled) },
  ];

  return (
    <div className="fall-page fall-settings">
      <div className="fall-list-head"><div><h2>홈캠 설정</h2></div></div>
      <div className="fall-tl-body">
        {!isOwner && <div className="fall-settings-banner">설정은 소유자만 바꿀 수 있어요. 지금 상태만 보여요.</div>}
        {message && <p className="fall-notice" role="status">{message}</p>}

        <div className="fall-card fall-settings-list">
          {rows.map((row) => {
            const blocked = row.needsCamera && !cameraEnabled;
            const disabled = !isOwner || blocked || row.busy;
            return (
              <div key={row.title} className={`fall-settings-row ${!isOwner || blocked ? "is-dim" : ""}`}>
                <span>
                  <strong>{row.title}</strong>
                  <small>{blocked ? "카메라가 꺼져 있어 동작하지 않아요." : row.desc}</small>
                  {row.title === "낙상 감지" && fall?.receiptState === "waiting" && <small>저장됨 · 말벗 적용 확인 중</small>}
                  {row.title === "낙상 감지" && fall?.receiptState === "no_response" && <small>말벗이 아직 적용했다고 알려 오지 않았어요</small>}
                </span>
                <Switch checked={row.checked && !blocked} disabled={disabled} label={row.title} onChange={row.onChange} />
              </div>
            );
          })}
        </div>

        <div className="fall-card is-flat">
          <h2>저장된 영상</h2>
          <div className="fall-sub">녹화 영상은 7일 동안 보관되고, 그 뒤에는 자동으로 지워져요. &quot;연속 녹화&quot;를 끄면 앞으로의 저장만 멈추고, 이미 저장된 영상은 7일이 지나면 지워져요.</div>
        </div>
      </div>
    </div>
  );
}
