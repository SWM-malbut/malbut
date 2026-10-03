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
type KeyView = { configured: boolean; last4: string | null; robotHasCurrent: boolean; robotModel: string | null };

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
  const [key, setKey] = useState<KeyView | null>(null);
  const [editingKey, setEditingKey] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const [fallResponse, keyResponse] = await Promise.all([
      request(`${base}/fall-settings`, { cache: "no-store" }).catch(() => null),
      request(`${base}/fall-cloud-key`, { cache: "no-store" }).catch(() => null),
    ]);
    if (fallResponse?.ok) setFall(await fallResponse.json());
    if (keyResponse?.ok) setKey(await keyResponse.json());
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
      setMessage("저장했어요. 로봇이 적용하면 상태가 바뀌어요.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "설정을 저장하지 못했습니다.");
    } finally {
      await load();
      setBusy("");
    }
  };

  const saveKey = async (value: string | null) => {
    setBusy("key");
    setMessage("");
    try {
      const response = await request(`${base}/fall-cloud-key`, value === null
        ? { method: "DELETE", headers: { "content-type": "application/json" } }
        : { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ apiKey: value }) });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error ?? "키를 저장하지 못했습니다.");
      setKey(body);
      setEditingKey(false);
      setNewKey("");
      setMessage(value === null ? "키를 지웠어요. 로봇도 곧 키를 지워요." : "키를 저장했어요. 로봇에 곧 전해져요.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "키를 저장하지 못했습니다.");
    } finally { setBusy(""); }
  };

  const rows: Array<{ title: string; desc: string; checked: boolean; needsCamera: boolean; onChange: () => void; busy: boolean }> = [
    { title: "카메라 사용", desc: "끄면 실시간 보기·연속 녹화·영상 분석이 모두 멈춰요.", checked: cameraEnabled,
      needsCamera: false, busy: settingBusy, onChange: () => onUpdateSetting("cameraEnabled", !cameraEnabled) },
    { title: "연속 녹화", desc: "집 안 영상을 계속 저장해 7일 동안 다시 볼 수 있어요. 끄면 새 녹화만 멈춰요.",
      checked: recordingEnabled, needsCamera: true, busy: settingBusy,
      onChange: () => onUpdateSetting("monitoringEnabled", !recordingEnabled) },
    { title: "넘어짐 감지", desc: "로봇이 넘어짐을 살펴요. 끄면 녹화와 실시간 보기는 그대로예요.",
      checked: fall?.settings.enabled ?? false, needsCamera: true, busy: !fall || busy === "enabled",
      onChange: () => void updateFall("enabled", !fall?.settings.enabled) },
    { title: "클라우드 AI 확인 동의", desc: "넘어짐이 의심될 때, 그리고 사용자가 신고한 순간을 외부 AI로 보내 한 번 더 확인해요.",
      checked: fall?.settings.cloudConsent ?? false, needsCamera: true, busy: !fall || busy === "cloudConsent",
      onChange: () => void updateFall("cloudConsent", !fall?.settings.cloudConsent) },
    // Not in the mockup: the existing live microphone switch is kept so the feature does not disappear.
    { title: "로봇 마이크", desc: "실시간 보기에서 집 안의 소리를 보호자에게 전해요.", checked: microphoneEnabled,
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
                  {row.title === "넘어짐 감지" && fall?.receiptState === "waiting" && <small>저장됨 · 로봇 적용 확인 중</small>}
                  {row.title === "넘어짐 감지" && fall?.receiptState === "no_response" && <small>로봇이 아직 적용했다고 알려 오지 않았어요</small>}
                </span>
                <Switch checked={row.checked && !blocked} disabled={disabled} label={row.title} onChange={row.onChange} />
              </div>
            );
          })}
        </div>

        <div className="fall-card is-flat">
          <h2>클라우드 AI 키</h2>
          <div className="fall-hint">이 로봇의 모든 사용자가 함께 쓰는 키예요. AI 확인 비용이 이 키로 나가요. 로봇에 직접 넣지 않아도 서버가 전해 줘요.</div>
          <div className="fall-key-row">
            <span>{key?.configured ? `•••• •••• •••• ${key.last4}` : "등록된 키 없음"}</span>
            <span className={key?.configured ? "is-ok" : "is-muted"}>
              {!key ? "확인 중" : !key.configured ? "AI 확인을 쓰려면 키가 필요해요"
                : key.robotHasCurrent ? "로봇에 전달됨" : "로봇에 전달하는 중"}
            </span>
          </div>
          {isOwner && editingKey && (
            <>
              <label className="fall-field">새 키 입력
                <input type="password" autoComplete="off" placeholder="키를 붙여 넣으세요" value={newKey}
                  onChange={(e) => setNewKey(e.target.value)} />
              </label>
              <div className="fall-two-buttons">
                <button type="button" className="fall-button is-soft" onClick={() => { setEditingKey(false); setNewKey(""); }}>취소</button>
                <button type="button" className="fall-button is-blue" disabled={!newKey.trim() || busy === "key"}
                  onClick={() => void saveKey(newKey.trim())}>{busy === "key" ? "저장 중…" : "저장"}</button>
              </div>
            </>
          )}
          {isOwner && !editingKey && (
            <div className="fall-two-buttons">
              <button type="button" className="fall-button" onClick={() => setEditingKey(true)}>키 바꾸기</button>
              <button type="button" className="fall-button is-danger-line" disabled={!key?.configured || busy === "key"}
                onClick={() => { if (window.confirm("키를 지울까요? 로봇의 클라우드 AI 확인도 멈춰요.")) void saveKey(null); }}>키 지우기</button>
            </div>
          )}
          <div className="fall-hint">입력한 키는 다시 보여 주지 않고 끝 네 자리만 표시해요. 소유자만 바꿀 수 있어요.</div>
        </div>

        <div className="fall-card is-flat">
          <h2>저장된 영상</h2>
          <div className="fall-sub">녹화 영상은 7일 동안 보관되고, 그 뒤에는 자동으로 지워져요. &quot;연속 녹화&quot;를 끄면 앞으로의 저장만 멈추고, 이미 저장된 영상은 7일이 지나면 지워져요.</div>
        </div>
      </div>
    </div>
  );
}
