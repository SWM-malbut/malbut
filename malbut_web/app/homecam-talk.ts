/**
 * 홈캠 › 현재 상태 › 마이크 (목업 20번): the guardian's voice to 말벗's speaker.
 * While it is on, 말벗 neither listens nor speaks, so it turns itself off.
 */
export const TALK_LIMIT_MS = 180_000;

/** Another guardian (or this person on another device) holding the microphone. */
export type TalkHolder = { name: string; self: boolean };

export type TalkPhase = "off" | "starting" | "talking";

/** Why the microphone went off without the switch: the 3-minute limit or anything else. */
export type TalkEnded = "timeout" | "dropped";

/** What the live viewer tells the 현재 상태 card about the microphone. */
export type LiveTalk = {
  phase: TalkPhase;
  /** False until the live video is connected; the switch waits like 스피커. */
  available: boolean;
  remainingMs: number;
  holder: TalkHolder | null;
  ended: TalkEnded | null;
  error: string;
  toggle: () => void;
};

export type TalkNote = { tone: "info" | "neutral" | "danger"; title: string; text: string };

export function formatTalkRemaining(ms: number) {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** The line under the microphone switch, or null when there is nothing to say. */
export function talkNote(
  talk: Pick<LiveTalk, "phase" | "remainingMs" | "holder" | "ended" | "error">,
): TalkNote | null {
  if (talk.phase === "talking") {
    return {
      tone: "info",
      title: `말하는 중 · ${formatTalkRemaining(talk.remainingMs)} 뒤 자동으로 꺼져요`,
      text: "내 목소리가 말벗 스피커로 나가요. 그동안 말벗은 듣지도 말하지도 않아요. 하던 말도 멈춰요.",
    };
  }
  if (talk.phase === "starting") {
    return { tone: "info", title: "말벗이 말하기를 준비하고 있어요", text: "말벗이 듣기를 멈추면 바로 켜져요." };
  }
  if (talk.holder) {
    return {
      tone: "neutral",
      title: talk.holder.self ? "다른 기기에서 말하는 중이에요" : `${talk.holder.name} 님이 말하는 중이에요`,
      text: "한 번에 한 명만 말할 수 있어요. 끝나면 켤 수 있어요.",
    };
  }
  if (talk.ended === "timeout") {
    return {
      tone: "neutral",
      title: "3분이 지나 마이크를 껐어요",
      text: "말벗이 다시 듣고 말할 수 있어요. 더 말하려면 다시 켜 주세요.",
    };
  }
  if (talk.error) return { tone: "danger", title: "마이크를 켜지 못했어요", text: talk.error };
  if (talk.ended === "dropped") {
    return {
      tone: "neutral",
      title: "마이크가 꺼졌어요",
      text: "화면을 떠났거나 홈캠 연결이 바뀌어 말하기가 끊겼어요. 다시 켜 주세요.",
    };
  }
  return null;
}
