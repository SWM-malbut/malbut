"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  ArrowClockwise,
  MapTrifold,
} from "@phosphor-icons/react";
import type { HomecamDevice } from "./homecam-dashboard";

export type RobotSnapshot = {
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
  "navigation_preview" | "navigation_start" | "navigation_cancel" |
  "room_split" | "room_merge" | "rooms_save" | "zones_apply";
export type MapMode = "view" | "navigate" | "rooms" | "zones";
type RoomTool = "select" | "split" | "merge";
type SplitLine = Array<[number, number]>;
type SplitValidation = "idle" | "ready" | "checking" | "valid" | "invalid";
type ZoneBehavior = "restricted" | "avoid" | "allow";
type ZoneCreateMode = "closed" | "menu" | "room";
type ZoneBounds = { minX: number; maxX: number; minY: number; maxY: number };
type PolygonCoordinates = Array<Array<[number, number]>>;
type ZoneDrag =
  | { type: "move"; zoneId: string; origin: [number, number]; geometry: GeoFeature["geometry"]; preferredGoal: [number, number] | null; wallEndpoints: [[number, number], [number, number]] | null }
  | { type: "corner"; zoneId: string; opposite: [number, number] }
  | { type: "edge"; zoneId: string; side: keyof ZoneBounds; bounds: ZoneBounds }
  | { type: "wall-endpoint"; zoneId: string; endpointIndex: 0 | 1; opposite: [number, number]; width: number };

type GeoFeature = {
  type: "Feature";
  id?: string;
  properties: Record<string, unknown>;
  geometry: { type: string; coordinates: unknown };
};

export type RobotSemantics = {
  revision: string;
  mapId: string;
  mapRevision: string;
  userMap: Record<string, unknown> | null;
  zones: Record<string, unknown> | null;
};

export function RobotMapPanel({
  device,
  initialMode = "view",
}: {
  device: HomecamDevice | null;
  initialMode?: MapMode;
}) {
  const deviceId = device?.id ?? "";
  const [snapshot, setSnapshot] = useState<RobotSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [navigationPreview, setNavigationPreview] = useState<Record<string, unknown> | null>(null);
  const [previewExpiresAt, setPreviewExpiresAt] = useState(0);
  const [clockNow, setClockNow] = useState(0);
  const [mapMode, setMapMode] = useState<MapMode>(initialMode);
  const [semantics, setSemantics] = useState<RobotSemantics | null>(null);
  const [roomDrafts, setRoomDrafts] = useState<GeoFeature[]>([]);
  const [roomTool, setRoomTool] = useState<RoomTool>("select");
  const [selectedRoomId, setSelectedRoomId] = useState("");
  const [mergeTargetId, setMergeTargetId] = useState("");
  const [splitLines, setSplitLines] = useState<SplitLine[]>([]);
  const [pendingSplitPoint, setPendingSplitPoint] = useState<[number, number] | null>(null);
  const [splitValidation, setSplitValidation] = useState<SplitValidation>("idle");
  const [splitValidationMessage, setSplitValidationMessage] = useState("");
  const [validatedSplit, setValidatedSplit] = useState<{
    signature: string;
    sourceId: string;
    rooms: GeoFeature[];
  } | null>(null);
  const [draggingSplitPoint, setDraggingSplitPoint] = useState<{ lineIndex: number; pointIndex: number } | null>(null);
  const [zoneDrafts, setZoneDrafts] = useState<GeoFeature[]>([]);
  const [selectedZoneId, setSelectedZoneId] = useState("");
  const [zoneGoalMode, setZoneGoalMode] = useState(false);
  const [draggingZone, setDraggingZone] = useState<ZoneDrag | null>(null);
  const [newZoneBehavior, setNewZoneBehavior] = useState<ZoneBehavior>("restricted");
  const [zoneCreateMode, setZoneCreateMode] = useState<ZoneCreateMode>("closed");
  const [semanticRefresh, setSemanticRefresh] = useState(0);
  const [robotTrail, setRobotTrail] = useState<Array<[number, number]>>([]);
  const processedCommand = useRef("");
  const trailSession = useRef("");
  const mapCanvasRef = useRef<HTMLDivElement | null>(null);
  const suppressMapClick = useRef(false);
  const pendingRoomAction = useRef<{
    operation: "room_split" | "room_merge";
    sourceIds: string[];
    signature?: string;
  } | null>(null);
  const completedRoomEdit = useRef<{
    operation: "room_split" | "room_merge";
    sourceIds: string[];
    replacement: GeoFeature[];
  } | null>(null);
  const pendingRoomSave = useRef<{
    rooms: GeoFeature[];
    retries: number;
  } | null>(null);
  const pendingZoneSave = useRef<{
    zones: GeoFeature[];
    retries: number;
  } | null>(null);
  const semanticRetryTimer = useRef<number | null>(null);
  const loadedSemanticIdentity = useRef("");
  const splitDraftSignature = JSON.stringify([selectedRoomId, splitLines, pendingSplitPoint]);
  const splitDraftSignatureRef = useRef(splitDraftSignature);

  useEffect(() => {
    splitDraftSignatureRef.current = splitDraftSignature;
  }, [splitDraftSignature]);

  const load = useCallback(async (quiet = false) => {
    if (!deviceId) {
      setSnapshot(null);
      setLoading(false);
      return;
    }
    if (!quiet) setLoading(true);
    try {
      const response = await fetch(
        `/api/devices/${encodeURIComponent(deviceId)}/robot`,
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
          const message = typeof result.error === "string" ? result.error : "로봇이 명령을 완료하지 못했습니다.";
          if (command.operation === "room_split") {
            const action = pendingRoomAction.current;
            if (action?.signature === splitDraftSignatureRef.current) {
              setValidatedSplit(null);
              setSplitValidation("invalid");
              setSplitValidationMessage(splitErrorMessage(message));
              setNotice(`방을 나눌 수 없습니다. ${splitErrorMessage(message)}`);
            }
          } else if (command.operation === "room_merge") {
            setNotice(`방을 합칠 수 없습니다. ${mergeErrorMessage(message)}`);
          } else if (command.operation === "rooms_save") {
            pendingRoomSave.current = null;
            setNotice(`방 설정을 저장하지 못했습니다. ${message}`);
          } else if (command.operation === "zones_apply") {
            pendingZoneSave.current = null;
            setNotice(`구역 설정을 저장하지 못했습니다. ${message}`);
          } else {
            setNotice(message);
          }
          pendingRoomAction.current = null;
        } else if (command.operation === "navigation_start") {
          setNavigationPreview(null);
          setPreviewExpiresAt(0);
          setNotice("선택한 목적지로 이동을 시작했습니다.");
        } else if (command.operation === "room_split" && isRecord(command.result)) {
          const splitRooms = Array.isArray(command.result.rooms)
            ? command.result.rooms.filter(isGeoFeature)
            : [];
          const splitFrom = splitRooms[0]?.properties.split_from;
          const selected = pendingRoomAction.current?.operation === "room_split"
            ? pendingRoomAction.current.sourceIds[0]
            : typeof splitFrom === "string" ? splitFrom : "";
          const signature = pendingRoomAction.current?.operation === "room_split"
            ? pendingRoomAction.current.signature
            : undefined;
          if (selected && splitRooms.length === 2 && signature === splitDraftSignatureRef.current) {
            setValidatedSplit({ signature, sourceId: selected, rooms: splitRooms });
            setSplitValidation("valid");
            setSplitValidationMessage("각 공간이 1㎡ 이상이고 정확히 두 공간으로 나뉩니다.");
            setNotice("분할선을 확인했습니다. 지도에서 확인한 뒤 방 나누기를 적용하세요.");
          }
        } else if (command.operation === "room_merge" && isRecord(command.result) && isGeoFeature(command.result.room)) {
          const merged = command.result.room;
          const mergedFrom = Array.isArray(merged.properties.merged_from)
            ? merged.properties.merged_from.filter((value): value is string => typeof value === "string")
            : [];
          const sourceIds = pendingRoomAction.current?.operation === "room_merge"
            ? pendingRoomAction.current.sourceIds
            : mergedFrom;
          if (sourceIds.length === 2) {
            completedRoomEdit.current = {
              operation: "room_merge",
              sourceIds,
              replacement: [merged],
            };
            setRoomDrafts((current) => replaceRoomsAtFirstIndex(current, sourceIds, [merged]));
            setSelectedRoomId(featureId(merged));
            setMergeTargetId("");
            setRoomTool("select");
            setNotice("인접한 두 방을 합쳤습니다. 확인한 뒤 저장해 주세요.");
          }
        } else if (command.operation === "rooms_save" || command.operation === "zones_apply") {
          if (command.operation === "rooms_save") {
            const normalizedRooms = isRecord(command.result) && Array.isArray(command.result.rooms)
              ? command.result.rooms.filter(isGeoFeature)
              : [];
            if (normalizedRooms.length > 0) {
              pendingRoomSave.current = { rooms: normalizedRooms, retries: 0 };
              setRoomDrafts(normalizedRooms);
            }
            completedRoomEdit.current = null;
          }
          setSemanticRefresh((value) => value + 1);
          setNotice("공간 설정을 저장하고 말벗에 반영했습니다.");
        }
        if (command.operation === "room_split" || command.operation === "room_merge") {
          pendingRoomAction.current = null;
        }
      }
      if (!quiet) setNotice("");
    } catch (error) {
      if (!quiet) setNotice(error instanceof Error ? error.message : "로봇 지도를 불러오지 못했습니다.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [deviceId]);

  useEffect(() => {
    if (!deviceId || !snapshot?.map?.revision) return;
    const controller = new AbortController();
    void fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/semantic`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({})) as RobotSemantics & { error?: string };
        if (!response.ok) throw new Error(payload.error ?? "공간 정보를 불러오지 못했습니다.");
        return payload;
      })
      .then((payload) => {
        if (controller.signal.aborted) return;
        setSemantics(payload);
        const baseRooms = featuresOf(payload.userMap).filter((feature) => feature.properties.role === "room");
        const pendingSave = pendingRoomSave.current;
        let nextRooms: GeoFeature[];
        if (pendingSave && roomSnapshotKey(baseRooms) === roomSnapshotKey(pendingSave.rooms)) {
          pendingRoomSave.current = null;
          completedRoomEdit.current = null;
          nextRooms = baseRooms;
        } else if (pendingSave) {
          nextRooms = pendingSave.rooms;
          if (pendingSave.retries < 8) {
            pendingSave.retries += 1;
            if (semanticRetryTimer.current !== null) {
              window.clearTimeout(semanticRetryTimer.current);
            }
            semanticRetryTimer.current = window.setTimeout(() => {
              semanticRetryTimer.current = null;
              setSemanticRefresh((value) => value + 1);
            }, 1_000);
          } else {
            setNotice("방 설정은 말벗에 저장됐지만 클라우드 반영 확인이 늦어지고 있습니다.");
          }
        } else {
          const edit = completedRoomEdit.current;
          nextRooms = edit
            ? replaceRoomsAtFirstIndex(baseRooms, edit.sourceIds, edit.replacement)
            : baseRooms;
        }
        setRoomDrafts(nextRooms);
        setSelectedRoomId((current) => nextRooms.some((room) => featureId(room) === current) ? current : "");
        setMergeTargetId((current) => nextRooms.some((room) => featureId(room) === current) ? current : "");
        const baseZones = featuresOf(payload.zones).map(cloneFeature);
        const pendingZones = pendingZoneSave.current;
        let nextZones = baseZones;
        if (pendingZones && zoneSnapshotKey(baseZones) === zoneSnapshotKey(pendingZones.zones)) {
          pendingZoneSave.current = null;
        } else if (pendingZones) {
          nextZones = pendingZones.zones;
          if (pendingZones.retries < 8) {
            pendingZones.retries += 1;
            if (semanticRetryTimer.current !== null) window.clearTimeout(semanticRetryTimer.current);
            semanticRetryTimer.current = window.setTimeout(() => {
              semanticRetryTimer.current = null;
              setSemanticRefresh((value) => value + 1);
            }, 1_000);
          } else {
            setNotice("구역 설정은 말벗에 저장됐지만 클라우드 반영 확인이 늦어지고 있습니다.");
          }
        }
        setZoneDrafts(nextZones);
        setSelectedZoneId((current) => nextZones.some((zone) => featureId(zone) === current) ? current : "");
        setZoneGoalMode(false);
        const identity = `${payload.mapId}:${payload.mapRevision}`;
        if (loadedSemanticIdentity.current && loadedSemanticIdentity.current !== identity) {
          clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
          setRoomTool("select");
        }
        loadedSemanticIdentity.current = identity;
      })
      .catch((error) => {
        if (!controller.signal.aborted) setNotice(error instanceof Error ? error.message : "공간 정보를 불러오지 못했습니다.");
      });
    return () => {
      controller.abort();
      if (semanticRetryTimer.current !== null) {
        window.clearTimeout(semanticRetryTimer.current);
        semanticRetryTimer.current = null;
      }
    };
  }, [deviceId, semanticRefresh, snapshot?.map?.revision]);

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
  const roomCommandPending = Boolean(activeCommand && snapshot?.command &&
    ["room_split", "room_merge", "rooms_save"].includes(snapshot.command.operation));
  const zoneCommandPending = Boolean(activeCommand && snapshot?.command?.operation === "zones_apply");
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
  const navigationSucceeded = mapMode === "navigate" && !previewToken && navigation?.state === "succeeded";
  const navigationProgress = navigationSucceeded ? 100 : navigationProgressPercent(navigation);
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
  const zoneFeatures = featuresOf(semantics?.zones);
  const renderedZoneFeatures = zoneDrafts;
  const selectedRoom = roomDrafts.find((room) => featureId(room) === selectedRoomId) ?? null;
  const mergeTarget = roomDrafts.find((room) => featureId(room) === mergeTargetId) ?? null;
  const selectedZone = zoneDrafts.find((zone) => featureId(zone) === selectedZoneId) ?? null;
  const selectedZoneRing = selectedZone ? polygonOuterRing(selectedZone) : null;
  const selectedWallEndpoints = selectedZone ? virtualWallEndpoints(selectedZone) : null;
  const selectedZoneBounds = !selectedWallEndpoints && selectedZoneRing && isRectangleRing(selectedZoneRing)
    ? rectangleBounds(selectedZoneRing)
    : null;
  const walkableArea = featuresOf(semantics?.userMap)
    .find((feature) => feature.properties.role === "walkable_area") ?? null;
  const originalRooms = featuresOf(semantics?.userMap).filter((feature) => feature.properties.role === "room");
  const roomsDirty = JSON.stringify(roomDrafts) !== JSON.stringify(originalRooms);
  const zonesDirty = zoneSnapshotKey(zoneDrafts) !== zoneSnapshotKey(zoneFeatures);

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
    if (!device || busy) return false;
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
                  : operation === "navigation_cancel" ? "주행 취소를 요청했습니다."
                    : operation === "room_split" ? "로봇에서 분할 가능 여부를 확인하고 있습니다."
                      : operation === "room_merge" ? "로봇에서 두 방의 인접 여부를 확인하고 있습니다."
                        : operation === "rooms_save" ? "방 설정을 말벗에 저장하고 있습니다."
                          : "구역 설정을 말벗에 적용하고 있습니다.",
      );
      await load(true);
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "지도 명령을 전달하지 못했습니다.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const selectDestination = (event: React.MouseEvent<HTMLDivElement>) => {
    if (suppressMapClick.current) {
      suppressMapClick.current = false;
      return;
    }
    const geometry = snapshot?.map?.geometry;
    if (!geometry || !isOwner || !snapshot?.online || navigationDriving) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const fractionX = (event.clientX - bounds.left) / bounds.width;
    const fractionY = (event.clientY - bounds.top) / bounds.height;
    const localX = fractionX * geometry.width * geometry.resolution;
    const localY = (1 - fractionY) * geometry.height * geometry.resolution;
    const cosine = Math.cos(geometry.originYaw);
    const sine = Math.sin(geometry.originYaw);
    const x = geometry.originX + cosine * localX - sine * localY;
    const y = geometry.originY + sine * localX + cosine * localY;
    if (mapMode === "rooms") {
      if (roomTool === "select") {
        const room = roomDrafts.find((candidate) => featureContains(candidate, x, y));
        setSelectedRoomId(room ? featureId(room) : "");
        setMergeTargetId("");
        clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
      } else if (roomTool === "merge") {
        const room = roomDrafts.find((candidate) => featureContains(candidate, x, y));
        if (!selectedRoom) {
          if (room) setSelectedRoomId(featureId(room));
          return;
        }
        if (!room || featureId(room) === selectedRoomId) {
          setNotice("현재 방과 맞닿아 있는 다른 방을 선택하세요.");
          return;
        }
        setMergeTargetId(featureId(room));
        setNotice("");
      } else {
        if (!selectedRoom) {
          setNotice("먼저 나눌 방을 선택한 뒤 ‘나누는 선 그리기’를 누르세요.");
          return;
        }
        setValidatedSplit(null);
        const clickedPoint: [number, number] = [roundMapCoordinate(x), roundMapCoordinate(y)];
        const point = snapToRoomWall(clickedPoint, selectedRoom.geometry, 0.25);
        if (!point) {
          setSplitValidation("invalid");
          setSplitValidationMessage("분할선의 양 끝점은 방 벽에서 25cm 이내에 놓아야 합니다.");
          return;
        }
        if (pendingSplitPoint) {
          const nextLines = [...splitLines, [pendingSplitPoint, point]];
          setSplitLines(nextLines);
          setPendingSplitPoint(null);
          const error = validateSplitDraft(selectedRoom, nextLines, null, geometry.resolution);
          setSplitValidation(error ? "invalid" : "ready");
          setSplitValidationMessage(error);
        } else {
          setPendingSplitPoint(point);
          setSplitValidation("idle");
          setSplitValidationMessage("두 번째 벽을 선택하세요.");
        }
      }
      return;
    }
    if (mapMode === "zones") {
      if (zoneGoalMode && selectedZone) {
        if (!featureContains(selectedZone, x, y)) {
          setNotice("대표 위치는 선택한 구역 안에 지정하세요.");
          return;
        }
        updateSelectedZone({ preferred_goal: [roundMapCoordinate(x), roundMapCoordinate(y)] });
        setZoneGoalMode(false);
        setNotice("구역의 대표 위치를 지정했습니다. 주행에 적용하면 말벗에 반영됩니다.");
      } else {
        const zone = [...zoneDrafts].reverse().find((candidate) => featureContains(candidate, x, y));
        setSelectedZoneId(zone ? featureId(zone) : "");
        setZoneGoalMode(false);
      }
      return;
    }
    if (runtimeMode !== "navigation" || mapMode !== "navigate") return;
    setNavigationPreview(null);
    setPreviewExpiresAt(0);
    void sendCommand("navigation_preview", { x, y });
  };

  const applyRoomSplit = () => {
    if (!selectedRoom) return;
    const validationError = validateSplitDraft(
      selectedRoom,
      splitLines,
      pendingSplitPoint,
      snapshot?.map?.geometry.resolution ?? 0.05,
    );
    if (validationError) {
      setSplitValidation("invalid");
      setSplitValidationMessage(validationError);
      return;
    }
    if (
      validatedSplit &&
      validatedSplit.signature === splitDraftSignature &&
      validatedSplit.sourceId === featureId(selectedRoom)
    ) {
      completedRoomEdit.current = {
        operation: "room_split",
        sourceIds: [validatedSplit.sourceId],
        replacement: validatedSplit.rooms,
      };
      setRoomDrafts((current) => replaceRoomsAtFirstIndex(
        current,
        [validatedSplit.sourceId],
        validatedSplit.rooms,
      ));
      setSelectedRoomId(featureId(validatedSplit.rooms[0]));
      setValidatedSplit(null);
      clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
      setRoomTool("select");
      setNotice("방을 나눴습니다. 이름과 종류를 확인한 뒤 방 설정을 저장하세요.");
      return;
    }
    setSplitValidation("checking");
    setSplitValidationMessage("로봇에서 최소 면적과 연결성을 확인하고 있습니다.");
    pendingRoomAction.current = {
      operation: "room_split",
      sourceIds: [featureId(selectedRoom)],
      signature: splitDraftSignature,
    };
    void sendCommand("room_split", {
      room: selectedRoom,
      lines: splitLines,
      resolution: snapshot?.map?.geometry.resolution ?? 0.05,
      minimum_room_area: 1,
    }).then((accepted) => {
      if (!accepted) {
        pendingRoomAction.current = null;
        setSplitValidation("ready");
        setSplitValidationMessage("");
      }
    });
  };

  const applyRoomMerge = () => {
    if (!selectedRoom || !mergeTarget) return;
    const sourceIds = [featureId(selectedRoom), featureId(mergeTarget)];
    pendingRoomAction.current = { operation: "room_merge", sourceIds };
    void sendCommand("room_merge", {
      rooms: [selectedRoom, mergeTarget],
      resolution: snapshot?.map?.geometry.resolution ?? 0.05,
    }).then((accepted) => {
      if (!accepted) pendingRoomAction.current = null;
    });
  };

  const updateSelectedRoom = (properties: Record<string, unknown>) => {
    if (!selectedRoom) return;
    const selectedId = featureId(selectedRoom);
    setRoomDrafts((current) => current.map((room) => featureId(room) === selectedId
      ? updateRoomProperties(room, properties)
      : room));
  };

  const saveRooms = () => {
    if (!semantics || roomDrafts.length === 0) return;
    pendingRoomSave.current = {
      rooms: roomDrafts.map(cloneFeature),
      retries: 0,
    };
    void sendCommand("rooms_save", {
      map_id: semantics.mapId,
      map_revision: semantics.mapRevision,
      rooms: roomDrafts,
      resolution: snapshot?.map?.geometry.resolution ?? 0.05,
    }).then((accepted) => {
      if (!accepted) pendingRoomSave.current = null;
    });
  };

  const updateSelectedZone = (updates: Record<string, unknown>) => {
    if (!selectedZone) return;
    const selectedId = featureId(selectedZone);
    setZoneDrafts((current) => current.map((zone) => {
      if (featureId(zone) !== selectedId) return zone;
      const properties = { ...zone.properties, ...updates };
      if (properties.behavior === "restricted") delete properties.preferred_goal;
      return { ...zone, properties };
    }));
  };

  const addDefaultZone = () => {
    if (!walkableArea) {
      setNotice("지도의 주행 가능 영역을 확인하지 못했습니다.");
      return;
    }
    const ring = defaultZoneRing(walkableArea, zoneDrafts.length);
    if (!ring) {
      setNotice("새 구역을 배치할 수 있는 열린 공간이 없습니다.");
      return;
    }
    const zone = createZoneFeature(
      ring,
      `구역 ${zoneDrafts.length + 1}`,
      newZoneBehavior,
    );
    setZoneDrafts((current) => [...current, zone]);
    setSelectedZoneId(featureId(zone));
    setZoneGoalMode(false);
    setZoneCreateMode("closed");
    setNotice("새 구역을 만들었습니다. 지도에서 끌거나 모서리와 변을 움직여 크기를 조절하세요.");
  };

  const addVirtualWall = () => {
    if (!walkableArea) {
      setNotice("지도의 주행 가능 영역을 확인하지 못했습니다.");
      return;
    }
    const wall = defaultVirtualWall(walkableArea, zoneDrafts.length);
    if (!wall) {
      setNotice("가상 벽을 배치할 수 있는 열린 공간이 없습니다.");
      return;
    }
    const zone = createVirtualWallFeature(
      wall.endpoints,
      `가상 벽 ${zoneDrafts.filter(isVirtualWall).length + 1}`,
      wall.width,
    );
    setZoneDrafts((current) => [...current, zone]);
    setSelectedZoneId(featureId(zone));
    setZoneGoalMode(false);
    setZoneCreateMode("closed");
    setNotice("가상 벽을 만들었습니다. 선을 끌어 이동하거나 양 끝점을 움직여 길이와 각도를 조절하세요.");
  };

  const addRoomAsZone = (room: GeoFeature) => {
    const geometries = polygonGeometries(room.geometry);
    if (geometries.length === 0) {
      setNotice("이 방의 경계를 구역으로 변환할 수 없습니다.");
      return;
    }
    const roomName = featureName(room, "이름 없는 방");
    const additions = geometries.map((coordinates, index) => {
      const zone = createZoneFeature(
        coordinates[0],
        `${roomName} · ${zoneBehaviorLabel(newZoneBehavior)}${geometries.length > 1 ? ` ${index + 1}` : ""}`,
        newZoneBehavior,
      );
      zone.geometry = {
        type: "Polygon",
        coordinates: coordinates.map((ring) => ring.map((point) => [...point])),
      };
      const metrics = polygonMetrics(coordinates);
      zone.properties.area_m2 = roundArea(metrics.area);
      zone.properties.centroid = metrics.centroid;
      const representative = room.properties.representative_point;
      if (newZoneBehavior !== "restricted" && validPoint(representative) && pointInPolygon(representative, coordinates)) {
        zone.properties.preferred_goal = [...representative];
      }
      zone.properties.source_room_id = featureId(room);
      return zone;
    });
    setZoneDrafts((current) => [...current, ...additions]);
    setSelectedZoneId(featureId(additions[0]));
    setZoneGoalMode(false);
    setZoneCreateMode("closed");
    setNotice(`${roomName} 전체를 ${zoneBehaviorLabel(newZoneBehavior)} 구역으로 추가했습니다.`);
  };

  const removeSelectedZone = () => {
    if (!selectedZone) return;
    const id = featureId(selectedZone);
    setZoneDrafts((current) => current.filter((zone) => featureId(zone) !== id));
    setSelectedZoneId("");
    setZoneGoalMode(false);
  };

  const saveZones = () => {
    if (!semantics || !walkableArea) return;
    const normalized = zoneDrafts.map(normalizeZoneFeature);
    const invalid = normalized.find((zone) => zoneGeometryValidationError(
      zone.geometry,
      walkableArea,
      snapshot?.map?.geometry.resolution ?? 0.05,
      isVirtualWall(zone) ? 0.02 : 0.1,
    ));
    if (invalid) {
      setSelectedZoneId(featureId(invalid));
      setNotice(`${featureName(invalid, "구역")}의 경계가 주행 가능한 지도 안에 있는지 확인하세요.`);
      return;
    }
    pendingZoneSave.current = { zones: normalized.map(cloneFeature), retries: 0 };
    void sendCommand("zones_apply", {
      type: "FeatureCollection",
      format: "malbut-semantic-zones-v1",
      map_id: semantics.mapId,
      map_revision: semantics.mapRevision,
      frame_id: "map",
      features: normalized,
    }).then((accepted) => {
      if (!accepted) pendingZoneSave.current = null;
    });
  };

  const chooseRoomTool = (tool: RoomTool) => {
    if (tool !== "select" && !selectedRoom) {
      setNotice("먼저 지도나 방 목록에서 편집할 방을 선택하세요.");
      return;
    }
    setRoomTool(tool);
    setValidatedSplit(null);
    setMergeTargetId("");
    clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
    setNotice(tool === "merge" ? "현재 방과 합칠 방 하나를 선택하세요." : "");
  };

  const undoSplitPoint = useCallback(() => {
    setValidatedSplit(null);
    if (pendingSplitPoint) {
      setPendingSplitPoint(null);
    } else {
      setSplitLines((current) => current.slice(0, -1));
    }
    setSplitValidation("idle");
    setSplitValidationMessage("");
  }, [pendingSplitPoint]);

  useEffect(() => {
    if (!draggingSplitPoint || !selectedRoom || !snapshot?.map?.geometry) return;
    const geometry = snapshot.map.geometry;
    const move = (event: PointerEvent) => {
      const canvas = mapCanvasRef.current;
      if (!canvas) return;
      let point = pointerToWorld(event.clientX, event.clientY, canvas, geometry);
      setSplitLines((current) => {
        const lines = current.map((line) => line.map((value) => [...value] as [number, number]));
        const line = lines[draggingSplitPoint.lineIndex];
        if (!line) return current;
        const pointIndex = draggingSplitPoint.pointIndex;
        const endpoint = pointIndex === 0 || pointIndex === line.length - 1;
        if (endpoint) {
          const snapped = snapToRoomWall(point, selectedRoom.geometry, 0.25);
          if (!snapped) return current;
          point = snapped;
        } else {
          point = orthogonalCorner(
            point,
            line[pointIndex - 1],
            line[pointIndex + 1],
            selectedRoom.geometry,
          );
          if (!pointInGeometry(point, selectedRoom.geometry) &&
              !pointNearRoomWall(point, selectedRoom.geometry, 0.25)) return current;
        }
        line[pointIndex] = point;
        setValidatedSplit(null);
        const error = validateSplitDraft(selectedRoom, lines, null, geometry.resolution);
        setSplitValidation(error ? "invalid" : "ready");
        setSplitValidationMessage(error);
        return lines;
      });
    };
    const end = () => {
      setDraggingSplitPoint(null);
      window.setTimeout(() => { suppressMapClick.current = false; }, 0);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
    window.addEventListener("pointercancel", end, { once: true });
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [draggingSplitPoint, selectedRoom, snapshot?.map?.geometry]);

  useEffect(() => {
    if (mapMode !== "rooms" || roomTool !== "split") return;
    const keydown = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
      if (event.key === "Backspace") {
        event.preventDefault();
        undoSplitPoint();
      } else if (event.key === "Escape") {
        setRoomTool("select");
        clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [mapMode, roomTool, undoSplitPoint]);

  useEffect(() => {
    if (!draggingZone || !snapshot?.map?.geometry || !walkableArea) return;
    const geometry = snapshot.map.geometry;
    const move = (event: PointerEvent) => {
      const canvas = mapCanvasRef.current;
      if (!canvas) return;
      const point = pointerToWorld(event.clientX, event.clientY, canvas, geometry);
      setZoneDrafts((current) => current.map((zone) => {
        if (featureId(zone) !== draggingZone.zoneId) return zone;
        let ring: Array<[number, number]> | null = null;
        if (draggingZone.type === "move") {
          const dx = point[0] - draggingZone.origin[0];
          const dy = point[1] - draggingZone.origin[1];
          const moved = translateZone(
            zone,
            draggingZone.geometry,
            draggingZone.preferredGoal,
            draggingZone.wallEndpoints,
            dx,
            dy,
          );
          if (zoneGeometryValidationError(
            moved.geometry,
            walkableArea,
            geometry.resolution,
            isVirtualWall(moved) ? 0.02 : 0.1,
          )) return zone;
          return moved;
        } else if (draggingZone.type === "corner") {
          ring = rectangleRing(point[0], point[1], draggingZone.opposite[0], draggingZone.opposite[1]);
        } else if (draggingZone.type === "edge") {
          const bounds = { ...draggingZone.bounds, [draggingZone.side]: draggingZone.side.endsWith("X") ? point[0] : point[1] };
          if (bounds.minX >= bounds.maxX || bounds.minY >= bounds.maxY) return zone;
          ring = rectangleRing(bounds.minX, bounds.minY, bounds.maxX, bounds.maxY);
        } else {
          const endpoints: [[number, number], [number, number]] = draggingZone.endpointIndex === 0
            ? [point, draggingZone.opposite]
            : [draggingZone.opposite, point];
          if (wallLength(endpoints) < 0.4) return zone;
          ring = virtualWallRing(endpoints, draggingZone.width);
          if (zoneRingValidationError(ring, walkableArea, geometry.resolution, 0.02)) return zone;
          return withVirtualWall(zone, endpoints, draggingZone.width);
        }
        if (zoneRingValidationError(ring, walkableArea, geometry.resolution)) return zone;
        return withZoneRing(zone, ring);
      }));
    };
    const end = () => {
      setDraggingZone(null);
      window.setTimeout(() => { suppressMapClick.current = false; }, 0);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
    window.addEventListener("pointercancel", end, { once: true });
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [draggingZone, snapshot?.map?.geometry, walkableArea]);

  return (
    <section className="homecam-section robot-map-section" aria-labelledby="robot-map-title">
      <div className="robot-map-topbar">
        <h1 id="robot-map-title">{mapping ? "집 둘러보는 중" : navigationDriving ? "이동 중" : navigationSucceeded ? "이동 완료" : "우리 집 지도"}</h1>
        <div className="robot-map-mode-tabs" aria-label="지도 모드">
          {([
            ["view", "보기"],
            ["navigate", "목적지 선택"],
            ["rooms", "방 편집"],
            ["zones", "구역 편집"],
          ] as Array<[MapMode, string]>).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              className={mapMode === mode ? "is-active" : ""}
              disabled={Boolean(mapping) && mode !== "view"}
              onClick={() => {
                setMapMode(mode);
                setRoomTool("select");
                setMergeTargetId("");
                setValidatedSplit(null);
                setZoneGoalMode(false);
                setDraggingZone(null);
                setZoneCreateMode("closed");
                clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <span className={`robot-map-top-status ${snapshot?.online ? "is-online" : ""}`}>
          <i aria-hidden="true" />
          {snapshot?.online ? "연결됨" : "오프라인"} · {localizationCopy(snapshot?.state?.localization.state)}
        </span>
        <button type="button" className="robot-map-refresh" onClick={() => void load()} disabled={loading} aria-label="지도 새로고침">
          <ArrowClockwise size={18} weight="bold" aria-hidden="true" />
        </button>
      </div>

      <div className="robot-map-layout">
        <div className="robot-map-primary">
          <div className={`robot-map-mode-banner mode-${mapping ? "mapping" : mapMode}`}>
            <strong>
              {mapping ? "지도 만들기 모드"
                : mapMode === "navigate" ? "목적지 선택 모드"
                  : mapMode === "rooms" ? "방 편집 모드"
                    : mapMode === "zones" ? "구역 편집 모드"
                      : "지도 보기 모드"}
            </strong>
            <span>
              {mapping ? "새로운 공간을 확인하는 동안 목적지 선택과 편집을 사용할 수 없어요."
                : mapMode === "navigate" ? "지도에서 보낼 곳을 누르세요. 방과 구역은 선택을 방해하지 않아요."
                  : mapMode === "rooms" ? "방 경계와 이름을 정리할 수 있어요. 저장하면 말벗에 반영됩니다."
                    : mapMode === "zones" ? "진입 금지·회피 구역을 확인하고 편집할 수 있어요."
                      : "저장된 공간과 말벗의 현재 위치를 확인하세요."}
            </span>
          </div>
          <div className={`robot-map-card mode-${mapping ? "mapping" : mapMode}`}>
            {snapshot?.map ? (
              <div
                ref={mapCanvasRef}
                className={`robot-map-canvas ${runtimeMode === "navigation" && mapMode === "navigate" ? "is-navigation" : ""}`}
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
              {(roomDrafts.length > 0 || renderedZoneFeatures.length > 0 || splitLines.length > 0 || pendingSplitPoint) && (
                <svg
                  className={`robot-map-semantics ${mapMode === "rooms" || mapMode === "zones" ? "is-interactive" : ""}`}
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                  style={mapMode === "rooms" || mapMode === "zones"
                    ? { pointerEvents: "auto", touchAction: "none", userSelect: "none" }
                    : undefined}
                  aria-hidden="true"
                >
                  <defs>
                    <pattern id="restricted-hatch" width="1.8" height="1.8" patternUnits="userSpaceOnUse">
                      <rect width="1.8" height="1.8" fill="rgba(192,64,47,.08)" />
                      <path d="M-.45 1.35 L1.35 -.45 M.45 2.25 L2.25 .45" stroke="rgba(192,64,47,.48)" strokeWidth=".18" />
                    </pattern>
                    <pattern id="avoid-dots" width="2.2" height="2.2" patternUnits="userSpaceOnUse">
                      <rect width="2.2" height="2.2" fill="rgba(200,144,26,.07)" />
                      <circle cx="1.1" cy="1.1" r=".22" fill="rgba(200,144,26,.65)" />
                    </pattern>
                  </defs>
                  {renderedZoneFeatures.map((zone) => {
                    const path = featureGeometryPath(zone, snapshot.map!.geometry);
                    const behavior = zoneBehaviorOf(zone);
                    const id = featureId(zone);
                    const wall = virtualWallEndpoints(zone);
                    if (!path) return null;
                    if (wall) {
                      const start = worldToPercent(wall[0][0], wall[0][1], snapshot.map!.geometry);
                      const end = worldToPercent(wall[1][0], wall[1][1], snapshot.map!.geometry);
                      return (
                        <g key={id}>
                          <line
                            className="robot-map-virtual-wall-hit"
                            x1={start.left}
                            y1={start.top}
                            x2={end.left}
                            y2={end.top}
                            onClick={(event) => {
                              if (mapMode !== "zones") return;
                              event.stopPropagation();
                              setSelectedZoneId(id);
                              setZoneGoalMode(false);
                            }}
                            onPointerDown={(event) => {
                              if (mapMode !== "zones" || !isOwner || zoneCommandPending || busy) return;
                              const canvas = mapCanvasRef.current;
                              if (!canvas) return;
                              event.preventDefault();
                              event.stopPropagation();
                              event.currentTarget.setPointerCapture(event.pointerId);
                              suppressMapClick.current = true;
                              setSelectedZoneId(id);
                              setZoneGoalMode(false);
                              setDraggingZone({
                                type: "move",
                                zoneId: id,
                                origin: pointerToWorld(event.clientX, event.clientY, canvas, snapshot.map!.geometry),
                                geometry: cloneFeature(zone).geometry,
                                preferredGoal: null,
                                wallEndpoints: wall.map((point) => [...point]) as [[number, number], [number, number]],
                              });
                            }}
                          />
                          <line
                            className={`robot-map-virtual-wall ${mapMode === "zones" && selectedZoneId === id ? "is-selected" : ""}`}
                            x1={start.left}
                            y1={start.top}
                            x2={end.left}
                            y2={end.top}
                            stroke={zoneColor(zone)}
                          />
                        </g>
                      );
                    }
                    return (
                      <path
                        key={id}
                        className={`robot-map-zone-shape is-${behavior} ${mapMode === "zones" && selectedZoneId === id ? "is-selected" : ""}`}
                        d={path}
                        fill={behavior === "restricted" ? "url(#restricted-hatch)" : behavior === "avoid" ? "url(#avoid-dots)" : "rgba(46,125,81,.15)"}
                        fillRule="evenodd"
                        stroke={zoneColor(zone)}
                        onClick={(event) => {
                          if (mapMode !== "zones") return;
                          event.stopPropagation();
                          setSelectedZoneId(id);
                          setZoneGoalMode(false);
                        }}
                        onPointerDown={(event) => {
                          if (mapMode !== "zones" || !isOwner || zoneCommandPending || busy) return;
                          const ring = polygonOuterRing(zone);
                          const canvas = mapCanvasRef.current;
                          if (!ring || !canvas) return;
                          event.preventDefault();
                          event.stopPropagation();
                          event.currentTarget.setPointerCapture(event.pointerId);
                          suppressMapClick.current = true;
                          setSelectedZoneId(id);
                          setZoneGoalMode(false);
                          setDraggingZone({
                            type: "move",
                            zoneId: id,
                            origin: pointerToWorld(event.clientX, event.clientY, canvas, snapshot.map!.geometry),
                            geometry: cloneFeature(zone).geometry,
                            preferredGoal: validPoint(zone.properties.preferred_goal)
                              ? [...zone.properties.preferred_goal] as [number, number]
                              : null,
                            wallEndpoints: null,
                          });
                        }}
                      />
                    );
                  })}
                  {roomDrafts.map((room) => {
                    const path = walkableArea
                      ? roomInternalBoundaryPath(
                        room,
                        walkableArea,
                        snapshot.map!.geometry,
                        snapshot.map!.geometry.resolution,
                      )
                      : "";
                    if (!path) return null;
                    const id = featureId(room);
                    return (
                      <path
                        key={id}
                        className={`robot-map-room-divider ${mapMode !== "rooms" ? "is-context" : ""} ${selectedRoomId === id ? "is-selected" : ""} ${mergeTargetId === id ? "is-merge-target" : ""}`}
                        d={path}
                      />
                    );
                  })}
                  {mapMode === "rooms" && splitLines.map((line, lineIndex) => (
                    <g key={`split-line-${lineIndex}`}>
                      <polyline
                        className={`robot-map-split-draft is-${splitValidation}`}
                        points={worldPointsToPolyline(line, snapshot.map!.geometry)}
                      />
                      {line.map((point, pointIndex) => {
                        const mapped = worldToPercent(point[0], point[1], snapshot.map!.geometry);
                        const endpoint = pointIndex === 0 || pointIndex === line.length - 1;
                        return (
                          <circle
                            key={`${lineIndex}-${pointIndex}`}
                            className={`robot-map-split-handle ${endpoint ? "is-endpoint" : "is-corner"}`}
                            cx={mapped.left}
                            cy={mapped.top}
                            r={endpoint ? 0.82 : 0.7}
                            onClick={(event) => event.stopPropagation()}
                            onPointerDown={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              suppressMapClick.current = true;
                              setDraggingSplitPoint({ lineIndex, pointIndex });
                            }}
                          />
                        );
                      })}
                      {line.length === 2 && (() => {
                        const center: [number, number] = [
                          (line[0][0] + line[1][0]) / 2,
                          (line[0][1] + line[1][1]) / 2,
                        ];
                        const mapped = worldToPercent(center[0], center[1], snapshot.map!.geometry);
                        return (
                          <circle
                            className="robot-map-split-handle is-bend"
                            cx={mapped.left}
                            cy={mapped.top}
                            r={0.66}
                            onClick={(event) => event.stopPropagation()}
                            onPointerDown={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              suppressMapClick.current = true;
                              setValidatedSplit(null);
                              setSplitValidation("ready");
                              setSplitValidationMessage("");
                              setSplitLines((current) => current.map((candidate, index) =>
                                index === lineIndex ? [candidate[0], center, candidate[1]] : candidate));
                              setDraggingSplitPoint({ lineIndex, pointIndex: 1 });
                            }}
                          />
                        );
                      })()}
                    </g>
                  ))}
                  {mapMode === "rooms" && pendingSplitPoint && (() => {
                    const mapped = worldToPercent(pendingSplitPoint[0], pendingSplitPoint[1], snapshot.map!.geometry);
                    return <circle className="robot-map-split-handle is-pending" cx={mapped.left} cy={mapped.top} r={0.9} />;
                  })()}
                  {mapMode === "zones" && selectedZone && selectedZoneBounds && (() => {
                    const bounds = selectedZoneBounds;
                    const corners = [
                      { point: [bounds.minX, bounds.minY] as [number, number], opposite: [bounds.maxX, bounds.maxY] as [number, number] },
                      { point: [bounds.maxX, bounds.minY] as [number, number], opposite: [bounds.minX, bounds.maxY] as [number, number] },
                      { point: [bounds.maxX, bounds.maxY] as [number, number], opposite: [bounds.minX, bounds.minY] as [number, number] },
                      { point: [bounds.minX, bounds.maxY] as [number, number], opposite: [bounds.maxX, bounds.minY] as [number, number] },
                    ];
                    const edges: Array<{ point: [number, number]; side: keyof ZoneBounds }> = [
                      { point: [(bounds.minX + bounds.maxX) / 2, bounds.minY], side: "minY" },
                      { point: [bounds.maxX, (bounds.minY + bounds.maxY) / 2], side: "maxX" },
                      { point: [(bounds.minX + bounds.maxX) / 2, bounds.maxY], side: "maxY" },
                      { point: [bounds.minX, (bounds.minY + bounds.maxY) / 2], side: "minX" },
                    ];
                    return (
                      <g className="robot-map-zone-handles">
                        {corners.map(({ point, opposite }) => {
                          const mapped = worldToPercent(point[0], point[1], snapshot.map!.geometry);
                          return (
                            <circle
                              key={`corner-${point.join("-")}`}
                              className="robot-map-zone-handle is-corner"
                              cx={mapped.left}
                              cy={mapped.top}
                              r={0.82}
                              onClick={(event) => event.stopPropagation()}
                              onPointerDown={(event) => {
                                if (!isOwner || zoneCommandPending || busy) return;
                                event.preventDefault();
                                event.stopPropagation();
                                event.currentTarget.setPointerCapture(event.pointerId);
                                suppressMapClick.current = true;
                                setDraggingZone({ type: "corner", zoneId: featureId(selectedZone), opposite });
                              }}
                            />
                          );
                        })}
                        {edges.map(({ point, side }) => {
                          const mapped = worldToPercent(point[0], point[1], snapshot.map!.geometry);
                          return (
                            <rect
                              key={`edge-${side}`}
                              className={`robot-map-zone-handle is-edge is-${side.toLowerCase()}`}
                              x={mapped.left - 0.65}
                              y={mapped.top - 0.65}
                              width={1.3}
                              height={1.3}
                              rx={0.3}
                              onClick={(event) => event.stopPropagation()}
                              onPointerDown={(event) => {
                                if (!isOwner || zoneCommandPending || busy) return;
                                event.preventDefault();
                                event.stopPropagation();
                                event.currentTarget.setPointerCapture(event.pointerId);
                                suppressMapClick.current = true;
                                setDraggingZone({ type: "edge", zoneId: featureId(selectedZone), side, bounds: { ...bounds } });
                              }}
                            />
                          );
                        })}
                      </g>
                    );
                  })()}
                  {mapMode === "zones" && selectedZone && selectedWallEndpoints && selectedWallEndpoints.map((endpoint, endpointIndex) => {
                    const mapped = worldToPercent(endpoint[0], endpoint[1], snapshot.map!.geometry);
                    const opposite = selectedWallEndpoints[endpointIndex === 0 ? 1 : 0];
                    return (
                      <circle
                        key={`wall-endpoint-${endpointIndex}`}
                        className="robot-map-virtual-wall-handle"
                        cx={mapped.left}
                        cy={mapped.top}
                        r={0.86}
                        onClick={(event) => event.stopPropagation()}
                        onPointerDown={(event) => {
                          if (!isOwner || zoneCommandPending || busy) return;
                          event.preventDefault();
                          event.stopPropagation();
                          event.currentTarget.setPointerCapture(event.pointerId);
                          suppressMapClick.current = true;
                          setDraggingZone({
                            type: "wall-endpoint",
                            zoneId: featureId(selectedZone),
                            endpointIndex: endpointIndex as 0 | 1,
                            opposite: [...opposite] as [number, number],
                            width: virtualWallWidth(selectedZone),
                          });
                        }}
                      />
                    );
                  })}
                  {mapMode === "zones" && selectedZone && validPoint(selectedZone.properties.preferred_goal) && (() => {
                    const goal = selectedZone.properties.preferred_goal;
                    const mapped = worldToPercent(goal[0], goal[1], snapshot.map!.geometry);
                    return <circle className="robot-map-zone-goal" cx={mapped.left} cy={mapped.top} r={0.92} />;
                  })()}
                </svg>
              )}
              {roomDrafts.map((room) => {
                const label = featureLabelPoint(room, snapshot.map!.geometry);
                if (!label) return null;
                return (
                  <span
                    key={`${featureId(room)}-label`}
                    className={`robot-map-room-label ${mapMode !== "rooms" ? "is-context" : ""} ${selectedRoomId === featureId(room) ? "is-selected" : ""}`}
                    style={{ left: `${label.left}%`, top: `${label.top}%` }}
                  >
                    {featureName(room, "이름 없는 방")}
                  </span>
                );
              })}
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
                  className={`robot-map-marker ${navigationDriving ? "is-driving" : ""}`}
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
          <div className="robot-map-legend">
            <span><i className="is-robot" />말벗 위치와 방향</span>
            <span><i className="is-goal" />선택·탐색 지점</span>
            <span><i className="is-route" />예상·실행 경로</span>
            {roomDrafts.length > 0 && <span><i className="is-room" />방 경계·이름</span>}
            {renderedZoneFeatures.length > 0 && (
              <>
                <span><i className="is-zone is-restricted" />진입 금지</span>
                <span><i className="is-zone is-avoid" />우회 권장</span>
                <span><i className="is-zone is-allow" />통행 허용</span>
                <span><i className="is-virtual-wall" />가상 벽</span>
              </>
            )}
          </div>
        </div>

        <aside className="robot-map-sidebar">
          {mapping ? (
            <>
              <div className="robot-map-summary">
                <h2>새로운 공간을 확인하러 이동하고 있어요</h2>
                <div className="robot-map-progress"><i style={{ width: "62%" }} /></div>
                <div className="robot-map-summary-grid">
                  <div><span>로봇 연결</span><strong>{snapshot?.online ? "정상" : "오프라인"}</strong></div>
                  <div><span>현재 위치</span><strong>{localizationShortCopy(snapshot?.state?.localization.state)}</strong></div>
                  <div><span>지도 상태</span><strong>생성 중</strong></div>
                  <div><span>현재 단계</span><strong>{snapshot?.state?.state === "review" ? "검토" : "탐색"}</strong></div>
                </div>
              </div>
              <div className="robot-map-panel-card">
                <h3>지금까지의 단계</h3>
                <ol className="robot-map-steps">
                  <li className="is-done">주변을 인식하고 있어요</li>
                  <li className="is-done">자율주행을 준비하고 있어요</li>
                  <li className="is-current">새로운 공간을 확인하러 이동하고 있어요</li>
                  <li>더 확인할 공간이 있는지 찾고 있어요</li>
                  <li>지도를 안전하게 저장하고 있어요</li>
                </ol>
              </div>
              <div className="robot-map-help">
                <span>새 지도를 저장하기 전까지 기존 지도는 그대로 유지됩니다. 중지해도 저장된 지도와 방·구역 설정은 지워지지 않아요.</span>
              </div>
              <div className="robot-map-actions is-inline">
                <button type="button" className="is-secondary" onClick={() => void sendCommand("cancel")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                  지도 생성 중지
                </button>
                <button type="button" onClick={() => void sendCommand("finish")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                  탐색 완료·저장
                </button>
              </div>
            </>
          ) : mapMode === "rooms" ? (
            <>
              <div className="robot-map-panel-card robot-map-editor-card">
                <h2>{selectedRoom ? featureName(selectedRoom, "이름 없는 방") : "편집할 방을 선택하세요"}</h2>
                <label>
                  <span>이름</span>
                  <input
                    value={selectedRoom ? featureName(selectedRoom, "") : ""}
                    disabled={!selectedRoom}
                    onChange={(event) => updateSelectedRoom({ name: event.target.value })}
                    maxLength={40}
                    placeholder="방 이름"
                  />
                </label>
                <div className="robot-map-editor-field">
                  <span>종류</span>
                  <div className="robot-map-choice-chips">
                    {([
                      ["unassigned", "미지정"],
                      ["living_room", "거실"],
                      ["bedroom", "침실"],
                      ["kitchen", "주방"],
                      ["dining_room", "식당"],
                      ["bathroom", "욕실"],
                      ["entrance", "현관"],
                      ["hallway", "복도"],
                      ["workspace", "작업 공간"],
                      ["storage", "수납 공간"],
                      ["utility", "다용도실"],
                      ["custom", "기타"],
                    ] as Array<[string, string]>).map(([category, label]) => (
                      <button
                        key={category}
                        type="button"
                        className={selectedRoom?.properties.category === category ? "is-active" : ""}
                        disabled={!selectedRoom}
                        onClick={() => updateSelectedRoom({ category })}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                {selectedRoom && (
                  <p className="robot-map-representative-note">
                    대표 위치 자동 계산됨
                    {typeof selectedRoom.properties.clearance_m === "number"
                      ? ` · 가장 가까운 벽에서 ${selectedRoom.properties.clearance_m.toFixed(2)}m`
                      : ""}
                  </p>
                )}
              </div>
              <div className="robot-map-panel-card">
                <h3>방 편집 도구</h3>
                <div className="robot-map-tool-grid">
                  {(["select", "split", "merge"] as RoomTool[]).map((tool) => (
                    <button key={tool} type="button" className={roomTool === tool ? "is-active" : ""} onClick={() => chooseRoomTool(tool)}>
                      {tool === "select" ? "방 선택" : tool === "split" ? "나누는 선 그리기" : "방 두 개 합치기"}
                    </button>
                  ))}
                </div>
                {roomTool === "split" && (
                  <div className="robot-map-split-guide">
                    <p>벽 두 곳을 누르면 선이 생깁니다. 여러 선을 만들 수 있고, 선 중앙의 작은 주황 포인터를 끌면 ㄱ자로 꺾입니다.</p>
                    <div className="robot-map-split-legend">
                      <span><i className="is-endpoint" />벽 끝점</span>
                      <span><i className="is-bend" />직각 꺾임</span>
                      <span><i className="is-pending" />다음 벽 선택 중</span>
                    </div>
                    <div className={`robot-map-split-status is-${splitValidation}`} role="status">
                      {pendingSplitPoint
                        ? "시작점을 정했습니다. 연결할 두 번째 벽을 선택하세요."
                        : splitValidation === "checking"
                          ? splitValidationMessage
                          : splitValidation === "valid"
                            ? splitValidationMessage
                          : splitValidation === "invalid"
                            ? splitValidationMessage
                            : splitLines.length > 0
                              ? `${splitLines.length}개 분할선 · 적용하면 최소 1㎡와 정확히 두 공간인지 로봇이 최종 확인합니다.`
                              : "분할선의 양 끝점은 벽에서 25cm 이내에 지정해야 합니다."}
                    </div>
                    <div className="robot-map-editor-buttons">
                      <button type="button" onClick={undoSplitPoint} disabled={!pendingSplitPoint && splitLines.length === 0}>마지막 선 되돌리기</button>
                      <button type="button" onClick={() => clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage)} disabled={!pendingSplitPoint && splitLines.length === 0}>모두 지우기</button>
                    </div>
                    <button type="button" className="robot-map-apply-tool" onClick={applyRoomSplit} disabled={splitLines.length === 0 || Boolean(pendingSplitPoint) || splitValidation === "invalid" || splitValidation === "checking" || roomCommandPending || busy}>
                      {splitValidation === "valid" ? "확인된 선대로 방 나누기" : "분할 가능 여부 확인"}
                    </button>
                  </div>
                )}
                {roomTool === "merge" && (
                  <div className="robot-map-split-guide">
                    <p><strong>{featureName(selectedRoom!, "현재 방")}</strong>과 맞닿아 있는 방 하나를 지도나 목록에서 선택하세요. 떨어진 방은 합칠 수 없습니다.</p>
                    <div className={`robot-map-split-status ${mergeTarget ? "is-ready" : "is-idle"}`}>
                      {mergeTarget ? `${featureName(selectedRoom!, "현재 방")} + ${featureName(mergeTarget, "다른 방")}` : "합칠 두 번째 방을 기다리고 있습니다."}
                    </div>
                    <button type="button" className="robot-map-apply-tool" onClick={applyRoomMerge} disabled={!mergeTarget || roomCommandPending || busy}>선택한 두 방 합치기</button>
                  </div>
                )}
              </div>
              <div className="robot-map-panel-card robot-map-list-card">
                <h3>방 목록 {roomDrafts.length}곳</h3>
                {roomDrafts.map((room, index) => (
                  <button key={featureId(room)} type="button" onClick={() => {
                    const id = featureId(room);
                    if (roomTool === "merge" && selectedRoomId && id !== selectedRoomId) {
                      setMergeTargetId(id);
                    } else {
                      setSelectedRoomId(id);
                      setMergeTargetId("");
                      if (roomTool === "split") clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
                    }
                  }}>
                    <i style={{ background: roomColor(room, index) }} />
                    {featureName(room, `공간 ${index + 1}`)}
                    {selectedRoomId === featureId(room) && <strong>현재 방</strong>}
                    {mergeTargetId === featureId(room) && <strong>합칠 방</strong>}
                  </button>
                ))}
              </div>
              <div className="robot-map-actions is-inline">
                <button type="button" className="is-secondary" onClick={() => {
                  setRoomDrafts(originalRooms);
                  setSelectedRoomId("");
                  setMergeTargetId("");
                  setRoomTool("select");
                  clearSplitDraft(setSplitLines, setPendingSplitPoint, setSplitValidation, setSplitValidationMessage);
                }} disabled={!roomsDirty}>저장 전 변경 취소</button>
                <button type="button" onClick={saveRooms} disabled={!isOwner || roomDrafts.length === 0 || !roomsDirty || roomCommandPending || busy}>방 설정 저장</button>
              </div>
            </>
          ) : mapMode === "zones" ? (
            <>
              <div className="robot-map-panel-card robot-map-editor-card">
                <div className="robot-map-editor-heading">
                  <div>
                    <small>이동 규칙</small>
                    <h2>{selectedZone ? featureName(selectedZone, "이름 없는 구역") : "편집할 구역을 선택하세요"}</h2>
                  </div>
                  <button
                    type="button"
                    className={`robot-map-zone-add ${zoneCreateMode !== "closed" ? "is-active" : ""}`}
                    onClick={() => setZoneCreateMode((current) => current === "closed" ? "menu" : "closed")}
                    disabled={!isOwner || zoneCommandPending || busy}
                  >
                    {zoneCreateMode === "closed" ? "+ 추가" : "닫기"}
                  </button>
                </div>
                {zoneCreateMode !== "closed" && (
                  <div className="robot-map-zone-create-menu">
                    <div className="robot-map-zone-create-rule">
                      <span>새 구역 규칙</span>
                      <div className="robot-map-zone-choices is-compact">
                        {(["restricted", "avoid", "allow"] as ZoneBehavior[]).map((behavior) => (
                          <button key={behavior} type="button" className={`is-${behavior} ${newZoneBehavior === behavior ? "is-active" : ""}`} onClick={() => setNewZoneBehavior(behavior)}>
                            <i />
                            <span>{zoneBehaviorLabel(behavior)}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="robot-map-zone-create-actions">
                      <button type="button" onClick={addDefaultZone}>
                        <i className="is-rectangle" />
                        <span><strong>사각형 구역</strong><small>끌어서 이동하고 크기를 조절해요</small></span>
                      </button>
                      <button type="button" onClick={addVirtualWall}>
                        <i className="is-wall" />
                        <span><strong>가상 벽</strong><small>출입구나 좁은 통로를 선으로 막아요</small></span>
                      </button>
                      <button type="button" onClick={() => setZoneCreateMode("room")} className={zoneCreateMode === "room" ? "is-active" : ""}>
                        <i className="is-room" />
                        <span><strong>방 전체 적용</strong><small>저장된 방 경계를 그대로 사용해요</small></span>
                      </button>
                    </div>
                    {zoneCreateMode === "room" && (
                      <div className="robot-map-room-zone-list">
                        {roomDrafts.map((room, index) => (
                          <button key={featureId(room)} type="button" onClick={() => addRoomAsZone(room)} disabled={!isOwner || zoneCommandPending || busy}>
                            <i style={{ background: roomColor(room, index) }} />
                            <span>{featureName(room, `공간 ${index + 1}`)}</span>
                            <strong>전체 추가</strong>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
                {selectedZone ? (
                  <>
                    <label>
                      <span>이름</span>
                      <input
                        value={featureName(selectedZone, "")}
                        maxLength={40}
                        onChange={(event) => updateSelectedZone({ name: event.target.value.slice(0, 40) })}
                        placeholder="구역 이름"
                      />
                    </label>
                    <div className="robot-map-zone-meta">
                      <span>{isVirtualWall(selectedZone) ? "길이" : "크기"}</span>
                      <strong>{isVirtualWall(selectedZone) ? formatMeters(wallLength(virtualWallEndpoints(selectedZone)!)) : formatSquareMeters(zoneArea(selectedZone))}</strong>
                    </div>
                    {isVirtualWall(selectedZone) ? (
                      <div className="robot-map-virtual-wall-note">
                        <i />
                        <span><strong>진입 금지 가상 벽</strong><small>말벗은 이 선을 가로질러 이동하지 않습니다.</small></span>
                      </div>
                    ) : (
                      <div className="robot-map-editor-field">
                        <span>이동 규칙</span>
                        <div className="robot-map-zone-choices">
                          {(["restricted", "avoid", "allow"] as ZoneBehavior[]).map((behavior) => (
                            <button
                              key={behavior}
                              type="button"
                              className={`is-${behavior} ${zoneBehaviorOf(selectedZone) === behavior ? "is-active" : ""}`}
                              onClick={() => updateSelectedZone({ behavior })}
                            >
                              <i />
                              <span>{zoneBehaviorLabel(behavior)}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                    {!isVirtualWall(selectedZone) && (
                      <label className="robot-map-zone-color">
                        <span>표시 색상</span>
                        <input type="color" value={zoneColor(selectedZone)} onChange={(event) => updateSelectedZone({ color: event.target.value })} />
                      </label>
                    )}
                    {zoneBehaviorOf(selectedZone) !== "restricted" && (
                      <div className="robot-map-zone-goal-setting">
                        <div>
                          <span>대표 목적지</span>
                          <strong>{validPoint(selectedZone.properties.preferred_goal) ? "지정됨" : "지정 안 됨"}</strong>
                        </div>
                        <button type="button" className={zoneGoalMode ? "is-active" : ""} onClick={() => setZoneGoalMode((current) => !current)}>
                          {zoneGoalMode ? "지도에서 위치 선택 중" : "대표 위치 지정"}
                        </button>
                      </div>
                    )}
                    <p>{isVirtualWall(selectedZone)
                      ? "선을 끌어 이동하고, 양 끝 포인터를 끌어 길이와 각도를 조절하세요."
                      : "구역 안을 끌어 이동하고, 네 모서리와 변의 포인터를 끌어 크기를 조절하세요."}</p>
                    <button type="button" className="robot-map-zone-delete" onClick={removeSelectedZone} disabled={zoneCommandPending || busy}>{isVirtualWall(selectedZone) ? "이 가상 벽 삭제" : "이 구역 삭제"}</button>
                  </>
                ) : (
                  <div className="robot-map-zone-empty">
                    <p>지도나 아래 목록에서 구역을 선택하세요.</p>
                    <span>추가 메뉴에서 사각형 구역·가상 벽·방 전체 적용 중 하나를 선택할 수 있습니다.</span>
                  </div>
                )}
              </div>

              <div className="robot-map-panel-card robot-map-list-card">
                <h3>구역 {zoneDrafts.length}개</h3>
                {zoneDrafts.length === 0 && <p>설정한 이동 규칙 구역이 없습니다.</p>}
                {zoneDrafts.map((zone) => (
                  <button
                    key={featureId(zone)}
                    type="button"
                    className={selectedZoneId === featureId(zone) ? "is-selected" : ""}
                    onClick={() => {
                      setSelectedZoneId(featureId(zone));
                      setZoneGoalMode(false);
                    }}
                  >
                    <i className={isVirtualWall(zone) ? "is-virtual-wall" : `is-${zoneBehaviorOf(zone)}`} />
                    <span>{featureName(zone, "이름 없는 구역")} · {isVirtualWall(zone) ? "가상 벽" : zoneBehaviorLabel(zoneBehaviorOf(zone))}</span>
                    {selectedZoneId === featureId(zone) && <strong>편집 중</strong>}
                  </button>
                ))}
              </div>
              <div className="robot-map-actions is-inline">
                <button type="button" className="is-secondary" onClick={() => {
                  setZoneDrafts(zoneFeatures.map(cloneFeature));
                  setSelectedZoneId("");
                  setZoneGoalMode(false);
                }} disabled={!zonesDirty || zoneCommandPending || busy}>저장 전 변경 취소</button>
                <button type="button" onClick={saveZones} disabled={!isOwner || !zonesDirty || zoneCommandPending || busy}>구역 설정 저장</button>
              </div>
              <div className="robot-map-help"><span>구역은 방 이름이 아니라 말벗의 이동 규칙입니다. 진입 금지를 해제하면 해당 공간으로 들어갈 수 있어요.</span></div>
            </>
          ) : (
            <>
              <div className={`robot-map-summary ${navigationDriving ? "is-driving" : navigationSucceeded ? "is-succeeded" : ""}`}>
                <small>지금 말벗은</small>
                <h2>
                  {navigationDriving ? "주변 장애물을 확인하며 이동하고 있어요"
                    : navigationSucceeded ? "선택한 목적지에 도착했어요"
                    : previewToken ? "선택한 위치까지 이동할 수 있어요"
                      : mapMode === "navigate" ? "지도에서 보낼 곳을 선택해 주세요"
                        : "저장된 지도를 사용하고 있어요"}
                </h2>
                {(navigationDriving || navigationSucceeded) && (
                  <div className="robot-map-progress-row">
                    <div
                      className="robot-map-progress"
                      role="progressbar"
                      aria-label="목적지 이동 진행률"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={navigationProgress}
                    >
                      <i style={{ width: `${navigationProgress}%` }} />
                    </div>
                    <strong>{navigationProgress}%</strong>
                  </div>
                )}
                <div className="robot-map-summary-grid">
                  {mapMode === "navigate" || navigationDriving || navigationSucceeded ? (
                    <>
                      <div><span>{navigationDriving || navigationSucceeded ? "남은 거리" : "거리"}</span><strong>{navigationSucceeded ? "0.0m" : formatMeters(navigationDriving ? navigation?.distance_remaining_m : isRecord(navigationPreview?.path) ? navigationPreview.path.length_m : null)}</strong></div>
                      <div><span>{navigationDriving || navigationSucceeded ? "도착까지" : "예상 시간"}</span><strong>{navigationSucceeded ? "도착" : formatEta(navigationDriving ? navigation?.estimated_time_remaining_s : estimateSeconds(navigationPreview))}</strong></div>
                      <div><span>현재 위치</span><strong>{localizationShortCopy(snapshot?.state?.localization.state)}</strong></div>
                      <div><span>구역 확인</span><strong>{previewToken || navigationDriving || navigationSucceeded ? "문제 없음" : "선택 전"}</strong></div>
                    </>
                  ) : (
                    <>
                      <div><span>로봇 연결</span><strong>{snapshot?.online ? "정상" : "오프라인"}</strong></div>
                      <div><span>현재 위치</span><strong>{localizationShortCopy(snapshot?.state?.localization.state)}</strong></div>
                      <div><span>지도 상태</span><strong>{snapshot?.map?.finalized ? "저장됨" : "생성 중"}</strong></div>
                      <div><span>주행 상태</span><strong>{navigationDriving ? "이동 중" : "대기"}</strong></div>
                    </>
                  )}
                </div>
                {previewToken && numberValue(navigationPreview?.snap_distance_m) > 0 && (
                  <div className="robot-map-snap-note">누른 곳에서 {Math.round(numberValue(navigationPreview?.snap_distance_m) * 100)}cm 옆의 안전한 바닥에 도착해요.</div>
                )}
                <div className="robot-map-actions is-inline">
                  {mapMode === "navigate" && previewToken && !navigationDriving && (
                    <>
                      <button type="button" className="is-secondary" onClick={() => { setNavigationPreview(null); setPreviewExpiresAt(0); }}>다시 선택</button>
                      <button type="button" onClick={() => void sendCommand("navigation_start", { previewToken })} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>이 위치로 이동</button>
                    </>
                  )}
                  {navigationDriving && typeof navigation?.session_id === "string" && (
                    <button type="button" className="is-danger" onClick={() => void sendCommand("navigation_cancel", { sessionId: navigation.session_id })} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>이동 취소</button>
                  )}
                </div>
              </div>
              {mapMode === "navigate" && roomDrafts.length > 0 && (
                <div className="robot-map-panel-card">
                  <h3>방 이름으로 보내기</h3>
                  <div className="robot-map-choice-chips">
                    {roomDrafts.map((room) => (
                      <button key={featureId(room)} type="button" onClick={() => {
                        const point = featureWorldLabelPoint(room);
                        if (point) void sendCommand("navigation_preview", { x: point[0], y: point[1] });
                      }}>{featureName(room, "공간")}</button>
                    ))}
                  </div>
                  <p>방마다 정해 둔 대표 위치로 이동합니다.</p>
                </div>
              )}
              {mapMode === "view" && (
                <div className="robot-map-panel-card">
                  <h3>지도 관리</h3>
                  <p>{snapshot?.state?.message ?? "말벗의 현재 위치와 저장된 공간을 실시간으로 확인합니다."}</p>
                  <button type="button" className="robot-map-apply-tool" onClick={() => void sendCommand("start")} disabled={!isOwner || !snapshot?.online || Boolean(activeCommand) || busy}>
                    <MapTrifold size={17} weight="bold" /> 지도 다시 만들기
                  </button>
                </div>
              )}
              <div className="robot-map-help">
                <strong>선택할 수 없는 위치일 때</strong>
                <span>장애물 · 미탐색 공간 · 진입 금지 구역 · 이어진 길이 없는 위치는 이유와 다음 행동을 함께 안내합니다.</span>
              </div>
            </>
          )}
          {!isOwner && <small className="robot-map-owner-note">지도 생성과 공간 편집, 목적지 이동은 소유자 계정에서만 할 수 있습니다.</small>}
          {notice && <p className="robot-map-notice" role="status">{notice}</p>}
        </aside>
      </div>
    </section>
  );
}

export function RobotMapSummaryOverlay({
  snapshot,
  semantics,
}: {
  snapshot: RobotSnapshot | null;
  semantics: RobotSemantics | null;
}) {
  const map = snapshot?.map;
  if (!map) return null;

  const semanticsMatch = semantics?.revision === map.revision;
  const userFeatures = semanticsMatch ? featuresOf(semantics?.userMap) : [];
  const rooms = userFeatures.filter((feature) => feature.properties.role === "room");
  const walkableArea = userFeatures.find((feature) => feature.properties.role === "walkable_area") ?? null;
  const zones = semanticsMatch ? featuresOf(semantics?.zones) : [];
  const pose = snapshot?.state?.localization.state === "ok" ? snapshot.state.pose : null;
  const mappedPose = pose ? worldToPercent(pose.x, pose.y, map.geometry) : null;
  const marker = mappedPose && mappedPose.left >= 0 && mappedPose.left <= 100 && mappedPose.top >= 0 && mappedPose.top <= 100
    ? {
        ...mappedPose,
        heading: -(pose!.yaw - map.geometry.originYaw) * 180 / Math.PI,
      }
    : null;
  const targetState = snapshot?.state?.target?.state;
  const driving = targetState === "driving" || targetState === "canceling";

  return (
    <>
      {(rooms.length > 0 || zones.length > 0) && (
        <svg
          className="robot-map-semantics robot-map-home-semantics"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <pattern id="home-restricted-hatch" width="1.8" height="1.8" patternUnits="userSpaceOnUse">
              <rect width="1.8" height="1.8" fill="rgba(192,64,47,.08)" />
              <path d="M-.45 1.35 L1.35 -.45 M.45 2.25 L2.25 .45" stroke="rgba(192,64,47,.48)" strokeWidth=".18" />
            </pattern>
            <pattern id="home-avoid-dots" width="2.2" height="2.2" patternUnits="userSpaceOnUse">
              <rect width="2.2" height="2.2" fill="rgba(200,144,26,.07)" />
              <circle cx="1.1" cy="1.1" r=".22" fill="rgba(200,144,26,.65)" />
            </pattern>
          </defs>
          {zones.map((zone) => {
            const id = featureId(zone);
            const wall = virtualWallEndpoints(zone);
            if (wall) {
              const start = worldToPercent(wall[0][0], wall[0][1], map.geometry);
              const end = worldToPercent(wall[1][0], wall[1][1], map.geometry);
              return (
                <line
                  key={id}
                  className="robot-map-virtual-wall"
                  x1={start.left}
                  y1={start.top}
                  x2={end.left}
                  y2={end.top}
                  stroke={zoneColor(zone)}
                />
              );
            }
            const path = featureGeometryPath(zone, map.geometry);
            if (!path) return null;
            const behavior = zoneBehaviorOf(zone);
            return (
              <path
                key={id}
                className={`robot-map-zone-shape is-${behavior}`}
                d={path}
                fill={behavior === "restricted" ? "url(#home-restricted-hatch)" : behavior === "avoid" ? "url(#home-avoid-dots)" : "rgba(46,125,81,.15)"}
                fillRule="evenodd"
                stroke={zoneColor(zone)}
              />
            );
          })}
          {walkableArea && rooms.map((room) => {
            const path = roomInternalBoundaryPath(
              room,
              walkableArea,
              map.geometry,
              map.geometry.resolution,
            );
            return path ? (
              <path
                key={featureId(room)}
                className="robot-map-room-divider is-context"
                d={path}
              />
            ) : null;
          })}
        </svg>
      )}
      {rooms.map((room) => {
        const label = featureLabelPoint(room, map.geometry);
        return label ? (
          <span
            key={`${featureId(room)}-home-label`}
            className="robot-map-room-label is-context is-home-summary"
            style={{ left: `${label.left}%`, top: `${label.top}%` }}
          >
            {featureName(room, "이름 없는 방")}
          </span>
        ) : null;
      })}
      {marker && (
        <span
          className={`robot-map-marker robot-map-home-marker ${driving ? "is-driving" : ""}`}
          aria-label="말벗 현재 위치"
          style={{ left: `${marker.left}%`, top: `${marker.top}%`, transform: `translate(-50%, -50%) rotate(${marker.heading}deg)` }}
        >
          <span />
        </span>
      )}
    </>
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

function featuresOf(value: unknown): GeoFeature[] {
  if (!isRecord(value) || !Array.isArray(value.features)) return [];
  return value.features.filter(isGeoFeature);
}

function isGeoFeature(value: unknown): value is GeoFeature {
  return isRecord(value) && value.type === "Feature" &&
    isRecord(value.properties) && isRecord(value.geometry) &&
    typeof value.geometry.type === "string" && "coordinates" in value.geometry;
}

function featureId(feature: GeoFeature) {
  const propertyId = feature.properties.room_id ?? feature.properties.zone_id;
  if (typeof feature.id === "string" && feature.id) return feature.id;
  if (typeof propertyId === "string" && propertyId) return propertyId;
  return JSON.stringify(feature.geometry).slice(0, 80);
}

function featureName(feature: GeoFeature, fallback: string) {
  return typeof feature.properties.name === "string" && feature.properties.name.trim()
    ? feature.properties.name.trim()
    : fallback;
}

function featureContains(feature: GeoFeature, x: number, y: number) {
  return pointInGeometry([x, y], feature.geometry);
}

function pointInRing(point: [number, number], ring: unknown[]) {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const currentPoint = ring[index];
    const previousPoint = ring[previous];
    if (!validPoint(currentPoint) || !validPoint(previousPoint)) continue;
    const intersects = (currentPoint[1] > point[1]) !== (previousPoint[1] > point[1]) &&
      point[0] < (previousPoint[0] - currentPoint[0]) * (point[1] - currentPoint[1]) /
        (previousPoint[1] - currentPoint[1] || Number.EPSILON) + currentPoint[0];
    if (intersects) inside = !inside;
  }
  return inside;
}

function pointInPolygon(point: [number, number], polygon: unknown[]) {
  const [outer, ...holes] = polygon;
  return Array.isArray(outer) && pointInRing(point, outer) &&
    !holes.some((hole) => Array.isArray(hole) && pointInRing(point, hole));
}

function pointInGeometry(point: [number, number], geometry: GeoFeature["geometry"]) {
  if (!Array.isArray(geometry.coordinates)) return false;
  if (geometry.type === "Polygon") return pointInPolygon(point, geometry.coordinates);
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.some((polygon) => Array.isArray(polygon) && pointInPolygon(point, polygon));
  }
  return false;
}

function geometryRings(geometry: GeoFeature["geometry"]): unknown[][] {
  if (!Array.isArray(geometry.coordinates)) return [];
  if (geometry.type === "Polygon") return geometry.coordinates.filter(Array.isArray) as unknown[][];
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.flatMap((polygon) => Array.isArray(polygon)
      ? polygon.filter(Array.isArray) as unknown[][]
      : []);
  }
  return [];
}

function pointSegmentDistance(point: [number, number], first: [number, number], second: [number, number]) {
  const nearest = nearestPointOnSegment(point, first, second);
  return Math.hypot(point[0] - nearest[0], point[1] - nearest[1]);
}

function nearestPointOnSegment(point: [number, number], first: [number, number], second: [number, number]): [number, number] {
  const dx = second[0] - first[0];
  const dy = second[1] - first[1];
  const squared = dx * dx + dy * dy;
  if (squared === 0) return first;
  const amount = Math.max(0, Math.min(1,
    ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / squared));
  return [first[0] + amount * dx, first[1] + amount * dy];
}

function pointNearRoomWall(point: [number, number], geometry: GeoFeature["geometry"], tolerance = 0.25) {
  return geometryRings(geometry).some((ring) => ring.slice(1).some((second, index) => {
    const first = ring[index];
    return validPoint(first) && validPoint(second) && pointSegmentDistance(point, first, second) <= tolerance;
  }));
}

function snapToRoomWall(
  point: [number, number],
  geometry: GeoFeature["geometry"],
  tolerance = 0.25,
): [number, number] | null {
  let nearest: [number, number] | null = null;
  let nearestDistance = Number.POSITIVE_INFINITY;
  for (const ring of geometryRings(geometry)) {
    for (let index = 1; index < ring.length; index += 1) {
      const first = ring[index - 1];
      const second = ring[index];
      if (!validPoint(first) || !validPoint(second)) continue;
      const candidate = nearestPointOnSegment(point, first, second);
      const distance = Math.hypot(point[0] - candidate[0], point[1] - candidate[1]);
      if (distance < nearestDistance) {
        nearest = candidate;
        nearestDistance = distance;
      }
    }
  }
  return nearest && nearestDistance <= tolerance
    ? [roundMapCoordinate(nearest[0]), roundMapCoordinate(nearest[1])]
    : null;
}

function distanceToRoomWall(point: [number, number], geometry: GeoFeature["geometry"]) {
  let distance = Number.POSITIVE_INFINITY;
  for (const ring of geometryRings(geometry)) {
    for (let index = 1; index < ring.length; index += 1) {
      const first = ring[index - 1];
      const second = ring[index];
      if (validPoint(first) && validPoint(second)) {
        distance = Math.min(distance, pointSegmentDistance(point, first, second));
      }
    }
  }
  return distance;
}

function segmentRunsThroughRoom(
  first: [number, number],
  second: [number, number],
  geometry: GeoFeature["geometry"],
  resolution: number,
) {
  const length = Math.hypot(second[0] - first[0], second[1] - first[1]);
  const steps = Math.max(4, Math.ceil(length / Math.max(0.025, resolution / 2)));
  for (let index = 1; index < steps; index += 1) {
    const ratio = index / steps;
    const point: [number, number] = [
      first[0] + (second[0] - first[0]) * ratio,
      first[1] + (second[1] - first[1]) * ratio,
    ];
    const nearEndpoint = ratio < 0.08 || ratio > 0.92;
    if (!pointInGeometry(point, geometry) &&
        !(nearEndpoint && pointNearRoomWall(point, geometry, resolution))) return false;
    if (!nearEndpoint && distanceToRoomWall(point, geometry) < resolution * 0.25) return false;
  }
  return true;
}

function orthogonalCorner(
  point: [number, number],
  previous: [number, number],
  next: [number, number],
  geometry: GeoFeature["geometry"],
): [number, number] {
  const candidates: Array<[number, number]> = [
    [previous[0], next[1]],
    [next[0], previous[1]],
  ];
  const usable = candidates.filter((candidate) =>
    pointInGeometry(candidate, geometry) || pointNearRoomWall(candidate, geometry));
  return (usable.length > 0 ? usable : candidates).reduce((closest, candidate) =>
    Math.hypot(point[0] - candidate[0], point[1] - candidate[1]) <
      Math.hypot(point[0] - closest[0], point[1] - closest[1])
      ? candidate
      : closest);
}

function validateSplitDraft(
  room: GeoFeature,
  lines: SplitLine[],
  pendingPoint: [number, number] | null,
  resolution: number,
) {
  if (pendingPoint) return "선택 중인 시작점과 연결할 두 번째 벽을 지정하세요.";
  if (lines.length === 0) return "분할선을 하나 이상 만드세요.";
  for (const line of lines) {
    if (line.length < 2) return "각 분할선의 양 끝점을 지정하세요.";
    if (!pointNearRoomWall(line[0], room.geometry, 0.25) ||
        !pointNearRoomWall(line[line.length - 1], room.geometry, 0.25)) {
      return "분할선의 양 끝점은 방 벽에서 25cm 이내에 놓아야 합니다.";
    }
    if (line.slice(1, -1).some((point) =>
      !pointInGeometry(point, room.geometry) && !pointNearRoomWall(point, room.geometry, 0.25))) {
      return "분할선의 꺾임점은 방 안에 놓아야 합니다.";
    }
    if (line.slice(1).some((point, index) =>
      Math.hypot(point[0] - line[index][0], point[1] - line[index][1]) < Math.max(0.08, resolution * 2))) {
      return "분할선 구간이 너무 짧습니다.";
    }
    if (line.slice(1).some((point, index) =>
      !segmentRunsThroughRoom(line[index], point, room.geometry, resolution))) {
      return "분할선은 벽이나 장애물을 따라가지 않고 방 안쪽의 열린 바닥을 가로질러야 합니다.";
    }
  }
  return "";
}

function splitErrorMessage(message: string) {
  const translations: Record<string, string> = {
    "at least one split divider is required": "분할선을 하나 이상 만드세요.",
    "each split divider must contain at least two finite points": "각 분할선의 양 끝점을 지정하세요.",
    "split divider points must be near a Room wall": "분할선의 점을 방 벽 근처에 놓으세요.",
    "split divider endpoints must be near a Room wall": "분할선의 양 끝점을 방 벽에서 25cm 이내에 놓으세요.",
    "split divider control points must stay in the Room": "분할선의 꺾임점은 방 안에 놓으세요.",
    "split divider segments are too short": "분할선 구간이 너무 짧습니다.",
    "the divider must cut the selected Room into exactly two meaningful areas": "분할선을 이어서 선택한 방을 각각 1㎡ 이상인 정확히 두 공간으로 나누세요.",
  };
  return translations[message] ?? message;
}

function mergeErrorMessage(message: string) {
  const translations: Record<string, string> = {
    "exactly two Rooms are required for a merge": "합칠 방을 정확히 두 곳 선택하세요.",
    "all selected features must be Rooms": "방으로 지정된 공간만 합칠 수 있습니다.",
    "two different Rooms are required for a merge": "현재 방과 다른 방을 선택하세요.",
    "only adjacent Rooms can be merged": "서로 맞닿아 있는 두 방만 합칠 수 있습니다.",
  };
  return translations[message] ?? message;
}

function featureGeometryPath(
  feature: GeoFeature,
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"],
) {
  const pathForRing = (ring: unknown[]) => {
    const points = ring.filter(validPoint);
    if (points.length < 3) return "";
    return `${points.map((point, index) => {
      const mapped = worldToPercent(point[0], point[1], geometry);
      return `${index === 0 ? "M" : "L"}${mapped.left},${mapped.top}`;
    }).join(" ")} Z`;
  };
  if (!Array.isArray(feature.geometry.coordinates)) return "";
  if (feature.geometry.type === "Polygon") {
    return feature.geometry.coordinates
      .filter(Array.isArray)
      .map((ring) => pathForRing(ring as unknown[]))
      .filter(Boolean)
      .join(" ");
  }
  if (feature.geometry.type === "MultiPolygon") {
    return feature.geometry.coordinates.flatMap((polygon) => Array.isArray(polygon)
      ? polygon.filter(Array.isArray).map((ring) => pathForRing(ring as unknown[]))
      : []).filter(Boolean).join(" ");
  }
  return "";
}

function roomInternalBoundaryPath(
  room: GeoFeature,
  walkableArea: GeoFeature,
  mapGeometry: NonNullable<RobotSnapshot["map"]>["geometry"],
  resolution: number,
) {
  const commands: string[] = [];
  const outerBoundaryTolerance = Math.max(0.08, resolution * 1.75);
  for (const ring of geometryRings(room.geometry)) {
    for (let index = 1; index < ring.length; index += 1) {
      const first = ring[index - 1];
      const second = ring[index];
      if (!validPoint(first) || !validPoint(second)) continue;
      const midpoint: [number, number] = [
        (first[0] + second[0]) / 2,
        (first[1] + second[1]) / 2,
      ];
      // The saved PNG already owns every wall/obstacle outline. Only draw
      // semantic dividers that were introduced by splitting Rooms.
      if (pointNearRoomWall(midpoint, walkableArea.geometry, outerBoundaryTolerance)) continue;
      const start = worldToPercent(first[0], first[1], mapGeometry);
      const end = worldToPercent(second[0], second[1], mapGeometry);
      commands.push(`M${start.left},${start.top} L${end.left},${end.top}`);
    }
  }
  return commands.join(" ");
}

function worldPointsToPolyline(
  points: Array<[number, number]>,
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"],
) {
  return points.map(([x, y]) => {
    const mapped = worldToPercent(x, y, geometry);
    return `${mapped.left},${mapped.top}`;
  }).join(" ");
}

function featureWorldLabelPoint(feature: GeoFeature): [number, number] | null {
  for (const key of ["representative_point", "centroid"]) {
    const point = feature.properties[key];
    if (validPoint(point)) return point;
  }
  if (feature.geometry.type !== "Polygon" || !Array.isArray(feature.geometry.coordinates)) return null;
  const ring = feature.geometry.coordinates[0];
  if (!Array.isArray(ring)) return null;
  const points = ring.filter(validPoint);
  if (points.length === 0) return null;
  return [
    points.reduce((sum, point) => sum + point[0], 0) / points.length,
    points.reduce((sum, point) => sum + point[1], 0) / points.length,
  ];
}

function featureLabelPoint(
  feature: GeoFeature,
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"],
) {
  const point = validPoint(feature.properties.centroid)
    ? feature.properties.centroid
    : featureWorldLabelPoint(feature);
  return point ? worldToPercent(point[0], point[1], geometry) : null;
}

function pointerToWorld(
  clientX: number,
  clientY: number,
  canvas: HTMLDivElement,
  geometry: NonNullable<RobotSnapshot["map"]>["geometry"],
): [number, number] {
  const bounds = canvas.getBoundingClientRect();
  const localX = (clientX - bounds.left) / bounds.width * geometry.width * geometry.resolution;
  const localY = (1 - (clientY - bounds.top) / bounds.height) * geometry.height * geometry.resolution;
  const cosine = Math.cos(geometry.originYaw);
  const sine = Math.sin(geometry.originYaw);
  return [
    roundMapCoordinate(geometry.originX + cosine * localX - sine * localY),
    roundMapCoordinate(geometry.originY + sine * localX + cosine * localY),
  ];
}

function roundMapCoordinate(value: number) {
  return Number(value.toFixed(3));
}

function clearSplitDraft(
  setLines: Dispatch<SetStateAction<SplitLine[]>>,
  setPending: Dispatch<SetStateAction<[number, number] | null>>,
  setValidation: Dispatch<SetStateAction<SplitValidation>>,
  setMessage: Dispatch<SetStateAction<string>>,
) {
  setLines([]);
  setPending(null);
  setValidation("idle");
  setMessage("");
}

function replaceRoomsAtFirstIndex(current: GeoFeature[], sourceIds: string[], replacement: GeoFeature[]) {
  const source = new Set(sourceIds);
  const firstIndex = current.findIndex((room) => source.has(featureId(room)));
  if (firstIndex < 0) return current;
  const retained = current.filter((room) => !source.has(featureId(room)));
  const insertAt = Math.min(firstIndex, retained.length);
  return [...retained.slice(0, insertAt), ...replacement, ...retained.slice(insertAt)];
}

function cloneFeature(feature: GeoFeature): GeoFeature {
  return JSON.parse(JSON.stringify(feature)) as GeoFeature;
}

function roomSnapshotKey(rooms: GeoFeature[]) {
  return JSON.stringify(
    [...rooms].sort((left, right) => featureId(left).localeCompare(featureId(right))),
  );
}

function zoneSnapshotKey(zones: GeoFeature[]) {
  return JSON.stringify(
    zones.map(normalizeZoneFeature)
      .sort((left, right) => featureId(left).localeCompare(featureId(right))),
  );
}

function polygonOuterRing(feature: GeoFeature): Array<[number, number]> | null {
  const polygons = polygonGeometries(feature.geometry);
  return polygons[0]?.[0]?.map((point) => [...point] as [number, number]) ?? null;
}

function polygonGeometries(geometry: GeoFeature["geometry"]): PolygonCoordinates[] {
  if (!Array.isArray(geometry.coordinates)) return [];
  const parsePolygon = (value: unknown): PolygonCoordinates | null => {
    if (!Array.isArray(value)) return null;
    const rings = value.flatMap((ring) => {
      if (!Array.isArray(ring)) return [];
      const points = ring.filter(validPoint).map((point) => [...point] as [number, number]);
      return points.length >= 4 ? [points] : [];
    });
    return rings.length > 0 ? rings : null;
  };
  if (geometry.type === "Polygon") {
    const polygon = parsePolygon(geometry.coordinates);
    return polygon ? [polygon] : [];
  }
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.flatMap((value) => {
      const polygon = parsePolygon(value);
      return polygon ? [polygon] : [];
    });
  }
  return [];
}

function rectangleRing(firstX: number, firstY: number, secondX: number, secondY: number): Array<[number, number]> {
  const minX = roundMapCoordinate(Math.min(firstX, secondX));
  const maxX = roundMapCoordinate(Math.max(firstX, secondX));
  const minY = roundMapCoordinate(Math.min(firstY, secondY));
  const maxY = roundMapCoordinate(Math.max(firstY, secondY));
  return [
    [minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY], [minX, minY],
  ];
}

function rectangleBounds(ring: Array<[number, number]>): ZoneBounds {
  const points = ring.slice(0, -1);
  return {
    minX: Math.min(...points.map((point) => point[0])),
    maxX: Math.max(...points.map((point) => point[0])),
    minY: Math.min(...points.map((point) => point[1])),
    maxY: Math.max(...points.map((point) => point[1])),
  };
}

function isRectangleRing(ring: Array<[number, number]>) {
  if (ring.length !== 5 || ring[0][0] !== ring[4][0] || ring[0][1] !== ring[4][1]) return false;
  const bounds = rectangleBounds(ring);
  const corners = new Set(ring.slice(0, -1).map((point) => `${point[0]},${point[1]}`));
  const expected = new Set([
    `${bounds.minX},${bounds.minY}`,
    `${bounds.maxX},${bounds.minY}`,
    `${bounds.maxX},${bounds.maxY}`,
    `${bounds.minX},${bounds.maxY}`,
  ]);
  return corners.size === 4 && [...corners].every((corner) => expected.has(corner));
}

function defaultZoneRing(boundary: GeoFeature, zoneIndex: number) {
  const points = geometryRings(boundary.geometry).flat().filter(validPoint);
  if (points.length === 0) return null;
  const minX = Math.min(...points.map((point) => point[0]));
  const maxX = Math.max(...points.map((point) => point[0]));
  const minY = Math.min(...points.map((point) => point[1]));
  const maxY = Math.max(...points.map((point) => point[1]));
  const centers: Array<[number, number]> = [[(minX + maxX) / 2, (minY + maxY) / 2]];
  for (let row = 1; row < 10; row += 1) {
    for (let column = 1; column < 10; column += 1) {
      centers.push([
        minX + (maxX - minX) * column / 10,
        minY + (maxY - minY) * row / 10,
      ]);
    }
  }
  const span = Math.min(maxX - minX, maxY - minY);
  const sizes = [...new Set([Math.min(1.2, span * 0.15), 0.8, 0.5, 0.4]
    .map((size) => roundMapCoordinate(size)))]
    .filter((size) => size >= 0.4);
  for (const size of sizes) {
    const valid = centers.map((center) => rectangleRing(
      center[0] - size / 2,
      center[1] - size / 2,
      center[0] + size / 2,
      center[1] + size / 2,
    )).filter((ring) => !zoneRingValidationError(ring, boundary, 0.05));
    if (valid.length > 0) return valid[zoneIndex % valid.length];
  }
  return null;
}

function isVirtualWall(zone: GeoFeature) {
  return zone.properties.geometry_kind === "virtual_wall" && virtualWallEndpoints(zone) !== null;
}

function virtualWallEndpoints(zone: GeoFeature): [[number, number], [number, number]] | null {
  const value = zone.properties.wall_endpoints;
  if (!Array.isArray(value) || value.length !== 2 || !validPoint(value[0]) || !validPoint(value[1])) return null;
  return [
    [...value[0]] as [number, number],
    [...value[1]] as [number, number],
  ];
}

function virtualWallWidth(zone: GeoFeature) {
  const value = zone.properties.wall_width_m;
  return typeof value === "number" && Number.isFinite(value) && value >= 0.05 && value <= 0.3
    ? value
    : 0.12;
}

function wallLength(endpoints: [[number, number], [number, number]]) {
  return Math.hypot(
    endpoints[1][0] - endpoints[0][0],
    endpoints[1][1] - endpoints[0][1],
  );
}

function virtualWallRing(
  endpoints: [[number, number], [number, number]],
  width: number,
): Array<[number, number]> {
  const [first, second] = endpoints;
  const length = Math.max(wallLength(endpoints), Number.EPSILON);
  const offsetX = -(second[1] - first[1]) / length * width / 2;
  const offsetY = (second[0] - first[0]) / length * width / 2;
  const startLeft: [number, number] = [roundMapCoordinate(first[0] + offsetX), roundMapCoordinate(first[1] + offsetY)];
  const endLeft: [number, number] = [roundMapCoordinate(second[0] + offsetX), roundMapCoordinate(second[1] + offsetY)];
  const endRight: [number, number] = [roundMapCoordinate(second[0] - offsetX), roundMapCoordinate(second[1] - offsetY)];
  const startRight: [number, number] = [roundMapCoordinate(first[0] - offsetX), roundMapCoordinate(first[1] - offsetY)];
  return [startLeft, endLeft, endRight, startRight, startLeft];
}

function defaultVirtualWall(boundary: GeoFeature, zoneIndex: number) {
  const points = geometryRings(boundary.geometry).flat().filter(validPoint);
  if (points.length === 0) return null;
  const minX = Math.min(...points.map((point) => point[0]));
  const maxX = Math.max(...points.map((point) => point[0]));
  const minY = Math.min(...points.map((point) => point[1]));
  const maxY = Math.max(...points.map((point) => point[1]));
  const centers: Array<[number, number]> = [[(minX + maxX) / 2, (minY + maxY) / 2]];
  for (let row = 1; row < 10; row += 1) {
    for (let column = 1; column < 10; column += 1) {
      centers.push([
        minX + (maxX - minX) * column / 10,
        minY + (maxY - minY) * row / 10,
      ]);
    }
  }
  const width = 0.12;
  for (const length of [1.4, 1.0, 0.7]) {
    const candidates = centers.flatMap((center) => [
      [[center[0] - length / 2, center[1]], [center[0] + length / 2, center[1]]] as [[number, number], [number, number]],
      [[center[0], center[1] - length / 2], [center[0], center[1] + length / 2]] as [[number, number], [number, number]],
    ]).filter((endpoints) => !zoneRingValidationError(
      virtualWallRing(endpoints, width),
      boundary,
      0.05,
      0.02,
    ));
    if (candidates.length > 0) return { endpoints: candidates[zoneIndex % candidates.length], width };
  }
  return null;
}

function ringSignedMetrics(ring: Array<[number, number]>) {
  let twiceArea = 0;
  let centerX = 0;
  let centerY = 0;
  for (let index = 0; index < ring.length - 1; index += 1) {
    const first = ring[index];
    const second = ring[index + 1];
    const cross = first[0] * second[1] - second[0] * first[1];
    twiceArea += cross;
    centerX += (first[0] + second[0]) * cross;
    centerY += (first[1] + second[1]) * cross;
  }
  const signedArea = twiceArea / 2;
  if (Math.abs(signedArea) < 1e-8) return { area: 0, centroid: null as [number, number] | null };
  return {
    area: signedArea,
    centroid: [centerX / (3 * twiceArea), centerY / (3 * twiceArea)] as [number, number],
  };
}

function polygonMetrics(coordinates: PolygonCoordinates) {
  const outer = ringSignedMetrics(coordinates[0]);
  if (!outer.centroid) return { area: 0, centroid: null as [number, number] | null };
  let area = Math.abs(outer.area);
  let weightedX = outer.centroid[0] * area;
  let weightedY = outer.centroid[1] * area;
  for (const hole of coordinates.slice(1)) {
    const metrics = ringSignedMetrics(hole);
    const holeArea = Math.abs(metrics.area);
    if (!metrics.centroid || holeArea <= 0) continue;
    area -= holeArea;
    weightedX -= metrics.centroid[0] * holeArea;
    weightedY -= metrics.centroid[1] * holeArea;
  }
  return {
    area: Math.max(0, area),
    centroid: area > 1e-8
      ? [roundMapCoordinate(weightedX / area), roundMapCoordinate(weightedY / area)] as [number, number]
      : null,
  };
}

function roundArea(value: number) {
  return Number(value.toFixed(2));
}

function zoneArea(zone: GeoFeature) {
  const polygon = polygonGeometries(zone.geometry)[0];
  return polygon ? polygonMetrics(polygon).area : 0;
}

function formatSquareMeters(value: number) {
  return `${value.toFixed(2)} m²`;
}

function createZoneFeature(ring: Array<[number, number]>, name: string, behavior: ZoneBehavior): GeoFeature {
  const closedRing = closeRing(ring);
  const metrics = polygonMetrics([closedRing]);
  const id = globalThis.crypto?.randomUUID?.() ?? `zone-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return {
    type: "Feature",
    id,
    properties: {
      role: "semantic_zone",
      zone_id: id,
      name,
      behavior,
      color: defaultZoneColor(behavior),
      area_m2: roundArea(metrics.area),
      centroid: metrics.centroid,
    },
    geometry: { type: "Polygon", coordinates: [closedRing] },
  };
}

function createVirtualWallFeature(
  endpoints: [[number, number], [number, number]],
  name: string,
  width: number,
) {
  const zone = createZoneFeature(virtualWallRing(endpoints, width), name, "restricted");
  zone.properties.geometry_kind = "virtual_wall";
  zone.properties.wall_endpoints = endpoints.map((point) => [...point]);
  zone.properties.wall_width_m = width;
  zone.properties.color = "#C0402F";
  return zone;
}

function normalizeZoneFeature(zone: GeoFeature): GeoFeature {
  const normalized = cloneFeature(zone);
  normalized.properties.role = "semantic_zone";
  normalized.properties.zone_id = featureId(normalized);
  normalized.properties.name = featureName(normalized, "이름 없는 구역").slice(0, 40);
  normalized.properties.behavior = zoneBehaviorOf(normalized);
  normalized.properties.color = zoneColor(normalized);
  delete normalized.properties.room_id;
  delete normalized.properties.room_name;
  delete normalized.properties.needs_review;
  const wall = virtualWallEndpoints(normalized);
  if (normalized.properties.geometry_kind === "virtual_wall" && wall) {
    const width = virtualWallWidth(normalized);
    normalized.properties.behavior = "restricted";
    normalized.properties.wall_endpoints = wall;
    normalized.properties.wall_width_m = width;
    normalized.geometry = { type: "Polygon", coordinates: [virtualWallRing(wall, width)] };
  }
  const polygon = polygonGeometries(normalized.geometry)[0];
  if (polygon) {
    normalized.geometry = {
      type: "Polygon",
      coordinates: polygon.map(closeRing),
    };
    const metrics = polygonMetrics(polygon);
    normalized.properties.area_m2 = roundArea(metrics.area);
    normalized.properties.centroid = metrics.centroid;
  }
  if (zoneBehaviorOf(normalized) === "restricted") delete normalized.properties.preferred_goal;
  return normalized;
}

function closeRing(ring: Array<[number, number]>) {
  const points = ring.map(([x, y]) => [roundMapCoordinate(x), roundMapCoordinate(y)] as [number, number]);
  if (points.length === 0) return points;
  const first = points[0];
  const last = points[points.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) points.push([...first]);
  return points;
}

function withZoneRing(zone: GeoFeature, ring: Array<[number, number]>): GeoFeature {
  const normalizedRing = closeRing(ring);
  const metrics = polygonMetrics([normalizedRing]);
  const properties: Record<string, unknown> = {
    ...zone.properties,
    area_m2: roundArea(metrics.area),
    centroid: metrics.centroid,
  };
  if (validPoint(properties.preferred_goal) && !pointInPolygon(properties.preferred_goal, [normalizedRing])) {
    delete properties.preferred_goal;
  }
  return { ...zone, properties, geometry: { type: "Polygon", coordinates: [normalizedRing] } };
}

function withVirtualWall(
  zone: GeoFeature,
  endpoints: [[number, number], [number, number]],
  width: number,
) {
  const next = withZoneRing(zone, virtualWallRing(endpoints, width));
  next.properties.geometry_kind = "virtual_wall";
  next.properties.behavior = "restricted";
  next.properties.wall_endpoints = endpoints.map((point) => [
    roundMapCoordinate(point[0]),
    roundMapCoordinate(point[1]),
  ]);
  next.properties.wall_width_m = width;
  delete next.properties.preferred_goal;
  return next;
}

function translateZone(
  zone: GeoFeature,
  sourceGeometry: GeoFeature["geometry"],
  preferredGoal: [number, number] | null,
  wallEndpoints: [[number, number], [number, number]] | null,
  dx: number,
  dy: number,
) {
  const polygons = polygonGeometries(sourceGeometry);
  const translated = polygons.map((polygon) => polygon.map((ring) => ring.map(([x, y]) => [
    roundMapCoordinate(x + dx),
    roundMapCoordinate(y + dy),
  ] as [number, number])));
  const geometry: GeoFeature["geometry"] = sourceGeometry.type === "MultiPolygon"
    ? { type: "MultiPolygon", coordinates: translated }
    : { type: "Polygon", coordinates: translated[0] ?? [] };
  const next = { ...zone, geometry, properties: { ...zone.properties } };
  if (preferredGoal) {
    next.properties.preferred_goal = [
      roundMapCoordinate(preferredGoal[0] + dx),
      roundMapCoordinate(preferredGoal[1] + dy),
    ];
  }
  if (wallEndpoints) {
    next.properties.wall_endpoints = wallEndpoints.map(([x, y]) => [
      roundMapCoordinate(x + dx),
      roundMapCoordinate(y + dy),
    ]);
  }
  const polygon = polygonGeometries(geometry)[0];
  if (polygon) {
    const metrics = polygonMetrics(polygon);
    next.properties.area_m2 = roundArea(metrics.area);
    next.properties.centroid = metrics.centroid;
  }
  return next;
}

function zoneGeometryValidationError(
  geometry: GeoFeature["geometry"],
  boundary: GeoFeature,
  resolution: number,
  minimumArea = 0.1,
) {
  if (geometry.type !== "Polygon") return "구역은 하나의 Polygon이어야 합니다.";
  const polygon = polygonGeometries(geometry)[0];
  if (!polygon) return "구역 경계를 확인할 수 없습니다.";
  const outerError = zoneRingValidationError(polygon[0], boundary, resolution, minimumArea, false);
  if (outerError) return outerError;
  for (const ring of polygon.slice(1)) {
    const closed = closeRing(ring);
    if (closed.length < 4 || ringSelfIntersects(closed)) return "구역의 내부 경계를 확인하세요.";
    for (let index = 1; index < closed.length; index += 1) {
      if (!segmentInsideBoundary(closed[index - 1], closed[index], boundary.geometry, resolution)) {
        return "구역 경계 전체가 지도의 주행 가능 영역 안에 있어야 합니다.";
      }
    }
  }
  if (!zoneInteriorInsideBoundary(polygon, boundary, resolution)) {
    return "구역 내부에 벽이나 장애물이 포함될 수 없습니다.";
  }
  return "";
}

function zoneRingValidationError(
  ring: Array<[number, number]>,
  boundary: GeoFeature,
  resolution: number,
  minimumArea = 0.1,
  validateInterior = true,
) {
  const closed = closeRing(ring);
  if (closed.length < 4) return "구역은 세 점 이상이어야 합니다.";
  if (ringSelfIntersects(closed)) return "구역 경계가 서로 교차할 수 없습니다.";
  if (Math.abs(ringSignedMetrics(closed).area) < minimumArea) return `구역 면적은 ${minimumArea} m² 이상이어야 합니다.`;
  for (let index = 1; index < closed.length; index += 1) {
    if (!segmentInsideBoundary(closed[index - 1], closed[index], boundary.geometry, resolution)) {
      return "구역 경계 전체가 지도의 주행 가능 영역 안에 있어야 합니다.";
    }
  }
  if (validateInterior && !zoneInteriorInsideBoundary([closed], boundary, resolution)) {
    return "구역 내부에 벽이나 장애물이 포함될 수 없습니다.";
  }
  return "";
}

function zoneInteriorInsideBoundary(
  polygon: PolygonCoordinates,
  boundary: GeoFeature,
  resolution: number,
) {
  const outer = closeRing(polygon[0] ?? []);
  if (outer.length < 4) return false;
  const rings = [outer, ...polygon.slice(1).map(closeRing)];
  const bounds = rectangleBounds(outer);
  const step = Math.max(0.025, resolution / 2);
  for (let y = bounds.minY + step / 2; y < bounds.maxY; y += step) {
    for (let x = bounds.minX + step / 2; x < bounds.maxX; x += step) {
      const point: [number, number] = [x, y];
      if (pointInPolygon(point, rings) && !pointInGeometry(point, boundary.geometry)) return false;
    }
  }
  return true;
}

function segmentInsideBoundary(
  first: [number, number],
  second: [number, number],
  geometry: GeoFeature["geometry"],
  resolution: number,
) {
  const length = Math.hypot(second[0] - first[0], second[1] - first[1]);
  const steps = Math.max(1, Math.ceil(length / Math.max(0.025, resolution / 2)));
  for (let index = 0; index <= steps; index += 1) {
    const ratio = index / steps;
    const point: [number, number] = [
      first[0] + (second[0] - first[0]) * ratio,
      first[1] + (second[1] - first[1]) * ratio,
    ];
    if (!pointInGeometry(point, geometry) && !pointNearRoomWall(point, geometry, Math.max(0.03, resolution))) return false;
  }
  return true;
}

function orientation(first: [number, number], second: [number, number], third: [number, number]) {
  return (second[0] - first[0]) * (third[1] - first[1]) -
    (second[1] - first[1]) * (third[0] - first[0]);
}

function pointOnSegment(point: [number, number], first: [number, number], second: [number, number]) {
  const epsilon = 1e-9;
  return Math.abs(orientation(first, second, point)) <= epsilon &&
    point[0] >= Math.min(first[0], second[0]) - epsilon &&
    point[0] <= Math.max(first[0], second[0]) + epsilon &&
    point[1] >= Math.min(first[1], second[1]) - epsilon &&
    point[1] <= Math.max(first[1], second[1]) + epsilon;
}

function segmentsIntersect(
  first: [number, number],
  second: [number, number],
  third: [number, number],
  fourth: [number, number],
) {
  const one = orientation(first, second, third);
  const two = orientation(first, second, fourth);
  const three = orientation(third, fourth, first);
  const four = orientation(third, fourth, second);
  if (((one > 0 && two < 0) || (one < 0 && two > 0)) &&
      ((three > 0 && four < 0) || (three < 0 && four > 0))) return true;
  return pointOnSegment(third, first, second) || pointOnSegment(fourth, first, second) ||
    pointOnSegment(first, third, fourth) || pointOnSegment(second, third, fourth);
}

function ringSelfIntersects(ring: Array<[number, number]>) {
  const edgeCount = ring.length - 1;
  for (let first = 0; first < edgeCount; first += 1) {
    for (let second = first + 1; second < edgeCount; second += 1) {
      const adjacent = Math.abs(first - second) === 1 || (first === 0 && second === edgeCount - 1);
      if (!adjacent && segmentsIntersect(ring[first], ring[first + 1], ring[second], ring[second + 1])) return true;
    }
  }
  return false;
}

function defaultZoneColor(behavior: ZoneBehavior) {
  return behavior === "restricted" ? "#C0402F" : behavior === "avoid" ? "#C8901A" : "#2E7D51";
}

function zoneColor(zone: GeoFeature) {
  return typeof zone.properties.color === "string" && /^#[0-9a-f]{6}$/i.test(zone.properties.color)
    ? zone.properties.color
    : defaultZoneColor(zoneBehaviorOf(zone));
}

function updateRoomProperties(room: GeoFeature, updates: Record<string, unknown>): GeoFeature {
  const properties: Record<string, unknown> = {
    ...room.properties,
    ...updates,
    edited: true,
    semantic_edited: true,
  };
  if (Object.prototype.hasOwnProperty.call(updates, "name")) {
    // Keep the raw empty value while the user replaces a room name. Display
    // labels apply their own fallback, so it must never be written back into
    // the controlled input on every keystroke.
    const name = typeof updates.name === "string"
      ? updates.name.slice(0, 40)
      : "";
    properties.name = name;
    properties.base_name = name.trim() || "이름 없는 방";
    delete properties.merged_from_names;
    delete properties.split_path;
  }
  return { ...room, properties };
}

function validPoint(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length >= 2 &&
    typeof value[0] === "number" && Number.isFinite(value[0]) &&
    typeof value[1] === "number" && Number.isFinite(value[1]);
}

function roomColor(feature: GeoFeature, index: number) {
  if (typeof feature.properties.color === "string" && /^#[0-9a-f]{6}$/i.test(feature.properties.color)) {
    return feature.properties.color;
  }
  return ["#E7EBE3", "#E3E7EE", "#EFE7DE", "#DDE9E8", "#EDE6DC", "#F0EDE6"][index % 6];
}

function zoneBehaviorOf(feature: GeoFeature): ZoneBehavior {
  return feature.properties.behavior === "avoid" || feature.properties.behavior === "allow"
    ? feature.properties.behavior
    : "restricted";
}

function zoneBehaviorLabel(behavior: ZoneBehavior) {
  return behavior === "restricted" ? "진입 금지" : behavior === "avoid" ? "우회 권장" : "통행 허용";
}

function localizationCopy(state: string | undefined) {
  if (state === "ok") return "위치 확인됨";
  if (state === "verifying") return "위치 재확인 중";
  if (state === "revalidation_required") return "부팅 후 위치 확인 필요";
  return "위치 확인 필요";
}

function localizationShortCopy(state: string | undefined) {
  if (state === "ok") return "확인됨";
  if (state === "verifying") return "재확인 중";
  if (state === "revalidation_required") return "부팅 후 확인 필요";
  return "확인 필요";
}

function numberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function navigationProgressPercent(value: unknown) {
  if (!isRecord(value)) return 0;
  const reported = numberValue(value.progress_ratio);
  const initialPathLength = numberValue(value.initial_path_length_m);
  const pathLength = initialPathLength > 0
    ? initialPathLength
    : numberValue(value.path_length_m);
  const remaining = numberValue(value.distance_remaining_m);
  const derived = pathLength > 0
    ? 1 - Math.max(0, remaining) / pathLength
    : 0;
  const ratio = Math.max(reported, derived);
  const upperBound = value.state === "succeeded" ? 1 : 0.99;
  return Math.round(Math.max(0, Math.min(upperBound, ratio)) * 100);
}

function formatMeters(value: unknown) {
  const meters = numberValue(value);
  return meters > 0 ? `${meters.toFixed(1)}m` : "—";
}

function estimateSeconds(preview: Record<string, unknown> | null) {
  const path = isRecord(preview?.path) ? preview.path : null;
  const length = numberValue(path?.length_m);
  return length > 0 ? Math.max(1, Math.round(length / 0.22)) : 0;
}

function formatEta(value: unknown) {
  const seconds = numberValue(value);
  return seconds > 0 ? `약 ${Math.ceil(seconds)}초` : "—";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
