"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ManualVelocity, RobotOperation } from "../robot-contract";
import { record } from "./managed-robot-controls";
import type { RobotSnapshot } from "./robot-map-panel";
import type { ManagedWorkspace, ZoneBehavior } from "./managed-robot-workspace";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
/** Sends one held velocity; resolves to an error message or "" when delivered. */
type Drive = (velocity: ManualVelocity) => Promise<string>;
type Props = {
  snapshot: RobotSnapshot;
  isOwner: boolean;
  busy: boolean;
  sendCommand: SendCommand;
  drive: Drive;
  workspace: ManagedWorkspace;
};

const BEHAVIOR_LABEL: Record<ZoneBehavior, string> = {
  restricted: "진입 금지",
  avoid: "우회 권장",
  allow: "통행 허용",
};
// Page limits stay under the driver's 0.2 m/s and 0.5 rad/s clamp.
const DRIVE_MAX = { vx: 0.15, vy: 0.15, wz: 0.5 };
// The robot holds each velocity for 1 s; repeating at 5 Hz rides out its 0.2 s poll.
const DRIVE_REPEAT_MS = 200;
const STICK_DEADZONE = 0.12;
const ZERO: ManualVelocity = { vx: 0, vy: 0, wz: 0 };
const KEYS: Record<string, keyof typeof KEY_AXES> = {
  ArrowUp: "forward", KeyW: "forward", ArrowDown: "backward", KeyS: "backward",
  ArrowLeft: "turnLeft", KeyA: "turnLeft", ArrowRight: "turnRight", KeyD: "turnRight",
  KeyQ: "strafeLeft", KeyE: "strafeRight",
};
const KEY_AXES = {
  forward: { vx: 1 }, backward: { vx: -1 }, turnLeft: { wz: 1 }, turnRight: { wz: -1 },
  strafeLeft: { vy: 1 }, strafeRight: { vy: -1 },
} as const;

function round(value: number) {
  return Math.round(value * 1000) / 1000;
}

function velocityFrom(stick: { x: number; y: number } | null, keys: Set<string>, strafe: boolean): ManualVelocity {
  if (stick) {
    // Up on the pad is forward; left is a left turn, or a left strafe when chosen.
    const vx = -stick.y * DRIVE_MAX.vx;
    return strafe
      ? { vx: round(vx), vy: round(-stick.x * DRIVE_MAX.vy), wz: 0 }
      : { vx: round(vx), vy: 0, wz: round(-stick.x * DRIVE_MAX.wz) };
  }
  const sum = { vx: 0, vy: 0, wz: 0 };
  for (const key of keys) {
    const axis = KEYS[key];
    if (!axis) continue;
    for (const [name, sign] of Object.entries(KEY_AXES[axis]) as Array<[keyof ManualVelocity, number]>) {
      sum[name] += sign;
    }
  }
  return {
    vx: round(Math.sign(sum.vx) * DRIVE_MAX.vx),
    vy: round(Math.sign(sum.vy) * DRIVE_MAX.vy),
    wz: round(Math.sign(sum.wz) * DRIVE_MAX.wz),
  };
}

function moving(velocity: ManualVelocity) {
  return velocity.vx !== 0 || velocity.vy !== 0 || velocity.wz !== 0;
}

