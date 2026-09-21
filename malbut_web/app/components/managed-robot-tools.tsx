"use client";

import type { RobotOperation } from "../robot-contract";
import { record } from "./managed-robot-controls";
import type { RobotSnapshot } from "./robot-map-panel";
import type { ManagedWorkspace, ZoneBehavior } from "./managed-robot-workspace";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
type Props = {
  snapshot: RobotSnapshot;
  isOwner: boolean;
  busy: boolean;
  sendCommand: SendCommand;
  workspace: ManagedWorkspace;
};

const BEHAVIOR_LABEL: Record<ZoneBehavior, string> = {
  restricted: "진입 금지",
  avoid: "우회 권장",
  allow: "통행 허용",
};
const STEPS: Array<[string, string, string]> = [
  ["turn_left", "⟲", "왼쪽으로 돌기"], ["forward", "↑", "앞으로"], ["turn_right", "⟳", "오른쪽으로 돌기"],
  ["left", "←", "왼쪽으로"], ["stop", "■", "정지"], ["right", "→", "오른쪽으로"],
  ["", "", ""], ["backward", "↓", "뒤로"], ["", "", ""],
];

/** Pose finding, manual steps and Zones for the saved map in use. */
export function ManagedRobotTools({ snapshot, isOwner, busy, sendCommand, workspace }: Props) {
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
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>수동 조작</h3>
      <p>
        {system.control_mode === 1 ? "수동 조작 중(MANUAL)" : "자동(AUTONOMOUS)"}
        {manual.message ? ` · ${String(manual.message)}` : ""}
      </p>
      <div className="robot-map-actions managed-robot-pad">
        {STEPS.map(([direction, symbol, label], index) => direction ? (
          <button key={direction} className={direction === "stop" ? "is-secondary" : undefined}
            disabled={disabled || (direction !== "stop" && !running)}
            aria-label={label} title={label}
            onClick={() => void sendCommand("manual_move", { direction })}>{symbol}</button>
        ) : <span key={`blank-${index}`} aria-hidden="true" />)}
      </div>
      <small>한 번 누르면 약 12cm 또는 23°만 움직입니다. 수동 조작은 실행 중인 자율 이동을 취소하며, 5초 동안 조작이 없으면 자동으로 돌아갑니다. 충돌 감시가 장애물 앞에서 멈춥니다.</small>
    </div>
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
