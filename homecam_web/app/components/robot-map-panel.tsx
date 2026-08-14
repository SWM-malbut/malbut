"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  CheckCircle,
  MapTrifold,
  StopCircle,
  WarningCircle,
} from "@phosphor-icons/react";
import type { HomecamDevice } from "./homecam-dashboard";

type RobotSnapshot = {
  online: boolean;
  state: {
    state: string;
    message: string;
    pose: { x: number; y: number; yaw: number } | null;
    localization: { state: string; tfAgeS: number | null };
    nav2: Record<string, string>;
    target: Record<string, unknown> | null;
    observedAt: string;
  } | null;
  map: {
    finalized: boolean;
    revision: string;
    mapId: string;
    geometry: {
      width: number;
      height: number;
      resolution: number;
      originX: number;
      originY: number;
      originYaw: number;
    };
    updatedAt: string;
  } | null;
  command: {
    id: string;
    operation: RobotOperation;
    status: "queued" | "claimed" | "completed" | "failed";
    requestedAt: string;
    result: unknown;
  } | null;
};

type RobotOperation = "start" | "finish" | "cancel" |
  "navigation_preview" | "navigation_start" | "navigation_cancel";

export function RobotMapPanel({ device }: { device: HomecamDevice | null }) {
  const [snapshot, setSnapshot] = useState<RobotSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [navigationPreview, setNavigationPreview] = useState<Record<string, unknown> | null>(null);
  const [previewExpiresAt, setPreviewExpiresAt] = useState(0);
  const [clockNow, setClockNow] = useState(0);
  const [robotTrail, setRobotTrail] = useState<Array<[number, number]>>([]);
  const processedCommand = useRef("");
  const trailSession = useRef("");

  const load = useCallback(async (quiet = false) => {
    if (!device) {
      setSnapshot(null);
      setLoading(false);
      return;
    }
    if (!quiet) setLoading(true);
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(device.id)}/robot`,
        { cache: "no-store" },
      );
      const payload = await response.json().catch(() => ({})) as RobotSnapshot & { error?: string };
      if (!response.ok) throw new Error(payload.error ?? "로봇 지도를 불러오지 못했습니다.");
      setSnapshot(payload);
      setClockNow(Date.now());
      const command = payload.command;
      if (command && command.id !== processedCommand.current && ["completed", "failed"].includes(command.status)) {
        processedCommand.current = command.id;
        if (command.operation === "navigation_preview" && command.status === "completed" && isRecord(command.result)) {
          setNavigationPreview(command.result);
          const ttl = typeof command.result.expires_in_s === "number"
            ? command.result.expires_in_s
            : 30;
          setPreviewExpiresAt(Date.now() + Math.max(0, ttl - 2) * 1_000);
          setNotice("");
        } else if (command.status === "failed") {
          const result = isRecord(command.result) ? command.result : {};
          setNotice(typeof result.error === "string" ? result.error : "로봇이 명령을 완료하지 못했습니다.");
        } else if (command.operation === "navigation_start") {
          setNavigationPreview(null);
          setPreviewExpiresAt(0);
          setNotice("선택한 목적지로 이동을 시작했습니다.");
        }
      }
      if (!quiet) setNotice("");
    } catch (error) {
      if (!quiet) setNotice(error instanceof Error ? error.message : "로봇 지도를 불러오지 못했습니다.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [device]);

  useEffect(() => {
    window.queueMicrotask(() => void load());
    const timer = window.setInterval(() => void load(true), 1_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const marker = useMemo(() => {
    const pose = snapshot?.state?.pose;
    const geometry = snapshot?.map?.geometry;
    if (!pose || !geometry) return null;
    const dx = pose.x - geometry.originX;
    const dy = pose.y - geometry.originY;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    const mapX = cosine * dx + sine * dy;
    const mapY = -sine * dx + cosine * dy;
    const left = mapX / (geometry.width * geometry.resolution) * 100;
    const top = (1 - mapY / (geometry.height * geometry.resolution)) * 100;
    if (left < 0 || left > 100 || top < 0 || top > 100) return null;
    return { left, top, heading: -(pose.yaw - geometry.originYaw) * 180 / Math.PI };
  }, [snapshot]);

  const activeCommand = snapshot?.command &&
    ["queued", "claimed"].includes(snapshot.command.status);
  const mapping = snapshot?.state && [
    "waiting_for_map", "waiting_for_navigation", "exploring", "navigating", "review", "saving",
  ].includes(snapshot.state.state);
  const isOwner = device?.role === "owner";
  const runtimeMode = snapshot?.state?.nav2.runtime_mode;
  const navigation = snapshot?.state?.target;
  const navigationDriving = navigation?.state === "driving" || navigation?.state === "canceling";
  const navigationSession = typeof navigation?.session_id === "string" ? navigation.session_id : "";
  const previewToken = previewExpiresAt > clockNow && typeof navigationPreview?.preview_token === "string"
    ? navigationPreview.preview_token
    : "";
  const previewPath = isRecord(navigationPreview?.path) && Array.isArray(navigationPreview.path.points)
    ? navigationPreview.path.points as unknown[]
    : [];
  const livePath = isRecord(navigation?.path) && Array.isArray(navigation.path.points)
    ? navigation.path.points as unknown[]
    : [];
  const previewPolyline = pathToPolyline(previewPath, snapshot?.map?.geometry);
  const livePolyline = pathToPolyline(livePath, snapshot?.map?.geometry);
  const trailPolyline = pathToPolyline(robotTrail, snapshot?.map?.geometry);
  const visibleGoal = isRecord(navigationPreview?.resolved)
    ? navigationPreview.resolved
    : isRecord(navigation?.goal)
      ? navigation.goal
      : navigation;

  useEffect(() => {
    const pose = snapshot?.state?.pose;
    if (!navigationDriving || !navigationSession || !pose) return;
    if (trailSession.current !== navigationSession) {
      trailSession.current = navigationSession;
      setRobotTrail([[pose.x, pose.y]]);
      return;
    }
    setRobotTrail((current) => {
      const previous = current.at(-1);
      if (previous && Math.hypot(pose.x - previous[0], pose.y - previous[1]) < 0.04) {
        return current;
      }
      return [...current.slice(-499), [pose.x, pose.y]];
    });
  }, [navigationDriving, navigationSession, snapshot?.state?.pose]);

  const sendCommand = async (operation: RobotOperation, commandPayload: Record<string, unknown> = {}) => {
    if (!device || busy) return;
    setBusy(true);
    setNotice("");
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(device.id)}/robot/commands`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ operation, payload: commandPayload }),
        },
      );
      const payload = await response.json().catch(() => ({})) as { error?: string };
      if (!response.ok) throw new Error(payload.error ?? "지도 명령을 전달하지 못했습니다.");
      setNotice(
        operation === "start" ? "로봇에 지도 생성을 요청했습니다."
          : operation === "finish" ? "탐색 완료와 지도 저장을 요청했습니다."
            : operation === "cancel" ? "지도 생성을 중지하도록 요청했습니다."
              : operation === "navigation_preview" ? "현재 costmap으로 안전한 경로를 확인하고 있습니다."
                : operation === "navigation_start" ? "출발 전에 경로를 다시 확인하고 있습니다."
                  : "주행 취소를 요청했습니다.",
      );
      await load(true);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "지도 명령을 전달하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const selectDestination = (event: React.MouseEvent<HTMLDivElement>) => {
    const geometry = snapshot?.map?.geometry;
    if (!geometry || runtimeMode !== "navigation" || !isOwner || !snapshot?.online || navigationDriving) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const fractionX = (event.clientX - bounds.left) / bounds.width;
    const fractionY = (event.clientY - bounds.top) / bounds.height;
    const localX = fractionX * geometry.width * geometry.resolution;
    const localY = (1 - fractionY) * geometry.height * geometry.resolution;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    const x = geometry.originX + cosine * localX - sine * localY;
    const y = geometry.originY + sine * localX + cosine * localY;
    setNavigationPreview(null);
    setPreviewExpiresAt(0);
    void sendCommand("navigation_preview", { x, y });
  };

  return (
    <section className="homecam-section robot-map-section" aria-labelledby="robot-map-title">
      <div className="homecam-section-heading">
        <div>
          <span>SLAM MAP · LIVE POSE</span>
          <h1 id="robot-map-title">우리 집 지도</h1>
        </div>
        <button type="button" className="homecam-refresh-button" onClick={() => void load()} disabled={loading}>
          <ArrowClockwise size={15} weight="bold" aria-hidden="true" />
          {loading ? "불러오는 중" : "새로고침"}
        </button>
      </div>

      <div className="robot-map-layout">
        <div className="robot-map-card">
          {snapshot?.map ? (
            <div
              className={`robot-map-canvas ${runtimeMode === "navigation" ? "is-navigation" : ""}`}
              style={{ aspectRatio: `${snapshot.map.geometry.width} / ${snapshot.map.geometry.height}` }}
              onClick={selectDestination}
            >
              <Image
                src={`/api/devices/${encodeURIComponent(device?.id ?? "")}/robot/map?revision=${encodeURIComponent(snapshot.map.revision)}`}
                alt="로봇이 생성한 우리 집 지도"
                fill
                unoptimized
                sizes="(max-width: 820px) 100vw, 70vw"
                priority
              />
              {previewPolyline && (
                <svg className="robot-map-path is-preview" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                  <polyline points={previewPolyline} />
                </svg>
              )}
              {livePolyline && (
                <svg className="robot-map-path is-live" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                  <polyline points={livePolyline} />
                </svg>
              )}
              {trailPolyline && (
                <svg className="robot-map-path is-trail" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                  <polyline points={trailPolyline} />
                </svg>
              )}
              {isRecord(visibleGoal) &&
                typeof visibleGoal.x === "number" &&
                typeof visibleGoal.y === "number" && (() => {
                  const goal = worldToPercent(
                    visibleGoal.x,
                    visibleGoal.y,
                    snapshot.map.geometry,
                  );
                  return <span className={`robot-map-goal ${runtimeMode === "mapping" ? "is-exploration" : ""}`} style={{ left: `${goal.left}%`, top: `${goal.top}%` }} aria-label={runtimeMode === "mapping" ? "다음 자동 탐색 목표" : "선택한 목적지"} />;
                })()}
              {marker && (
                <span
                  className="robot-map-marker"
                  aria-label="말벗 현재 위치"
                  style={{ left: `${marker.left}%`, top: `${marker.top}%`, transform: `translate(-50%, -50%) rotate(${marker.heading}deg)` }}
                >
                  <span />
                </span>
              )}
            </div>
          ) : (
            <div className="robot-map-empty">
              <MapTrifold size={44} weight="light" aria-hidden="true" />
              <strong>{loading ? "지도를 확인하고 있어요" : "아직 저장된 지도가 없어요"}</strong>
              <p>소유자가 지도 생성을 시작하고 집 안을 한 번 탐색한 뒤 완료하면 이곳에 저장됩니다.</p>
            </div>
          )}
        </div>

        <aside className="robot-map-sidebar">
          <div className="robot-map-status">
            <span className={`robot-map-online-dot ${snapshot?.online ? "is-online" : ""}`} />
            <div><small>로봇 연결</small><strong>{snapshot?.online ? "실시간 연결됨" : "오프라인"}</strong></div>
          </div>
          <div className="robot-map-status">
            {snapshot?.state?.localization.state === "ok"
              ? <CheckCircle size={24} weight="fill" aria-hidden="true" />
              : <WarningCircle size={24} weight="fill" aria-hidden="true" />}
            <div><small>현재 위치</small><strong>{snapshot?.state?.localization.state === "ok" ? "지도에 반영 중" : "위치 확인 필요"}</strong></div>
          </div>
          <p className="robot-map-message">{snapshot?.state?.message ?? "로봇에서 상태를 기다리고 있습니다."}</p>
          <div className="robot-map-actions">
            {runtimeMode === "mapping" && (!mapping ? (
              <button type="button" onClick={() => void sendCommand("start")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                <MapTrifold size={17} weight="bold" /> {snapshot?.map ? "지도 다시 만들기" : "지도 만들기 시작"}
              </button>
            ) : (
              <>
                <button type="button" onClick={() => void sendCommand("finish")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                  <CheckCircle size={17} weight="bold" /> 탐색 완료·저장
                </button>
                <button type="button" className="is-secondary" onClick={() => void sendCommand("cancel")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                  <StopCircle size={17} weight="bold" /> 중지
                </button>
              </>
            ))}
            {runtimeMode === "navigation" && previewToken && !navigationDriving && (
              <button type="button" onClick={() => void sendCommand("navigation_start", { previewToken })} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                <CheckCircle size={17} weight="bold" /> 이 위치로 이동
              </button>
            )}
            {runtimeMode === "navigation" && navigationDriving && typeof navigation?.session_id === "string" && (
              <button type="button" className="is-secondary" onClick={() => void sendCommand("navigation_cancel", { sessionId: navigation.session_id })} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                <StopCircle size={17} weight="bold" /> 주행 취소
              </button>
            )}
            {runtimeMode === "navigation" && !navigationDriving && !previewToken && (
              <button type="button" className="is-secondary" onClick={() => void sendCommand("start")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                <MapTrifold size={17} weight="bold" /> 지도 다시 만들기
              </button>
            )}
          </div>
          {runtimeMode === "navigation" && !navigationDriving && !previewToken && (
            <small className="robot-map-owner-note">지도에서 목적지를 선택하면 현재 costmap으로 안전성과 경로를 확인합니다.</small>
          )}
          {!isOwner && <small className="robot-map-owner-note">지도 생성과 목적지 이동은 소유자 계정에서만 할 수 있습니다.</small>}
          {notice && <p className="robot-map-notice" role="status">{notice}</p>}
        </aside>
      </div>
    </section>
  );
}

function worldToPercent(
  x: number,
  y: number,
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"],
) {
  const dx = x - geometry.originX;
  const dy = y - geometry.originY;
  const cosine = Math.cos(geometry.originYaw);
  const sine = Math.sin(geometry.originYaw);
  const localX = cosine * dx + sine * dy;
  const localY = -sine * dx + cosine * dy;
  return {
    left: localX / (geometry.width * geometry.resolution) * 100,
    top: (1 - localY / (geometry.height * geometry.resolution)) * 100,
  };
}

function pathToPolyline(
  points: unknown[],
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"] | undefined,
) {
  if (!geometry) return "";
  return points.flatMap((point) => {
    if (
      !Array.isArray(point) || point.length < 2 ||
      typeof point[0] !== "number" || typeof point[1] !== "number"
    ) return [];
    const mapped = worldToPercent(point[0], point[1], geometry);
    return [`${mapped.left},${mapped.top}`];
  }).join(" ");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