/** Pose finding, the drive pad and Zones for the saved map in use. */
export function ManagedRobotTools({ snapshot, isOwner, busy, sendCommand, drive, workspace }: Props) {
  const target = snapshot.state?.target ?? {};
  const runtime = record(target.runtime);
  const localization = record(runtime.localization);
  const system = record(target.system);
  const manual = record(target.manual);
  const zoneState = record(target.zones);
  const managed = snapshot.state?.nav2.robot_interface === "malbut_manager_v1";
  const disabled = !isOwner || !snapshot.online || !managed || busy;
  const onSavedMap = localization.mode === "LOCALIZATION";
  const running = runtime.state === "RUNNING";
  const { pose } = workspace;
  const zones = workspace.zones;
  const zoneCommand = snapshot.command?.operation === "zones_save" ? snapshot.command : null;
  const relocalize = (args: Record<string, unknown>) => sendCommand("mission_start", {
    capability: "relocalize", arguments: args,
  });
  const saveZones = () => {
    const payload = zones.payload();
    if (payload) void sendCommand("zones_save", payload);
  };
  return <>
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>위치 보정</h3>
      <p>{localization.mode === "SWITCHING" ? "위치를 찾는 중입니다."
        : onSavedMap ? String(localization.message || "저장 지도에서 위치를 추정하고 있습니다.")
          : "저장 지도로 주행 준비를 하면 사용할 수 있습니다."}</p>
      <div className="robot-map-actions">
        <button disabled={disabled || !onSavedMap} onClick={() => void relocalize({ method: 0 })}>위치 다시 찾기(저장 위치 먼저)</button>
        <button className="is-secondary" disabled={disabled || !onSavedMap} onClick={() => void relocalize({ method: 2 })}>지도 전체에서 찾기(제자리 회전)</button>
      </div>
      {pose ? <>
        <p>지정한 위치: {pose.x.toFixed(2)}, {pose.y.toFixed(2)}m · {(pose.yaw * 180 / Math.PI).toFixed(0)}°</p>
        <div className="robot-map-actions">
          <button disabled={disabled || !onSavedMap} onClick={() => void relocalize({ method: 1, ...pose })}>이 위치로 설정</button>
        </div>
      </> : <small>현재 위치를 직접 정하려면 지도 위 <strong>현재 위치 지정</strong>에서 로봇이 있는 곳을 누른 채 바라보는 방향으로 끌어 놓으세요.</small>}
      <small>저장 위치 확인과 전체 찾기는 로봇이 제자리에서 회전할 수 있습니다. 결과는 실행 상태·결과에 표시됩니다.</small>
    </div>
    <DrivePad drive={drive} enabled={isOwner && snapshot.online && managed && running}
      manualMode={system.control_mode === 1} robotState={String(manual.state ?? "")} />
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>구역(진입 금지·우회)</h3>
      <p>
        {zoneState.state ? `주행 반영: ${String(zoneState.state)}` : "주행 반영 상태 수신 대기"}
        {zoneState.message ? ` · ${String(zoneState.message)}` : ""}
      </p>
      {!zones.document?.editable ? <p>{zones.document?.message || "저장 지도로 주행 중일 때 구역을 편집할 수 있습니다."}</p> : <>
        {zones.zones.length === 0 ? <p>아직 구역이 없습니다.</p> : <div className="managed-robot-zone-list">
          {zones.zones.map((zone, index) => (
            <button key={index} type="button" className={index === zones.selected ? "is-selected" : undefined}
              onClick={() => zones.select(index)}>
              {index + 1}. {BEHAVIOR_LABEL[zone.behavior]}{zone.name ? ` · ${zone.name}` : ""} · 꼭짓점 {zone.points.length}개
            </button>
          ))}
        </div>}
        <label>{zones.selected === null ? "새 구역 종류" : "선택한 구역 종류"}
          <select value={zones.selected === null ? zones.behavior : zones.zones[zones.selected]?.behavior ?? zones.behavior}
            disabled={!isOwner}
            onChange={(event) => zones.setBehavior(event.target.value as ZoneBehavior)}>
            <option value="restricted">진입 금지(비용 100)</option>
            <option value="avoid">우회 권장(비용 70)</option>
            <option value="allow">통행 허용(비용 0)</option>
          </select>
        </label>
        {zones.drawing.length > 0 ? <>
          <p>꼭짓점 {zones.drawing.length}개를 찍었습니다. 3개 이상이면 완료할 수 있습니다.</p>
          <div className="robot-map-actions is-inline">
            <button disabled={zones.drawing.length < 3} onClick={zones.finishDrawing}>그리기 완료</button>
            <button className="is-secondary" onClick={zones.undoPoint}>점 하나 취소</button>
            <button className="is-secondary" onClick={zones.cancelDrawing}>그리기 취소</button>
          </div>
        </> : <small>지도 위 <strong>구역 편집</strong>에서 빈 곳을 눌러 꼭짓점을 찍습니다. 기존 구역을 누르면 선택됩니다.</small>}
        <div className="robot-map-actions">
          <button className="is-danger" disabled={!isOwner || zones.selected === null} onClick={zones.removeSelected}>선택한 구역 삭제</button>
          <button disabled={disabled || !zones.dirty || zones.drawing.length > 0} onClick={saveZones}>구역 저장·주행에 반영</button>
          <button className="is-secondary" disabled={!zones.dirty} onClick={zones.discard}>변경 취소</button>
        </div>
        {zoneCommand && <small>
          {zoneCommand.status === "failed" ? `저장 실패: ${String(record(zoneCommand.result).error ?? "")}`
            : zoneCommand.status === "completed" ? "로봇에 저장했습니다. 지도가 다시 올라오면 이 화면에도 반영됩니다."
              : "로봇에 저장하는 중입니다."}
        </small>}
      </>}
      <small>진입 금지 구역은 경계 20cm까지 비용 100(통과 불가), 우회 권장은 비용 70으로 경로가 피해 갑니다. 구역은 이 저장 지도에만 적용됩니다.</small>
    </div>
  </>;
}

