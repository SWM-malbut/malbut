"use client";

import { useState } from "react";
import type { RobotOperation } from "../robot-contract";
import { record } from "./managed-robot-controls";
import type { RobotSnapshot } from "./robot-map-panel";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
type Capability = { id: string; title: string; fields: Record<string, { type?: string; default?: unknown }> };
type CommandRecord = {
  id: string;
  operation: string;
  payload: unknown;
  status: string;
  requestedAt: string;
  claimedAt: string | null;
  completedAt: string | null;
  result: unknown;
};

/** Owner-only tools for checking the robot link and trying any registered capability. */
export function ManagedRobotDebug({ deviceId, snapshot, busy, sendCommand, report, now }: {
  deviceId: string;
  snapshot: RobotSnapshot;
  busy: boolean;
  sendCommand: SendCommand;
  /** The last completed diagnostics result, kept while other requests follow it. */
  report: Record<string, unknown> | null;
  now: number;
}) {
  const [capabilityId, setCapabilityId] = useState("");
  const [argumentsText, setArgumentsText] = useState("{}");
  const [argumentsError, setArgumentsError] = useState("");
  const [history, setHistory] = useState<CommandRecord[] | null>(null);
  const [historyError, setHistoryError] = useState("");
  const command = snapshot.command;
  const disabled = !snapshot.online || busy;
  const capabilities = Array.isArray(report?.capabilities)
    ? (report.capabilities as unknown[]).map(record).filter((item): item is Capability => typeof item.id === "string")
    : [];
  const observedAt = snapshot.state?.observedAt ? Date.parse(snapshot.state.observedAt) : NaN;
  const topics = record(report?.topics);
  const actions = record(report?.actions);
  const silentTopics = Object.entries(topics).filter(([, value]) => record(value).publishers === 0).map(([name]) => name);
  const missingActions = Object.entries(actions).filter(([, ready]) => ready !== true).map(([name]) => name);

  function chooseCapability(id: string) {
    setCapabilityId(id);
    const capability = capabilities.find((item) => item.id === id);
    const defaults = Object.fromEntries(Object.entries(capability?.fields ?? {}).map(
      ([name, spec]) => [name, spec.default ?? null],
    ));
    setArgumentsText(JSON.stringify(defaults, null, 2));
    setArgumentsError("");
  }

  function runCapability() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(argumentsText);
    } catch {
      setArgumentsError("인자는 JSON 객체여야 합니다.");
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setArgumentsError("인자는 JSON 객체여야 합니다.");
      return;
    }
    setArgumentsError("");
    void sendCommand("debug_mission_start", { capability: capabilityId, arguments: parsed });
  }

  async function loadHistory() {
    setHistoryError("");
    try {
      const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/commands`, { cache: "no-store" });
      const payload = await response.json().catch(() => ({})) as { commands?: CommandRecord[]; error?: string };
      if (!response.ok || !Array.isArray(payload.commands)) throw new Error(payload.error ?? "명령 기록을 불러오지 못했습니다.");
      setHistory(payload.commands);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "명령 기록을 불러오지 못했습니다.");
    }
  }

  return (
    <div className="robot-map-panel-card managed-robot-controls managed-robot-debug">
      <h3>디버깅</h3>
      <details open>
        <summary>연결</summary>
        <dl>
          <dt>연결</dt><dd>{snapshot.online ? "온라인" : "오프라인"}</dd>
          <dt>마지막 상태</dt><dd>{Number.isFinite(observedAt) ? `${new Date(observedAt).toLocaleTimeString()} (${secondsSince(observedAt, now)})` : "없음"}</dd>
          <dt>최근 명령</dt><dd>{command ? `${command.operation} · ${command.status} · ${timing(command)}` : "없음"}</dd>
        </dl>
        {command?.status === "failed" && <pre>{pretty(command.result)}</pre>}
        <div className="robot-map-actions is-inline">
          <button disabled={disabled} onClick={() => void sendCommand("robot_ping")}>왕복 시간 측정</button>
          <button className="is-secondary" disabled={disabled} onClick={() => void sendCommand("mission_cancel")}>웹 작업 모두 취소</button>
        </div>
        <small>왕복 시간은 웹 요청 → 로봇 수신 → 로봇 응답까지입니다. 로봇은 약 1초마다 명령을 가져갑니다.</small>
      </details>
      <details open>
        <summary>로봇 진단</summary>
        <div className="robot-map-actions">
          <button disabled={disabled} onClick={() => void sendCommand("robot_diagnostics")}>진단 실행</button>
        </div>
        {report ? <>
          <p>노드 {Array.isArray(report.nodes) ? report.nodes.length : 0}개 · 발행자 없는 토픽 {silentTopics.length}개 · 준비 안 된 Action {missingActions.length}개</p>
          {silentTopics.length > 0 && <p>발행자 없음: {silentTopics.join(", ")}</p>}
          {missingActions.length > 0 && <p>Action 없음: {missingActions.join(", ")}</p>}
          <details>
            <summary>진단 원본(JSON)</summary>
            <CopyButton text={pretty(report)} />
            <pre>{pretty(report)}</pre>
          </details>
        </> : <small>로봇의 노드·토픽 발행자·Action 서버·위치 추정·구역·수동 조작 상태와 등록된 기능 목록을 가져옵니다.</small>}
      </details>
      <details>
        <summary>기능 직접 실행</summary>
        {capabilities.length === 0 ? <small>먼저 진단을 실행하면 등록된 기능과 기본 인자를 불러옵니다.</small> : <>
          <label>기능
            <select value={capabilityId} onChange={(event) => chooseCapability(event.target.value)}>
              <option value="">기능을 선택하세요</option>
              {capabilities.map((item) => <option key={item.id} value={item.id}>{item.id}{item.title ? ` · ${item.title}` : ""}</option>)}
            </select>
          </label>
          <label>인자(JSON, 시스템 관리자가 Manifest로 검사)
            <textarea rows={8} spellCheck={false} value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} />
          </label>
          {argumentsError && <p role="alert">{argumentsError}</p>}
          <div className="robot-map-actions">
            <button disabled={disabled || !capabilityId} onClick={runCapability}>시스템 관리자로 실행</button>
          </div>
          <small>우선순위·자원·지도 조건은 일반 요청과 같습니다. 결과는 실행 상태·결과에 표시됩니다.</small>
        </>}
      </details>
      <details>
        <summary>최근 명령</summary>
        <div className="robot-map-actions">
          <button className="is-secondary" onClick={() => void loadHistory()}>기록 새로고침</button>
        </div>
        {historyError && <p role="alert">{historyError}</p>}
        {history && (history.length === 0 ? <p>명령 기록이 없습니다.</p> : <ol className="managed-robot-history">
          {history.map((item) => <li key={item.id}>
            <details>
              <summary>{new Date(item.requestedAt).toLocaleTimeString()} · {item.operation} · {item.status} · {timing(item)}</summary>
              <pre>{pretty({ payload: item.payload, result: item.result })}</pre>
            </details>
          </li>)}
        </ol>)}
      </details>
      <details>
        <summary>상태 원본(JSON)</summary>
        <CopyButton text={pretty(snapshot.state)} />
        <pre>{pretty(snapshot.state)}</pre>
      </details>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="robot-map-actions">
      <button className="is-secondary" onClick={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}>
        {copied ? "복사했습니다" : "복사"}
      </button>
    </div>
  );
}

function timing(command: { requestedAt: string; claimedAt?: string | null; completedAt?: string | null }) {
  const requested = Date.parse(command.requestedAt);
  const claimed = command.claimedAt ? Date.parse(command.claimedAt) : NaN;
  const completed = command.completedAt ? Date.parse(command.completedAt) : NaN;
  if (Number.isFinite(completed)) {
    return `왕복 ${((completed - requested) / 1000).toFixed(2)}초`
      + (Number.isFinite(claimed) ? ` (수신까지 ${((claimed - requested) / 1000).toFixed(2)}초)` : "");
  }
  return Number.isFinite(claimed) ? "로봇이 처리 중" : "로봇 수신 대기";
}

function secondsSince(time: number, now: number) {
  const seconds = Math.max(0, (now - time) / 1000);
  return seconds < 1 ? "방금" : `${seconds.toFixed(0)}초 전`;
}

function pretty(value: unknown) {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch {
    return String(value);
  }
}
