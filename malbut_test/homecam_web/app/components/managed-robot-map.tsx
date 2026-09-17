"use client";

import { useEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";
import type { RobotSnapshot } from "./robot-map-panel";
import styles from "./managed-robot-map.module.css";

type MapSnapshot = NonNullable<RobotSnapshot["map"]>;
type Point = { x: number; y: number };
type Frame = { url: string; map: Pick<MapSnapshot, "mapId" | "revision" | "geometry">; deviceId: string };
type Props = {
  deviceId: string;
  map: MapSnapshot;
  pose: NonNullable<RobotSnapshot["state"]>["pose"];
  goal: Point | null;
  selectable: boolean;
  onSelect: (point: Point & { mapId: string }) => void;
};

export function ManagedRobotMap({ deviceId, map, pose, goal, selectable, onSelect }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 640, height: 480 });
  const [frame, setFrame] = useState<Frame | null>(null);
  const [error, setError] = useState<{ mapId: string; message: string } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const drag = useRef<{ id: number; start: Point; pan: Point; moved: boolean } | null>(null);
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

  function toScreen(point: Point) {
    if (!geometry) return null;
    const dx = point.x - geometry.originX;
    const dy = point.y - geometry.originY;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    const x = (cosine * dx + sine * dy) / geometry.resolution;
    const y = geometry.height - (-sine * dx + cosine * dy) / geometry.resolution;
    if (x < 0 || x > geometry.width || y < 0 || y > geometry.height) return null;
    return { x: left + x * scale, y: top + y * scale };
  }

  function zoomBy(factor: number) {
    setView((current) => {
      const zoom = Math.max(0.5, Math.min(12, current.zoom * factor));
      return { zoom, x: current.x * zoom / current.zoom, y: current.y * zoom / current.zoom };
    });
  }

  function pointerDown(event: PointerEvent<SVGSVGElement>) {
    if (event.button !== 0 || drag.current) return;
    drag.current = { id: event.pointerId, start: { x: event.clientX, y: event.clientY }, pan: view, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: PointerEvent<SVGSVGElement>) {
    const current = drag.current;
    if (!current || current.id !== event.pointerId) return;
    const x = event.clientX - current.start.x;
    const y = event.clientY - current.start.y;
    if (Math.hypot(x, y) > 5) current.moved = true;
    if (current.moved) setView((previous) => ({ ...previous, x: current.pan.x + x, y: current.pan.y + y }));
  }

  function pointerUp(event: PointerEvent<SVGSVGElement>) {
    const current = drag.current;
    if (!current || current.id !== event.pointerId) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (current.moved || !selectable || !geometry || !visible) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const cellX = (event.clientX - bounds.left - left) / scale;
    const cellY = geometry.height - (event.clientY - bounds.top - top) / scale;
    if (cellX < 0 || cellX > geometry.width || cellY < 0 || cellY > geometry.height) return;
    const x = cellX * geometry.resolution;
    const y = cellY * geometry.resolution;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    onSelect({
      x: geometry.originX + cosine * x - sine * y,
      y: geometry.originY + sine * x + cosine * y,
      mapId: visible.map.mapId,
    });
  }

  const robot = pose ? toScreen(pose) : null;
  const target = goal ? toScreen(goal) : null;
  const message = error?.mapId === map.mapId ? error.message : !visible ? "지도를 불러오는 중…" : null;

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
          onPointerCancel={() => { drag.current = null; }}
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
          {target && (
            <g transform={`translate(${target.x} ${target.y})`} aria-label="선택한 목적지">
              <circle r="10" fill="var(--goal-600, #e07a28)" stroke="white" strokeWidth="2.5" />
              <path d="M-5 0H5M0-5V5" stroke="white" strokeWidth="2" />
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
      <p className={styles.hint}>
        {selectable ? "지점을 선택한 뒤 별도로 이동을 요청하세요. 지도 선택만으로는 주행하지 않습니다." : "드래그하여 지도 이동 · +/−로 확대·축소"}
      </p>
    </div>
  );
}
