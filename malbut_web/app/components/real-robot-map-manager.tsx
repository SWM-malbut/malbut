"use client";

/**
 * 지도 탭에서 실로봇 다루기 (SWM25-237): 개발자 화면에 있던 지도 만들기·저장 지도 전환·삭제와
 * 수동 조작을 일반 화면(목업 18·19번)으로 옮긴 것. 로봇 쪽은 개발자 화면과 같은 명령
 * (autoslam 임무, runtime_start navigation, map_delete, manual_move)을 그대로 쓴다.
 */
import { useCallback, useEffect, useState } from "react";
import type { ManualVelocity, RobotOperation } from "../robot-contract";
import { newMapNames, twoDigits } from "../robot-map-names";
import { useManualDrive } from "./managed-robot-tools";
import type { RobotSnapshot } from "./robot-map-panel";

type SendCommand = (operation: RobotOperation, payload?: Record<string, unknown>) => Promise<boolean>;
type SavedMap = { id: string; name: string; savedAt: string | null };
type Request = { id: string; capability: string; state: string; feedback: unknown; result: unknown };

const ACTIVE = new Set(["PENDING", "RUNNING", "CANCELING", "UNCONFIRMED"]);
const STEP_COPY: Record<string, string> = {
  WAITING: "준비하고 있어요",
  EXPLORING: "집을 둘러보고 있어요",
  NAVIGATING: "새로운 곳으로 이동하고 있어요",
  SAVING: "지도를 저장하고 있어요",
  CANCELING: "멈추는 중이에요",
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** "state: EXPLORING\nknown_area_m2: 24.5" — the flat YAML the manager forwards. */
function yamlFields(value: unknown) {
  const fields: Record<string, string> = {};
  if (typeof value !== "string") return fields;
  for (const line of value.split("\n")) {
    const match = /^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$/.exec(line);
    if (match) fields[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return fields;
}

function savedMaps(target: Record<string, unknown>): SavedMap[] {
  const maps = Array.isArray(target.maps) ? target.maps : [];
  return maps.flatMap((item) => {
    const map = record(item);
    return typeof map.id === "string" && typeof map.name === "string"
      ? [{ id: map.id, name: map.name, savedAt: typeof map.savedAt === "string" ? map.savedAt : null }]
      : [];
  });
}

function latestAutoslam(target: Record<string, unknown>): Request | null {
  const requests = Array.isArray(target.requests) ? target.requests : [];
  for (let index = requests.length - 1; index >= 0; index -= 1) {
    const item = record(requests[index]);
    if (item.capability === "autoslam" && typeof item.id === "string") {
      return { id: item.id, capability: "autoslam", state: String(item.state ?? ""), feedback: item.feedback, result: item.result };
    }
  }
  return null;
}

function madeCopy(value: string | null, now: number) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const sameDay = date.toDateString() === new Date(now).toDateString();
  const clock = `${twoDigits(date.getHours())}:${twoDigits(date.getMinutes())}`;
  return sameDay ? `오늘 ${clock} 만듦` : `${date.getMonth() + 1}월 ${date.getDate()}일 만듦`;
}

export function RealRobotMapManager({
  deviceId, snapshot, isOwner, busy, commandActive, driveActive, roomCount, sendCommand,
}: {
  deviceId: string;
  snapshot: RobotSnapshot | null;
  isOwner: boolean;
  busy: boolean;
  /** Another robot command is still being delivered. */
  commandActive: boolean;
  /** A destination drive, patrol or following is running. */
  driveActive: boolean;
  roomCount: number;
  sendCommand: SendCommand;
}) {
  const target = record(snapshot?.state?.target);
  const runtime = record(target.runtime);
  const localization = record(runtime.localization);
  const servers = record(target.servers);
  const maps = savedMaps(target);
  const running = runtime.state === "RUNNING";
  const switching = localization.mode === "SWITCHING";
  const inUse = runtime.mode === "navigation" && typeof runtime.map === "string" ? runtime.map : "";
  const autoslam = latestAutoslam(target);
  const mapping = Boolean(autoslam && ACTIVE.has(autoslam.state));
  const online = Boolean(snapshot?.online);

  const [labels, setLabels] = useState<Record<string, string>>({});
  const [draftName, setDraftName] = useState(() => newMapNames(new Date(), new Set()).label);
  const [editing, setEditing] = useState<{ map: string; name: string } | null>(null);
  const [message, setMessage] = useState("");
  const [now, setNow] = useState(0);

  const loadLabels = useCallback(async () => {
    const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/map-labels`, { cache: "no-store" });
    const payload = await response.json().catch(() => ({})) as { labels?: Record<string, string> };
    if (response.ok && payload.labels) setLabels(payload.labels);
  }, [deviceId]);
  useEffect(() => {
    window.queueMicrotask(() => {
      setNow(Date.now());
      void loadLabels();
    });
  }, [loadLabels]);

  const saveLabel = async (map: string, name: string) => {
    const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/map-labels`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ map, name }),
    });
    const payload = await response.json().catch(() => ({})) as { error?: string };
    if (!response.ok) {
      setMessage(payload.error ?? "지도 이름을 저장하지 못했어요.");
      return false;
    }
    setLabels((current) => ({ ...current, [map]: name.trim() }));
    return true;
  };

  const shownName = (map: string) => labels[map] ?? maps.find((item) => item.id === map)?.name ?? map.replace(/\.ya?ml$/, "");
  const current = inUse && maps.some((item) => item.id === inUse) ? inUse : "";
  // The map being made: named here before AutoSLAM started, not listed by the robot yet.
  const making = Object.keys(labels).filter((file) => !maps.some((item) => item.id === file)).sort().at(-1) ?? "";

  const result = record(autoslam?.result);
  const made = yamlFields(result.result_yaml);
  const madeFile = made.map_yaml ? made.map_yaml.split("/").at(-1) ?? "" : "";
  const done = autoslam?.state === "SUCCEEDED" && madeFile && maps.some((item) => item.id === madeFile) &&
    madeFile !== current ? madeFile : "";
  // After a stop or failure the robot is on the empty default map until a map is chosen.
  const failed = autoslam && ["ABORTED", "REJECTED", "ERROR"].includes(autoslam.state) && !current;
  const stopped = autoslam?.state === "CANCELED" && !current;
  const progress = yamlFields(record(autoslam?.feedback).feedback_yaml);

  const canStart = isOwner && online && running && !switching && !mapping && !driveActive &&
    servers.autoslam === true && !commandActive && !busy;
  const startMapping = async () => {
    const { stem } = newMapNames(new Date(), new Set([...maps.map((item) => item.id), ...Object.keys(labels)]));
    const name = draftName.trim();
    setMessage("");
    if (!(await saveLabel(`${stem}.yaml`, name))) return;
    await sendCommand("mission_start", { capability: "autoslam", arguments: { map_name: stem } });
  };
  // 지도를 바꾸면 말벗이 위치를 찾으며 제자리에서 돌 수 있다(malbut_bringup README · 저장 지도 주행).
  const switchToMap = (map: string) => {
    if (window.confirm(`'${shownName(map)}' 지도로 바꿀까요? 말벗이 위치를 찾으며 제자리에서 한 바퀴 돌 수 있어요. 주변을 비워 주세요.`)) {
      void sendCommand("runtime_start", { mode: "navigation", map });
    }
  };
  const deleteMap = (map: string) => {
    if (window.confirm(`'${shownName(map)}' 지도를 지울까요? 이 지도의 방·구역도 함께 지워지고 되돌릴 수 없어요.`)) {
      void sendCommand("map_delete", { map });
    }
  };

  const banner = !running ? {
    tone: "neutral", title: "주행 시스템이 꺼져 있어요",
    text: "지도 만들기와 지도 바꾸기는 주행 시스템이 켜져 있을 때 쓸 수 있어요. 말벗을 다시 켜면 자동으로 켜져요.",
  } : switching ? {
    tone: "info", title: "지도를 바꾸고 있어요",
    text: "새 지도에서 말벗의 위치를 찾는 중이에요. 그동안 목적지 보내기·순찰은 잠시 쓸 수 없어요.",
  } : done ? {
    tone: "ok", title: "새 지도를 만들었어요",
    // After AutoSLAM the robot is on the empty default map until a saved map is chosen.
    text: `${shownName(done)}${made.known_area_m2 ? ` · 알아낸 면적 ${Math.round(Number(made.known_area_m2))}㎡` : ""}. 이 지도를 쓰려면 고르세요. 고르기 전까지는 빈 지도라 목적지 보내기·순찰을 쓸 수 없어요.`,
  } : failed ? {
    tone: "danger", title: "지도를 만들지 못했어요",
    text: "그린 지도는 저장되지 않았어요. 지금은 빈 지도라, 다시 시작하거나 아래 목록에서 쓰던 지도를 골라 주세요.",
  } : stopped ? {
    tone: "neutral", title: "지도 만들기를 멈췄어요",
    text: "그린 지도는 저장되지 않았어요. 지금은 빈 지도라, 아래 목록에서 쓰던 지도를 골라 주세요.",
  } : null;
  const sorted = [...maps].sort((left, right) => (right.savedAt ?? "").localeCompare(left.savedAt ?? ""));

  return (
    <>
      <article className="ui-card ui-map-summary">
        <div>
          <span>지금 쓰는 지도</span>
          <strong>{!running ? "—" : switching ? "바꾸는 중" : current ? shownName(current) : "없음 (빈 기본 지도)"}</strong>
        </div>
        <div>
          <span>저장된 방</span>
          <span>{running && !switching && current ? `${roomCount}곳` : "—"}</span>
        </div>
      </article>
      {banner && (
        <div className={`ui-map-sync is-${banner.tone}`} role="status">
          <strong>{banner.title}</strong>
          <span>{banner.text}</span>
          {done && (
            <button type="button" className="ui-button ui-small ui-map-sync-action" onClick={() => switchToMap(done)}
              disabled={!isOwner || !online || commandActive || busy}>이 지도 쓰기</button>
          )}
        </div>
      )}
      {message && <p className="ui-info" role="status">{message}</p>}

      {mapping ? (
        <article className="ui-card ui-map-mapping is-real-robot">
          <span className="ui-badge is-accent">지도 만드는 중{making ? ` · ${shownName(making)}` : ""}</span>
          <strong className="ui-map-status-title">{STEP_COPY[progress.state] ?? "준비하고 있어요"}</strong>
          <span>
            알아낸 면적 {Math.round(Number(progress.known_area_m2) || 0)}㎡ · 아직 안 가 본 곳 {Number(progress.frontier_count) || 0}군데
          </span>
          <p className="ui-note">다 둘러보면 말벗이 알아서 저장해요. 손이 닿지 않는 구석이 조금 남아도 저장해요.</p>
          {/* The robot pauses fall detection while mapping: a moving camera reads as false falls. */}
          <p className="ui-note">지도를 만드는 동안 낙상 감지는 잠시 꺼져요. 다 만들거나 멈추면 다시 켜져요.</p>
          <button type="button" className="ui-button is-danger-line" onClick={() => void sendCommand("mission_cancel")}
            disabled={!isOwner || !online || autoslam?.state === "CANCELING" || commandActive || busy}>중지</button>
          <p className="ui-note is-danger">중지하면 지금까지 그린 지도는 저장되지 않아요.</p>
        </article>
      ) : (
        <article className="ui-card">
          <h2>새 지도 만들기</h2>
          <p className="ui-hint">말벗이 집 안을 스스로 둘러보며 지도를 만들어요. 다 만들 때까지 지금 쓰는 지도와 방·구역은 그대로이고, 다 만든 뒤 아래 목록에서 골라 쓰면 돼요.</p>
          <label className="ui-field">
            <span>지도 이름</span>
            <input value={draftName} maxLength={40} onChange={(event) => setDraftName(event.target.value)} />
          </label>
          <small className="ui-caption">만든 날짜·시간으로 채워져 있어요. 나중에 목록에서도 바꿀 수 있어요.</small>
          {running && driveActive && <p className="ui-note">다른 주행을 멈춘 뒤 지도를 만들 수 있어요.</p>}
          {running && servers.autoslam !== true && <p className="ui-note">말벗이 아직 지도 만들기를 준비하고 있어요.</p>}
          <button type="button" className="ui-button is-strong" onClick={() => void startMapping()}
            disabled={!canStart || !draftName.trim()}>지도 만들기 시작</button>
        </article>
      )}

      <article className="ui-card ui-map-list robot-saved-maps">
        <h2>저장 지도 {maps.length}개</h2>
        {sorted.map((map) => {
          const used = map.id === current && !switching;
          const canChange = isOwner && online && running && !used && !mapping && !switching && !commandActive && !busy;
          return editing?.map === map.id ? (
            <div key={map.id} className="robot-saved-map is-editing">
              <label className="ui-field">
                <span>지도 이름</span>
                <input value={editing.name} maxLength={40} autoFocus
                  onChange={(event) => setEditing({ map: map.id, name: event.target.value })} />
              </label>
              <div className="ui-two-buttons">
                <button type="button" className="ui-button ui-small" onClick={() => setEditing(null)}>취소</button>
                <button type="button" className="ui-button ui-small is-strong" disabled={!editing.name.trim() || busy}
                  onClick={() => void saveLabel(map.id, editing.name).then((saved) => saved && setEditing(null))}>이름 저장</button>
              </div>
            </div>
          ) : (
            <div key={map.id} className="robot-saved-map">
              <div className="robot-saved-map-head">
                <span><strong>{shownName(map.id)}</strong><small>{madeCopy(map.savedAt, now)}</small></span>
                {used && <span className="ui-badge is-ok">사용 중</span>}
              </div>
              <div className="robot-saved-map-actions">
                {!used && <button type="button" className="ui-button ui-small" onClick={() => switchToMap(map.id)} disabled={!canChange}>이 지도 쓰기</button>}
                <button type="button" className="ui-button ui-small" disabled={!isOwner}
                  onClick={() => setEditing({ map: map.id, name: shownName(map.id) })}>이름 바꾸기</button>
                {!used && <button type="button" className="ui-button ui-small is-danger-line" onClick={() => deleteMap(map.id)} disabled={!canChange}>삭제</button>}
              </div>
            </div>
          );
        })}
        <small className="ui-caption">지도를 지우면 그 지도의 방·구역도 함께 지워져요. 사용 중인 지도는 지울 수 없어요.</small>
      </article>
    </>
  );
}

