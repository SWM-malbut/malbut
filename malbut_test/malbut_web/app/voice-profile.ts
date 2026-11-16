export type VoiceProfile = {
  endpointMinSilenceS: number;
  silenceTimeoutS: number;
  piperSpeed: number;
  speechVolumePercent: number;
};

export const defaultVoiceProfile: VoiceProfile = {
  endpointMinSilenceS: 0.8, silenceTimeoutS: 2.0, piperSpeed: 0.9, speechVolumePercent: 100,
};

export const voiceProfileLimits = {
  endpointMinSilenceS: [0.4, 2.0], silenceTimeoutS: [1.2, 5.0],
  piperSpeed: [0.5, 2.0], speechVolumePercent: [0, 100],
} as const;

export function parseVoiceProfile(input: unknown): VoiceProfile | null {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const value = input as Record<string, unknown>;
  if (Object.keys(value).length !== Object.keys(voiceProfileLimits).length) return null;
  for (const [key, [min, max]] of Object.entries(voiceProfileLimits)) {
    const number = value[key];
    if (typeof number !== "number" || !Number.isFinite(number) || number < min || number > max) return null;
  }
  const profile = value as VoiceProfile;
  return profile.endpointMinSilenceS <= profile.silenceTimeoutS && Number.isInteger(profile.speechVolumePercent)
    ? profile : null;
}

export function matchesVoiceProfile(expected: VoiceProfile, applied: VoiceProfile | null) {
  return applied !== null && expected.endpointMinSilenceS === applied.endpointMinSilenceS &&
    expected.silenceTimeoutS === applied.silenceTimeoutS && expected.piperSpeed === applied.piperSpeed &&
    expected.speechVolumePercent === applied.speechVolumePercent;
}

export type VoiceSettingsView = {
  profile: VoiceProfile;
  configured: boolean;
  applied: boolean | null;
  onlineUntil: number;
};
