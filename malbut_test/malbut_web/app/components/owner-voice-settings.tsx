"use client";

import { useEffect, useState } from "react";
import {
  defaultVoiceProfile, matchesVoiceProfile, voiceProfileLimits,
  type VoiceProfile, type VoiceSettingsView,
} from "../voice-profile";

const fields: { key: keyof VoiceProfile; label: string; unit: string; step: number }[] = [
  { key: "endpointMinSilenceS", label: "말끝을 확정하기 전 최소 쉼", unit: "초", step: 0.1 },
  { key: "silenceTimeoutS", label: "말이 더 이어질 때 기다릴 최대 쉼", unit: "초", step: 0.1 },
  { key: "piperSpeed", label: "Piper 답변 속도", unit: "배", step: 0.05 },
  { key: "speechVolumePercent", label: "말벗 출력 음량", unit: "%", step: 1 },
];

export function OwnerVoiceSettings({ deviceId, settings, online, refresh }: {
  deviceId: string; settings: VoiceSettingsView; online: boolean;
  refresh: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<VoiceProfile | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expiredReport, setExpiredReport] = useState(0);
  const profile = draft ?? settings.profile;
  const invalid = profile.endpointMinSilenceS > profile.silenceTimeoutS;

  useEffect(() => {
    if (!online || !settings.onlineUntil) return;
    const expiresIn = settings.onlineUntil - Date.now();
    const timer = window.setTimeout(() => setExpiredReport(settings.onlineUntil), Math.max(0, expiresIn));
    return () => window.clearTimeout(timer);
  }, [online, settings.onlineUntil]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/voice-profile`, {
        method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(profile),
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error ?? "대화 설정을 저장하지 못했어요.");
      }
      await refresh();
      setDraft(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "대화 설정을 저장하지 못했어요.");
    } finally { setBusy(false); }
  }

  const status = draft && !matchesVoiceProfile(settings.profile, draft) ? "수정 중 · 저장하면 적용돼요."
    : settings.applied === null ? "개인 설정을 저장하기 전에는 말벗의 기존 설정을 사용해요."
    : !online || expiredReport >= settings.onlineUntil ? "이 말벗의 연결을 기다리는 중"
    : settings.applied ? "이 말벗에 적용됨" : "이 말벗의 적용 확인을 기다리는 중";

  return (
    <section className="robot-map-panel-card managed-robot-controls">
      <h3>내 대화 설정</h3>
      <p>저장하면 내 계정에 등록한 모든 말벗에 적용돼요.</p>
      <fieldset disabled={busy} className="voice-settings-fields">
        <legend>듣기와 말하기</legend>
        {fields.map(({ key, label, unit, step }) => (
          <label key={key}>
            {label} · {profile[key]}{unit}
            <input type="range" min={voiceProfileLimits[key][0]} max={voiceProfileLimits[key][1]} step={step}
              value={profile[key]} aria-valuetext={`${profile[key]}${unit}`}
              onChange={(event) => setDraft({ ...profile, [key]: event.target.valueAsNumber })} />
          </label>
        ))}
        <small>0.4초 쉬면 인식을 미리 시작해요. 녹음 안내는 원래 속도로 재생되며 출력 음량은 답변과 안내에 모두 적용돼요.</small>
        {invalid && <p role="alert">최대 쉼을 최소 쉼 이상으로 설정해 주세요.</p>}
        <div className="robot-map-actions is-inline">
          <button type="button" className="is-secondary" onClick={() => setDraft(defaultVoiceProfile)}>기본값 불러오기</button>
          <button type="button" onClick={() => void save()}
            disabled={invalid || (settings.configured && matchesVoiceProfile(settings.profile, profile))}>저장</button>
        </div>
      </fieldset>
      <small role="status">{status}</small>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
