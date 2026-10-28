"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";
import { editedPixels, pixelToMap, polygonError } from "../robot-map-edit";
import type { MapPoint, SavedMapEdit } from "../robot-map-edit";
import type { RobotOperation } from "../robot-contract";
import styles from "./robot-map-cleanup.module.css";

type Tool = "rectangle" | "polygon" | "pan";
type Command = { id: string; status: string; result?: { error?: string; revision?: string } };

export function RobotMapCleanup({ deviceId, map, name, online, canApply, onClose }: {
  deviceId: string; map: string; name: string; online: boolean; canApply: boolean; onClose: () => void;
}) {
  const [saved, setSaved] = useState<SavedMapEdit | null>(null);
  const [history, setHistory] = useState<MapPoint[][]>([]);
  const [cursor, setCursor] = useState(0);
  const [drawing, setDrawing] = useState<MapPoint[]>([]);
  const [tool, setTool] = useState<Tool>("rectangle");
  const [before, setBefore] = useState(false);
  const [view, setView] = useState({ zoom: 1, rotation: 0, x: 0, y: 0 });
  const [preview, setPreview] = useState({ url: "", count: 0 });
  const [source, setSource] = useState<ImageData | null>(null);
  const [pending, setPending] = useState("");
  const [message, setMessage] = useState("");
  const started = useRef(false);
  const mounted = useRef(true);
  const working = useRef(false);
  const group = useRef<SVGGElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const drag = useRef<{ start: MapPoint; root: MapPoint; x: number; y: number } | null>(null);
  const dirty = cursor > 0 || drawing.length > 0;

  const run = useCallback(async (operation: RobotOperation, payload: Record<string, unknown>) => {
    if (working.current) return;
    working.current = true;
    setPending(operation); setMessage("");
    const base = `/api/devices/${encodeURIComponent(deviceId)}/robot`;
    try {
      const response = await fetch(`${base}/commands`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ operation, payload }) });
      const body = await response.json() as { command?: Command; error?: string };
      if (!response.ok || !body.command) throw new Error(body.error ?? "요청하지 못했어요.");
      const id = body.command.id, deadline = Date.now() + 310_000;
      let completed: Command | undefined;
      while (mounted.current && Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        try {
          const receipt = await fetch(`${base}/commands`, { cache: "no-store" });
          if (!receipt.ok) continue;
          const values = await receipt.json() as { commands?: Command[] };
          const command = values.commands?.find((item) => item.id === id);
          if (command?.status === "failed") throw new Error(command.result?.error ?? "로봇에서 처리하지 못했어요.");
          if (command?.status === "completed") { completed = command; break; }
        } catch (error) {
          if (error instanceof TypeError) continue; // Keep the draft through a temporary connection loss.
          throw error;
        }
      }
      if (!mounted.current) return;
      if (!completed) throw new Error("처리 결과를 확인하지 못했어요. 로봇 상태를 확인한 뒤 다시 불러와 주세요.");
      const fetched = await fetch(`${base}/saved-maps?map=${encodeURIComponent(map)}`, { cache: "no-store" });
      const value = await fetched.json() as SavedMapEdit & { error?: string };
      if (!fetched.ok || value.map !== map || value.revision !== completed.result?.revision) {
        throw new Error(value.error ?? "최신 수정본을 확인하지 못했어요. 다시 불러와 주세요.");
      }
      setSource(null); setPreview({ url: "", count: 0 });
      setSaved(value); setHistory([]); setCursor(0); setDrawing([]); setBefore(false);
      setMessage(operation === "map_edit_apply" ? "수정 지도를 주행에 적용했어요."
        : operation === "map_edit_save" ? "수정본을 저장했어요. 주행에는 아직 적용하지 않았어요."
          : operation === "map_edit_restore" ? "원본을 수정본으로 저장했어요. 주행에 적용하려면 아래 버튼을 눌러 주세요." : "");
    } catch (error) {
      if (mounted.current) setMessage(error instanceof Error ? error.message : "연결을 확인해 주세요.");
    } finally {
      working.current = false;
      if (mounted.current) setPending("");
    }
  }, [deviceId, map]);

  useEffect(() => {
    mounted.current = true;
    if (!started.current) {
      started.current = true;
      void run("map_edit_open", { map });
    }
    return () => { mounted.current = false; };
  }, [map, run]);

  useEffect(() => {
    if (!saved) return;
    let cancelled = false;
    const image = new window.Image();
    image.onload = () => {
      if (cancelled) return;
      if (image.width !== saved.geometry.width || image.height !== saved.geometry.height) {
        setMessage("지도 이미지 크기가 맞지 않습니다. 다시 불러와 주세요."); return;
      }
      const canvas = document.createElement("canvas");
      canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return;
      context.drawImage(image, 0, 0);
      setSource(context.getImageData(0, 0, image.width, image.height));
    };
    image.onerror = () => { if (!cancelled) setMessage("지도 이미지를 읽지 못했어요."); };
    image.src = `data:image/png;base64,${saved.previewBase64}`;
    return () => { cancelled = true; };
  }, [saved]);

  useEffect(() => {
    if (!source) return;
    const { pixels, count } = editedPixels(source.data, source.width, source.height, history.slice(0, cursor));
    const canvas = document.createElement("canvas");
    canvas.width = source.width; canvas.height = source.height;
    canvas.getContext("2d")?.putImageData(new ImageData(pixels, source.width, source.height), 0, 0);
    const url = canvas.toDataURL("image/png");
    window.queueMicrotask(() => setPreview({ url, count }));
  }, [source, history, cursor]);

  useEffect(() => {
    if (!dirty && !pending) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, pending]);

  function point(event: PointerEvent<SVGSVGElement>, root = false): MapPoint {
    const matrix = (root ? svg.current : group.current)?.getScreenCTM();
    const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix?.inverse());
    if (root || !saved) return [p.x, p.y];
    return [Math.max(0, Math.min(saved.geometry.width, p.x)), Math.max(0, Math.min(saved.geometry.height, p.y))];
  }

  function add(ring: MapPoint[]) {
    const error = polygonError(ring);
    if (error) { setMessage(error); return; }
    if (cursor >= 64) { setMessage("한 번에 64개 영역까지 저장할 수 있어요."); return; }
    setHistory([...history.slice(0, cursor), ring]); setCursor(cursor + 1); setDrawing([]); setMessage("");
  }

  function down(event: PointerEvent<SVGSVGElement>) {
    if (pending || !source || event.button !== 0 || !event.isPrimary) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { start: point(event), root: point(event, true), x: view.x, y: view.y };
    if (tool === "rectangle") setDrawing([drag.current.start, drag.current.start]);
  }

  function move(event: PointerEvent<SVGSVGElement>) {
    if (!drag.current) return;
    if (tool === "pan") {
      const p = point(event, true), d = drag.current;
      setView({ ...view, x: d.x + p[0] - d.root[0], y: d.y + p[1] - d.root[1] });
    } else if (tool === "rectangle") setDrawing([drag.current.start, point(event)]);
  }

  function up(event: PointerEvent<SVGSVGElement>) {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    const p = point(event);
    if (tool === "rectangle") add([d.start, [p[0], d.start[1]], p, [d.start[0], p[1]]]);
    if (tool === "polygon") {
      if (drawing.length >= 64) setMessage("꼭짓점은 64개까지 선택할 수 있어요.");
      else setDrawing([...drawing, p]);
    }
  }

  const originalUrl = saved ? `data:image/png;base64,${saved.previewBase64}` : "";
  const geometry = saved?.geometry;
  const selected = history.slice(0, cursor);
  const reload = () => {
    if (!dirty || window.confirm("저장하지 않은 선택을 버리고 다시 불러올까요?")) void run("map_edit_open", { map });
  };

  return <section className={`ui-card ${styles.editor}`} aria-label="지도 정리">
    <div className={styles.heading}>
      <div><span className="ui-caption">지도 정리</span><h2>{name}</h2></div>
      <button type="button" className="ui-button" disabled={Boolean(pending)} onClick={() => {
        if (!dirty || window.confirm("저장하지 않은 선택을 버리고 닫을까요?")) onClose();
      }}>지도 관리로</button>
    </div>
    <p className="ui-hint">사라진 임시 장애물만 선택해 지워 주세요. 회색 미확인 공간과 진입 금지 구역은 유지돼요.</p>
    {!online && <p className="ui-note">로봇 연결이 끊겼어요. 선택 내용은 유지되며, 연결되면 저장할 수 있어요.</p>}
    <div className={styles.toolbar}>
      {([["rectangle", "사각형"], ["polygon", "다각형"], ["pan", "지도 이동"]] as const).map(([id, label]) =>
        <button type="button" className="ui-button ui-small" aria-pressed={tool === id} key={id} disabled={Boolean(pending)}
          onClick={() => { setTool(id); setDrawing([]); }}>{label}</button>)}
      <button type="button" className="ui-button ui-small" disabled={!cursor || Boolean(pending)} onClick={() => { setCursor(cursor-1); setDrawing([]); }}>실행 취소</button>
      <button type="button" className="ui-button ui-small" disabled={cursor >= history.length || Boolean(pending)} onClick={() => setCursor(cursor+1)}>다시 실행</button>
      <button type="button" className="ui-button ui-small" aria-pressed={before} onClick={() => setBefore(!before)}>{before ? "수정 후 보기" : "수정 전 보기"}</button>
    </div>
    <div className={styles.canvas}>
      {geometry && <svg ref={svg} viewBox={`0 0 ${geometry.width} ${geometry.height}`} role="img" aria-label="장애물 정리 지도"
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { drag.current = null; setDrawing([]); }}>
        <g ref={group} transform={`translate(${geometry.width/2+view.x} ${geometry.height/2+view.y}) rotate(${view.rotation}) scale(${view.zoom}) translate(${-geometry.width/2} ${-geometry.height/2})`}>
          <image href={before || !preview.url ? originalUrl : preview.url} width={geometry.width} height={geometry.height} style={{ imageRendering: "pixelated" }} />
          {!before && selected.map((ring, i) => <polygon key={i} points={ring.map((p) => p.join(",")).join(" ")}
            fill="none" stroke="#16896e" strokeWidth={2} vectorEffect="non-scaling-stroke" />)}
          {tool === "rectangle" && drawing.length === 2 && <rect x={Math.min(drawing[0][0],drawing[1][0])} y={Math.min(drawing[0][1],drawing[1][1])}
            width={Math.abs(drawing[1][0]-drawing[0][0])} height={Math.abs(drawing[1][1]-drawing[0][1])} fill="#43b49933" stroke="#16896e" vectorEffect="non-scaling-stroke" />}
          {tool === "polygon" && drawing.length > 0 && <polyline points={drawing.map((p) => p.join(",")).join(" ")} fill="#43b49933" stroke="#16896e" vectorEffect="non-scaling-stroke" />}
        </g>
      </svg>}
      {!saved && <p>저장 지도를 불러오는 중…</p>}
    </div>
    <div className={styles.toolbar}>
      <button type="button" className="ui-button ui-small" aria-label="지도 확대" onClick={() => setView({ ...view, zoom: Math.min(10,view.zoom*1.4) })}>＋</button>
      <button type="button" className="ui-button ui-small" aria-label="지도 축소" onClick={() => setView({ ...view, zoom: Math.max(.5,view.zoom/1.4) })}>−</button>
      <button type="button" className="ui-button ui-small" onClick={() => setView({ ...view, rotation: view.rotation+90 })}>90° 회전</button>
      <button type="button" className="ui-button ui-small" onClick={() => setView({ zoom: 1, rotation: 0, x: 0, y: 0 })}>전체 보기</button>
      {tool === "polygon" && <button type="button" className="ui-button ui-small" disabled={drawing.length < 3 || Boolean(pending)} onClick={() => add(drawing)}>영역 선택 완료</button>}
      {drawing.length > 0 && <button type="button" className="ui-button ui-small" onClick={() => setDrawing([])}>선택 중단</button>}
    </div>
    <p className="ui-caption">{tool === "rectangle" ? "드래그해서 영역을 선택하세요." : tool === "polygon" ? "꼭짓점을 누른 뒤 영역 선택 완료를 눌러 주세요." : "드래그해서 지도를 옮기세요."} 초록색으로 표시한 장애물 {preview.count}칸을 비웁니다.</p>
    <div className={styles.toolbar}>
      <button type="button" className="ui-button is-strong" disabled={!online || !saved || !preview.count || Boolean(pending) || drawing.length > 0}
        onClick={() => saved && void run("map_edit_save", { map, expectedRevision: saved.revision,
          polygons: selected.map((ring) => ring.map((p) => pixelToMap(p,saved.geometry))) })}>수정본 저장</button>
      <button type="button" className="ui-button" disabled={!online || !saved || !canApply || Boolean(pending) || dirty || saved.revision === saved.appliedRevision}
        onClick={() => saved && window.confirm("수정 지도를 주행에 적용할까요? 위치를 찾는 동안 로봇이 제자리에서 회전할 수 있어요.") &&
          void run("map_edit_apply", { map, expectedRevision: saved.revision, revision: saved.revision })}>주행에 적용</button>
      <button type="button" className="ui-button" disabled={!online || !saved || Boolean(pending)}
        onClick={() => saved && window.confirm("장애물 지도를 최초 편집 전 원본으로 복원해 저장할까요? 방·구역은 유지돼요.") &&
          void run("map_edit_restore", { map, expectedRevision: saved.revision })}>원본으로 복원</button>
      <button type="button" className="ui-button" disabled={!online || Boolean(pending)} onClick={reload}>다시 불러오기</button>
    </div>
    <p className="ui-note">저장과 주행 적용은 별도예요. 주행 적용은 로봇이 대기 중일 때 할 수 있어요.</p>
    <p role="status" aria-live="polite">{pending ? pending === "map_edit_apply" ? "수정 지도를 적용하고 위치를 확인하고 있어요…" : "로봇에서 지도를 처리하고 있어요…" : message}</p>
  </section>;
}
