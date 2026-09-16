"use client";

import { useState } from "react";
import type { RobotOperation } from "../robot-contract";
import type { RobotSnapshot } from "./robot-map-panel";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
const terminal = new Set(["SUCCEEDED", "CANCELED", "ABORTED", "REJECTED", "ERROR"]);

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
  const servers = record(target.servers);
  const maps = Array.isArray(target.maps) ? target.maps.map(record).filter((map) => typeof map.id === "string") : [];
  const requests = Array.isArray(target.requests) ? target.requests.map(record) : [];
  const active = requests.filter((request) => !terminal.has(String(request.state)));
  const managed = snapshot.state?.nav2.robot_interface === "malbut_manager_v1";
  const disabled = !isOwner || !snapshot.online || !managed || busy;
  const stopped = runtime.state === "STOPPED";
  const navigationReady = runtime.mode === "navigation" && runtime.ready === true && servers.manager === true;
  const mappingReady = runtime.mode === "mapping" && runtime.ready === true && servers.autoslam === true;
  const mission = (capability: string, args: Record<string, unknown>) => sendCommand("mission_start", { capability, arguments: args });
  return <>
    <div className="robot-map-panel-card managed-robot-controls">
      <h3>로봇 실행 준비</h3>
      {!managed && <p>실로봇 연결을 기다리고 있습니다.</p>}
      <p>{String(runtime.message || runtime.state || "상태 수신 대기")}</p>
      {Array.isArray(runtime.waiting) && runtime.waiting.length > 0 && <p>준비 대기: {runtime.waiting.join(", ")}</p>}
      <div className="robot-map-actions is-inline">
        <button disabled={disabled || !stopped} onClick={() => void sendCommand("runtime_start", { mode: "mapping" })}>지도 만들기 모드</button>
        <button className="is-secondary" disabled={disabled || stopped || runtime.state === "STOPPING"} onClick={() => void sendCommand("runtime_stop")}>Bringup 종료</button>
      </div>
      <label>저장 지도
        <select value={selectedMap} onChange={(event) => setSelectedMap(event.target.value)}>
          <option value="">지도를 선택하세요</option>
          {maps.map((map) => <option key={String(map.id)} value={String(map.id)}>{String(map.name || map.id)}</option>)}
        </select>
      </label>
      <div className="robot-map-actions">
        <button disabled={disabled || !stopped || !maps.some((map) => map.id === selectedMap)} onClick={() => void sendCommand("runtime_start", { mode: "navigation", map: selectedMap })}>선택한 지도로 주행 준비</button>
      </div>
      <small>모드를 바꾸려면 Bringup을 종료하세요. 준비만으로 자동 주행하지 않습니다.</small>
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
          {String(request.message || result.message || "")}
        </p>;
      })}
      <small>명령 접수와 작업 완료는 다릅니다. 최종 실행 결과를 확인하세요.</small>
    </div>
  </>;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
