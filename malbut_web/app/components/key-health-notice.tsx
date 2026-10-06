"use client";

import { useEffect, useState } from "react";

type Service = "openai" | "kma" | "fall";
type Problem = { service: Service; problem: "missing" | "invalid" | "quota" };

const COPY: Record<Service, { name: string; title: string; body: string; urgent: boolean }> = {
  openai: { name: "대화 키", title: "말벗이 지금 대화를 할 수 없어요", body: "대화용 OpenAI 키가 없거나 쓸 수 없어요.", urgent: true },
  fall: { name: "낙상 AI 키", title: "낙상 AI 확인을 할 수 없어요",
    body: "클라우드 AI 키가 없거나 쓸 수 없어요. 낙상 감지와 알림은 그대로 계속되지만, AI 재확인이 빠져 정확도가 떨어질 수 있어요.", urgent: true },
  kma: { name: "날씨 키", title: "말벗이 날씨를 확인할 수 없어요", body: "기상청 키가 없거나 쓸 수 없어요. 대화는 그대로 할 수 있어요.", urgent: false },
};

const asProblems = (value: unknown): Problem[] => Array.isArray(value)
  ? value.filter((item): item is Problem => Boolean(item) && typeof item === "object" &&
    (item as Problem).service in COPY && ["missing", "invalid", "quota"].includes((item as Problem).problem))
  : [];

/** 홈 화면 안내 (목업 15): 키 문제가 있으면 소유자와 보호자에게 알린다. 대화·낙상 AI는 빨강, 날씨는 노랑. */
export function KeyHealthNotice({ deviceId, isOwner, onOpenKeys, demo = false }: {
  deviceId: string;
  isOwner: boolean;
  onOpenKeys: () => void;
  demo?: boolean;
}) {
  const [problems, setProblems] = useState<Problem[]>([]);

  useEffect(() => {
    if (demo) {
      // Local UI demo only: /?demoKeys=openai,kma shows those keys as missing.
      const asked = new URLSearchParams(window.location.search).get("demoKeys")?.split(",") ?? [];
      window.queueMicrotask(() => setProblems(asProblems(
        (["openai", "fall", "kma"] as const).filter((service) => asked.includes(service)).map((service) => ({ service, problem: "missing" })),
      )));
      return;
    }
    let active = true;
    const load = () => void fetch(`/api/devices/${encodeURIComponent(deviceId)}/key-health`, { cache: "no-store", credentials: "same-origin" })
      .then((response) => response.ok ? response.json() : { problems: [] })
      .then((body: { problems?: unknown }) => { if (active) setProblems(asProblems(body.problems)); })
      .catch(() => undefined);
    load();
    // The robot reports every minute.
    const timer = window.setInterval(load, 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [demo, deviceId]);

  const [first, ...others] = problems;
  if (!first) return null;
  const copy = COPY[first.service];
  return (
    <article className={`ui-card ui-key-notice ${copy.urgent ? "is-urgent" : "is-warn"}`} role="alert">
      <span className={`ui-badge ${copy.urgent ? "is-danger" : "is-warn"}`}>{copy.name} 확인 필요</span>
      <strong>{copy.title}</strong>
      <p>{copy.body} {isOwner ? "키를 확인해 주세요." : "소유자에게 키를 확인해 달라고 알려 주세요."}</p>
      {others.length > 0 && <small>{others.map((other) => COPY[other.service].name).join(", ")}도 확인이 필요해요.</small>}
      {isOwner && <button type="button" className="ui-button is-strong" onClick={onOpenKeys}>키 확인하기</button>}
    </article>
  );
}