/** 지도 탭 › 직접 움직이기 (목업 19번): 개발자 화면 패드와 같은 입력 처리. */
export function MapDrivePad({ drive, enabled }: {
  drive: (velocity: ManualVelocity) => Promise<string>;
  enabled: boolean;
}) {
  const { padProps, knob, active, sent, error } = useManualDrive(drive, enabled);
  return (
    <article className="ui-card ui-map-drive">
      <h2>직접 움직이기</h2>
      <span className="ui-caption">소유자만 볼 수 있어요.</span>
      <div {...padProps} className={`ui-map-drive-pad${enabled ? "" : " is-disabled"}${active ? " is-active" : ""}`}
        role="application" aria-label="직접 움직이기 패드: 누른 채 끄는 방향으로 움직여요. 방향키나 W·A·S·D로도 움직여요.">
        <span className="is-front" aria-hidden="true">앞</span>
        <span className="is-back" aria-hidden="true">뒤</span>
        <span className="is-left" aria-hidden="true">왼쪽 회전</span>
        <span className="is-right" aria-hidden="true">오른쪽 회전</span>
        <i aria-hidden="true" style={{ transform: `translate(${(knob.x * 62).toFixed(1)}px, ${(knob.y * 62).toFixed(1)}px)` }} />
      </div>
      <p className={active ? "ui-map-drive-status is-moving" : "ui-map-drive-status"} role="status">
        {!enabled ? "말벗이 연결돼 있고 주행 시스템이 켜져 있을 때 움직일 수 있어요."
          : active ? `${sent.vx > 0 ? "앞으로" : sent.vx < 0 ? "뒤로" : "제자리에서"} 움직이는 중 · ${Math.abs(sent.vx).toFixed(2)}m/s`
            : "멈춤"}
      </p>
      {error && <p className="ui-note is-danger" role="alert">말벗에 전달하지 못했어요: {error}</p>}
      <p className="ui-note">패드를 누른 채 끄는 만큼 빨라져요(최대 0.15m/s). 손을 떼면 바로 멈춰요. 컴퓨터에서는 패드를 누른 뒤 방향키나 W·A·S·D로도 움직여요. 처음 움직이면 하던 자율 이동·순찰·따라가기는 멈춰요. 장애물 앞에서는 말벗이 스스로 멈춰요.</p>
    </article>
  );
}