/** A held joystick (pointer drag) or keyboard drive that stops on release. */
function DrivePad({ drive, enabled, manualMode, robotState }: {
  drive: Drive;
  enabled: boolean;
  manualMode: boolean;
  robotState: string;
}) {
  const [stick, setStick] = useState<{ x: number; y: number } | null>(null);
  const [keys, setKeys] = useState<Set<string>>(() => new Set());
  const [strafe, setStrafe] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState<ManualVelocity>(ZERO);
  const padRef = useRef<HTMLDivElement | null>(null);
  const velocity = useMemo(
    () => (enabled ? velocityFrom(stick, keys, strafe) : ZERO),
    [enabled, keys, stick, strafe],
  );
  const latest = useRef(velocity);
  useEffect(() => {
    latest.current = velocity;
  }, [velocity]);
  const active = moving(velocity);

  const send = useCallback(async (value: ManualVelocity) => {
    setSent(value);
    const message = await drive(value);
    setError(message);
  }, [drive]);

  useEffect(() => {
    if (!active) return;
    void send(latest.current);
    const timer = window.setInterval(() => void send(latest.current), DRIVE_REPEAT_MS);
    return () => {
      window.clearInterval(timer);
      void send(ZERO);  // Release: an explicit stop, not only the robot's hold timeout.
    };
  }, [active, send]);

  useEffect(() => {
    // A hidden tab or lost window focus releases every input like a dropped stick.
    const release = () => {
      setStick(null);
      setKeys(new Set());
    };
    const onVisibility = () => {
      if (document.visibilityState !== "visible") release();
    };
    window.addEventListener("blur", release);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", release);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  function readStick(event: React.PointerEvent<HTMLDivElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const radius = Math.min(bounds.width, bounds.height) / 2;
    let x = (event.clientX - (bounds.left + bounds.width / 2)) / radius;
    let y = (event.clientY - (bounds.top + bounds.height / 2)) / radius;
    const length = Math.hypot(x, y);
    if (length < STICK_DEADZONE) return { x: 0, y: 0 };
    if (length > 1) {
      x /= length;
      y /= length;
    }
    return { x, y };
  }

  const pointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!enabled) return;
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    setStick(readStick(event));
  };
  const pointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (stick && event.currentTarget.hasPointerCapture(event.pointerId)) setStick(readStick(event));
  };
  const pointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setStick(null);
  };
  const keyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.code === "Space") {
      event.preventDefault();
      setKeys(new Set());
      setStick(null);
      return;
    }
    if (!KEYS[event.code]) return;
    event.preventDefault();
    if (!enabled || event.repeat) return;
    setKeys((current) => new Set(current).add(event.code));
  };
  const keyUp = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!KEYS[event.code]) return;
    event.preventDefault();
    setKeys((current) => {
      const next = new Set(current);
      next.delete(event.code);
      return next;
    });
  };

  const knob = stick ?? {
    x: -Math.sign(strafe ? velocity.vy : velocity.wz) * (velocity.wz || velocity.vy ? 0.8 : 0),
    y: -Math.sign(velocity.vx) * (velocity.vx ? 0.8 : 0),
  };
  return (
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>수동 조작</h3>
      <p>
        {manualMode ? "수동 조작 중(MANUAL)" : "자동(AUTONOMOUS)"}
        {robotState === "MOVING" ? " · 로봇이 명령을 받고 있습니다" : ""}
      </p>
      <div ref={padRef} className={`managed-robot-joystick${enabled ? "" : " is-disabled"}${active ? " is-active" : ""}`}
        role="application" tabIndex={enabled ? 0 : -1}
        aria-label="수동 조작 패드: 누른 채 끌거나, 포커스한 뒤 방향키·WASD로 움직입니다"
        onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp}
        onPointerCancel={pointerUp} onLostPointerCapture={() => setStick(null)}
        onKeyDown={keyDown} onKeyUp={keyUp} onBlur={() => setKeys(new Set())}>
        <span className="managed-robot-joystick-axis is-vertical" aria-hidden="true" />
        <span className="managed-robot-joystick-axis is-horizontal" aria-hidden="true" />
        <span className="managed-robot-joystick-knob" aria-hidden="true"
          style={{ transform: `translate(${(knob.x * 52).toFixed(1)}px, ${(knob.y * 52).toFixed(1)}px)` }}>
          {active ? "●" : "＋"}
        </span>
      </div>
      <p className="managed-robot-drive-readout">
        {active
          ? `전진 ${sent.vx.toFixed(2)} m/s · ${strafe ? `옆으로 ${sent.vy.toFixed(2)} m/s` : `회전 ${sent.wz.toFixed(2)} rad/s`}`
          : enabled ? "정지 · 패드를 누른 채 끌거나 키보드로 조작하세요" : "저장 지도나 지도 만들기로 실행 중일 때 조작할 수 있습니다"}
      </p>
      {error && <p role="alert">명령 전달 실패: {error}</p>}
      <label className="managed-robot-drive-option">
        <input type="checkbox" checked={strafe} onChange={(event) => setStrafe(event.target.checked)} />
        패드 좌우를 회전 대신 옆이동(메카넘)으로
      </label>
      <small>패드를 누른 채 끄는 만큼 속도가 커집니다(최대 0.15 m/s, 0.5 rad/s). 키보드는 패드를 클릭해 포커스한 뒤 방향키 또는 W·A·S·D(회전 A·D), Q·E(옆이동), 스페이스(정지)입니다. 손을 떼면 바로 정지 명령을 보내고, 로봇은 마지막 명령을 1초 넘게 받지 못하면 스스로 멈춥니다. 첫 입력에 자율 이동이 취소되고 수동 조작으로 바뀌며, 5초 동안 입력이 없으면 자동으로 돌아갑니다. 충돌 감시가 장애물 앞에서 멈춥니다.</small>
    </div>
  );
}
