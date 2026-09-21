"use client";

import { useState } from "react";
import type { RobotOperation } from "../robot-contract";
import type { RobotSnapshot } from "./robot-map-panel";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
const terminal = new Set(["SUCCEEDED", "CANCELED", "ABORTED", "REJECTED", "ERROR"]);
const LOCALIZATION_LABEL: Record<string, string> = {
  MAPPING: "지도 작성 중(SLAM)",
  LOCALIZATION: "저장 지도 사용 중(AMCL)",
  SWITCHING: "전환 중",
  ERROR: "오류",
};

/** The service UI adapts existing robot contracts; scheduling stays on the robot. */
export function ManagedRobotControls({ snapshot, isOwner, busy, sendCommand, goal }: {
  snapshot: RobotSnapshot;
  isOwner: boolean;
  busy: boolean;
  sendCommand: SendCommand;
  goal: { x: number; y: number } | null;
}) {
  const [mapName, setMapName] = useState("home");
  const [selectedMap, setSelectedMap] = useState("");
  const [distance, setDistance] = useState("1.0");
  const [thoroughness, setThoroughness] = useState(1);
  const target = snapshot.state?.target ?? {};
  const runtime = record(target.runtime);
  const localization = record(runtime.localization);
  const servers = record(target.servers);
  const maps = Array.isArray(target.maps) ? target.maps.map(record).filter((map) => typeof map.id === "string") : [];
  const requests = Array.isArray(target.requests) ? target.requests.map(record) : [];
  const active = requests.filter((request) => !terminal.has(String(request.state)));
  const managed = snapshot.state?.nav2.robot_interface === "malbut_manager_v1";
  const disabled = !isOwner || !snapshot.online || !managed || busy;
  const stopped = runtime.state === "STOPPED";
  const running = runtime.state === "RUNNING";
  const switching = localization.mode === "SWITCHING";
  const navigationReady = runtime.mode === "navigation" && runtime.ready === true && servers.manager === true;
  const mappingReady = runtime.mode === "mapping" && runtime.ready === true && servers.autoslam === true;
  const inUse = !stopped && typeof runtime.map === "string" ? runtime.map : "";
  const knownMap = maps.some((map) => map.id === selectedMap);
  const mission = (capability: string, args: Record<string, unknown>) => sendCommand("mission_start", { capability, arguments: args });
  const deleteMap = () => {
    if (window.confirm(`저장 지도 '${selectedMap}'을(를) 로봇에서 삭제할까요? 지도에 저장한 구역도 함께 지워지며 되돌릴 수 없습니다.`)) {
      void sendCommand("map_delete", { map: selectedMap });
    }
  };
  return <>
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>로봇 실행 준비</h3>
      {!managed && <p>실로봇 연결을 기다리고 있습니다.</p>}
      <p>{String(runtime.message || runtime.state || "상태 수신 대기")}</p>
      {Array.isArray(runtime.waiting) && runtime.waiting.length > 0 && <p>준비 대기: {runtime.waiting.join(", ")}</p>}
      {typeof localization.mode === "string" && <p>
        위치 추정: {LOCALIZATION_LABEL[localization.mode] ?? localization.mode}
        {typeof localization.map === "string" ? ` · ${localization.map}` : ""}
        {localization.message ? ` · ${String(localization.message)}` : ""}
      </p>}
      <div className="robot-map-actions is-inline">
        <button disabled={disabled || switching || !(stopped || (running && runtime.mode !== "mapping"))}
          onClick={() => void sendCommand("runtime_start", { mode: "mapping" })}>
          {stopped ? "지도 만들기 모드" : "지도 만들기로 전환"}
        </button>
        <button className="is-secondary" disabled={disabled || stopped || runtime.state === "STOPPING"} onClick={() => void sendCommand("runtime_stop")}>Bringup 종료</button>
      </div>
      <label>저장 지도
        <select value={selectedMap} onChange={(event) => setSelectedMap(event.target.value)}>
          <option value="">지도를 선택하세요</option>
          {maps.map((map) => <option key={String(map.id)} value={String(map.id)}>
            {String(map.name || map.id)}{map.id === inUse ? " (사용 중)" : ""}
          </option>)}
        </select>
      </label>
      <div className="robot-map-actions">
        <button disabled={disabled || switching || !knownMap || !(stopped || running) || selectedMap === inUse}
          onClick={() => void sendCommand("runtime_start", { mode: "navigation", map: selectedMap })}>
          {stopped ? "선택한 지도로 주행 준비" : "선택한 지도로 전환"}
        </button>
        <button className="is-danger" disabled={disabled || !knownMap || selectedMap === inUse} onClick={deleteMap}>
          선택한 지도 삭제
        </button>
      </div>
      <small>{stopped
        ? "준비만으로 자동 주행하지 않습니다."
        : "실행 중에는 재시작 없이 위치 추정만 바꿉니다. 이동 중인 작업이 있으면 거부합니다. 사용 중인 지도는 삭제할 수 없습니다."}</small>
    </div>
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>기능 실행</h3>
      <label>새 지도 이름 <input value={mapName} maxLength={64} onChange={(event) => setMapName(event.target.value)} /></label>
      <div className="robot-map-actions">
        <button disabled={disabled || !mappingReady || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(mapName)} onClick={() => void mission("autoslam", { map_name: mapName })}>자동 지도 만들기</button>
      </div>
      <label>사람과 유지할 거리(m)
        <input type="number" min="0.2" step="0.1" value={distance} onChange={(event) => setDistance(event.target.value)} />
      </label>
      <div className="robot-map-actions">
        <button disabled={disabled || !navigationReady || !Number.isFinite(Number(distance)) || Number(distance) < 0.2} onClick={() => void mission("follow_person", { target_mode: 0, target_person_id: "", desired_distance_m: Number(distance) })}>앞에 보이는 사람 따라가기</button>
      </div>
      <label>순찰 꼼꼼함
        <select value={thoroughness} onChange={(event) => setThoroughness(Number(event.target.value))}>
          <option value={0}>가볍게</option><option value={1}>보통</option><option value={2}>꼼꼼히</option>
        </select>
      </label>
      <div className="robot-map-actions">
        <button disabled={disabled || !navigationReady} onClick={() => void mission("patrol", { thoroughness })}>순찰 시작</button>
      </div>
      {goal && <>
        <p>선택 위치: {goal.x.toFixed(2)}, {goal.y.toFixed(2)}m</p>
        <div className="robot-map-actions">
          <button disabled={disabled || !navigationReady} onClick={() => void mission("navigate_to_pose", { ...goal, yaw: 0 })}>선택한 위치로 이동</button>
        </div>
        <small>매니저를 통해 Nav2에 요청합니다. 지도 선택은 경로 검증 결과가 아닙니다.</small>
      </>}
      <div className="robot-map-actions">
        <button className="is-danger" disabled={disabled || active.length === 0} onClick={() => void sendCommand("mission_cancel")}>웹에서 요청한 작업 취소</button>
      </div>
      <small>새 주행 요청은 같은 자원을 쓰는 작업을 안전히 중지하고 대체할 수 있습니다. 창 닫기·연결 끊김은 정지가 아닙니다.</small>
    </div>
    <div className="robot-map-panel-card">
      <h3>실행 상태·결과</h3>
      {requests.length === 0 ? <p>아직 요청한 작업이 없습니다.</p> : requests.slice().reverse().map((request) => {
        const result = record(request.result);
        return <p key={String(request.id)}>
          <strong>{String(request.capability)} · {String(request.state)}</strong><br />
          {String(request.message || result.message || yamlMessage(result.result_yaml))}
        </p>;
      })}
      <small>명령 접수와 작업 완료는 다릅니다. 최종 실행 결과를 확인하세요.</small>
    </div>
  </>;
}

/** Downstream results arrive as the manager's YAML; show their message line. */
function yamlMessage(value: unknown) {
  const match = typeof value === "string" ? /^message: *(.*)$/m.exec(value) : null;
  return match ? match[1].replace(/^(['"])(.*)\1$/, "$2") : "";
}

export function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
