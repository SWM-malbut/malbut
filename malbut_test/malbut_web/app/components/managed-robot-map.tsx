"use client";

import { useEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";
import type { RobotSnapshot } from "./robot-map-panel";
import type { MapPoint, MapPose, ZoneBehavior, ZoneDraft } from "./managed-robot-workspace";
import styles from "./managed-robot-map.module.css";

type MapSnapshot = NonNullable<RobotSnapshot["map"]>;
type Point = { x: number; y: number };
type Frame = { url: string; map: Pick<MapSnapshot, "mapId" | "revision" | "geometry">; deviceId: string };
export type ManagedMapTool = "view" | "navigate" | "pose" | "zones";
type Props = {
  deviceId: string;
  map: MapSnapshot;
  pose: NonNullable<RobotSnapshot["state"]>["pose"];
  goal: MapPoint | null;
  initialPose: MapPose | null;
  zones: ZoneDraft[];
  drawing: Array<[number, number]>;
  selectedZone: number | null;
  tool: ManagedMapTool;
  interactive: boolean;
  onPick: (point: MapPoint) => void;
  onPose: (pose: MapPose) => void;
};

const ZONE_STYLE: Record<ZoneBehavior, { fill: string; stroke: string }> = {
  restricted: { fill: "rgb(220 38 38 / 0.32)", stroke: "#b91c1c" },
  avoid: { fill: "rgb(234 179 8 / 0.32)", stroke: "#a16207" },
  allow: { fill: "rgb(34 197 94 / 0.22)", stroke: "#15803d" },
};

export function ManagedRobotMap({
  deviceId, map, pose, goal, initialPose, zones, drawing, selectedZone, tool, interactive, onPick, onPose,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 640, height: 480 });
  const [frame, setFrame] = useState<Frame | null>(null);
  const [error, setError] = useState<{ mapId: string; message: string } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [aiming, setAiming] = useState<MapPose | null>(null);
  const drag = useRef<{ id: number; start: Point; pan: Point; moved: boolean; aim: MapPoint | null } | null>(null);
  const { mapId, revision } = map;
  const { width, height, resolution, originX, originY, originYaw } = map.geometry;

  useEffect(() => {
    if (!host.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width && entry.contentRect.height) {
        setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
      }
    });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let pendingUrl: string | null = null;
    // A frame owns its geometry: never place a new map origin over an old image.
    const requestedMap = {
      mapId,
      revision,
      geometry: { width, height, resolution, originX, originY, originYaw },
    };
    async function load() {
      try {
        const response = await fetch(
          `/api/devices/${encodeURIComponent(deviceId)}/robot/map?revision=${encodeURIComponent(requestedMap.revision)}`,
          { signal: controller.signal },
        );
        if (!response.ok) throw new Error("지도 갱신 실패 · 마지막 지도를 표시합니다.");
        pendingUrl = URL.createObjectURL(await response.blob());
        const image = new window.Image();
        image.src = pendingUrl;
        await image.decode();
        if (controller.signal.aborted) return;
        setFrame({ url: pendingUrl, map: requestedMap, deviceId });
        pendingUrl = null; // The displayed frame releases its own URL after replacement.
        setError(null);
      } catch {
        if (!controller.signal.aborted) {
          setError({ mapId: requestedMap.mapId, message: "지도 갱신 실패 · 수신된 지도가 있으면 유지합니다." });
        }
      } finally {
        if (pendingUrl) URL.revokeObjectURL(pendingUrl);
      }
    }
    void load();
    return () => controller.abort();
  }, [deviceId, mapId, revision, width, height, resolution, originX, originY, originYaw]);

  useEffect(() => {
    if (!frame) return;
    return () => URL.revokeObjectURL(frame.url);
  }, [frame]);

  const visible = frame?.deviceId === deviceId && frame.map.mapId === map.mapId ? frame : null;
  const geometry = visible?.map.geometry;
  const scale = geometry
    ? Math.min(Math.max(1, size.width - 32) / geometry.width, Math.max(1, size.height - 32) / geometry.height) * view.zoom
    : 1;
  const left = size.width / 2 + view.x - (geometry?.width ?? 0) * scale / 2;
  const top = size.height / 2 + view.y - (geometry?.height ?? 0) * scale / 2;

  /** Map metres to screen pixels without clipping, for polygons crossing the edge. */
  function project(point: Point) {
    if (!geometry) return null;
    const dx = point.x - geometry.originX;
    const dy = point.y - geometry.originY;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    const x = (cosine * dx + sine * dy) / geometry.resolution;
    const y = geometry.height - (-sine * dx + cosine * dy) / geometry.resolution;
    return { x: left + x * scale, y: top + y * scale, inside: x >= 0 && x <= geometry.width && y >= 0 && y <= geometry.height };
  }

  function toScreen(point: Point) {
    const result = project(point);
    return result?.inside ? result : null;
  }

  function toWorld(event: PointerEvent<SVGSVGElement>): MapPoint | null {
    if (!geometry || !visible) return null;
    const bounds = event.currentTarget.getBoundingClientRect();
    const cellX = (event.clientX - bounds.left - left) / scale;
    const cellY = geometry.height - (event.clientY - bounds.top - top) / scale;
    if (cellX < 0 || cellX > geometry.width || cellY < 0 || cellY > geometry.height) return null;
    const x = cellX * geometry.resolution;
    const y = cellY * geometry.resolution;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    return { x: geometry.originX + cosine * x - sine * y, y: geometry.originY + sine * x + cosine * y };
  }

  function zoomBy(factor: number) {
    setView((current) => {
      const zoom = Math.max(0.5, Math.min(12, current.zoom * factor));
      return { zoom, x: current.x * zoom / current.zoom, y: current.y * zoom / current.zoom };
    });
  }

  function pointerDown(event: PointerEvent<SVGSVGElement>) {
    if (event.button !== 0 || drag.current) return;
    // Pose tool: press on the robot's position, drag toward its heading.
    const aim = interactive && tool === "pose" ? toWorld(event) : null;
    drag.current = { id: event.pointerId, start: { x: event.clientX, y: event.clientY }, pan: view, moved: false, aim };
    if (aim) setAiming({ ...aim, yaw: initialPose?.yaw ?? pose?.yaw ?? 0 });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: PointerEvent<SVGSVGElement>) {
    const current = drag.current;
    if (!current || current.id !== event.pointerId) return;
    const x = event.clientX - current.start.x;
    const y = event.clientY - current.start.y;
    if (Math.hypot(x, y) > 5) current.moved = true;
    if (!current.moved) return;
    if (current.aim) {
      const target = toWorld(event);
      const aim = current.aim;
      if (target) setAiming({ ...aim, yaw: Math.atan2(target.y - aim.y, target.x - aim.x) });
      return;
    }
    setView((previous) => ({ ...previous, x: current.pan.x + x, y: current.pan.y + y }));
  }

  function pointerUp(event: PointerEvent<SVGSVGElement>) {
    const current = drag.current;
    if (!current || current.id !== event.pointerId) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (current.aim) {
      if (aiming) onPose(aiming);
      setAiming(null);
      return;
    }
    if (current.moved || !interactive || (tool !== "navigate" && tool !== "zones")) return;
    const point = toWorld(event);
    if (point) onPick(point);
  }

  const robot = pose ? toScreen(pose) : null;
  const target = goal ? toScreen(goal) : null;
  const shownPose = aiming ?? initialPose;
  const poseMarker = shownPose ? toScreen(shownPose) : null;
  const draft = drawing.map(([x, y]) => project({ x, y })).filter((point) => point !== null);
  const message = error?.mapId === map.mapId ? error.message : !visible ? "지도를 불러오는 중…" : null;
  const hint = !interactive ? "드래그하여 지도 이동 · +/−로 확대·축소"
    : tool === "navigate" ? "지점을 선택한 뒤 별도로 이동을 요청하세요. 지도 선택만으로는 주행하지 않습니다."
      : tool === "pose" ? "로봇이 실제로 있는 곳을 누른 채 바라보는 방향으로 끌었다 놓으세요."
        : tool === "zones" ? "지도를 눌러 구역 꼭짓점을 찍고, 오른쪽에서 그리기를 완료하세요. 기존 구역을 누르면 선택됩니다."
          : "드래그하여 지도 이동 · +/−로 확대·축소";

  return (
    <div className={styles.container}>
      <div className={styles.toolbar}>
        <span>{geometry ? `${geometry.width} × ${geometry.height} · ${(geometry.resolution * 100).toFixed(1)}cm/칸` : "지도 수신 대기"}</span>
        <div className={styles.buttons}>
          <button type="button" onClick={() => zoomBy(1 / 1.4)} aria-label="지도 축소">−</button>
          <button type="button" onClick={() => setView({ zoom: 1, x: 0, y: 0 })}>전체 보기</button>
          <button type="button" onClick={() => zoomBy(1.4)} aria-label="지도 확대">+</button>
        </div>
      </div>
      <div ref={host} className={styles.viewport}>
        <svg
          className={styles.canvas}
          viewBox={`0 0 ${size.width} ${size.height}`}
          aria-label="실시간 로봇 지도. 드래그하여 이동하고 확대 버튼으로 자세히 볼 수 있습니다."
          onPointerDown={pointerDown}
          onPointerMove={pointerMove}
          onPointerUp={pointerUp}
          onPointerCancel={() => { drag.current = null; setAiming(null); }}
          onLostPointerCapture={() => { drag.current = null; }}
        >
          {visible && geometry && (
            <image
              href={visible.url}
              x={left}
              y={top}
              width={geometry.width * scale}
              height={geometry.height * scale}
              className={styles.image}
            />
          )}
          {geometry && zones.map((zone, index) => {
            const points = zone.points.map(([x, y]) => project({ x, y })).filter((point) => point !== null);
            return (
              <polygon
                key={index}
                points={points.map((point) => `${point.x},${point.y}`).join(" ")}
                fill={ZONE_STYLE[zone.behavior].fill}
                stroke={ZONE_STYLE[zone.behavior].stroke}
                strokeWidth={index === selectedZone ? 3.5 : 1.5}
                strokeDasharray={index === selectedZone ? "6 4" : undefined}
                aria-label={zone.behavior === "restricted" ? "진입 금지 구역" : zone.behavior === "avoid" ? "우회 권장 구역" : "통행 허용 구역"}
              />
            );
          })}
          {draft.length > 0 && (
            <g aria-label="그리는 중인 구역">
              <polyline
                points={[...draft, ...(draft.length >= 3 ? [draft[0]] : [])].map((point) => `${point.x},${point.y}`).join(" ")}
                fill="none"
                stroke="#7c3aed"
                strokeWidth="2"
                strokeDasharray="5 4"
              />
              {draft.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="5" fill="#7c3aed" stroke="white" strokeWidth="1.5" />)}
            </g>
          )}
          {target && (
            <g transform={`translate(${target.x} ${target.y})`} aria-label="선택한 목적지">
              <circle r="10" fill="var(--goal-600, #e07a28)" stroke="white" strokeWidth="2.5" />
              <path d="M-5 0H5M0-5V5" stroke="white" strokeWidth="2" />
            </g>
          )}
          {poseMarker && shownPose && geometry && (
            <g transform={`translate(${poseMarker.x} ${poseMarker.y}) rotate(${-(shownPose.yaw - geometry.originYaw) * 180 / Math.PI})`} aria-label="지정한 현재 위치와 방향">
              <circle r="12" fill="none" stroke="#7c3aed" strokeWidth="3" />
              <path d="M0 0H26" stroke="#7c3aed" strokeWidth="3" />
              <path d="M26 0L17-6L17 6Z" fill="#7c3aed" />
            </g>
          )}
          {robot && pose && geometry && (
            <g transform={`translate(${robot.x} ${robot.y}) rotate(${-(pose.yaw - geometry.originYaw) * 180 / Math.PI})`} aria-label="로봇 위치와 방향">
              <circle r="13" fill="var(--robot-600, #2f6fe0)" stroke="white" strokeWidth="2.5" />
              <path d="M8 0L-5-6L-2 0L-5 6Z" fill="white" />
            </g>
          )}
        </svg>
        {message && <p className={styles.status} role="status">{message}</p>}
      </div>
      <p className={styles.hint}>{hint}</p>
    </div>
  );
}
