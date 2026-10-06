"use client";

import { useEffect, useState } from "react";
import type { RobotSnapshot } from "./robot-map-panel";

export type MapPoint = { x: number; y: number };
export type MapPose = MapPoint & { yaw: number };
export type ZoneBehavior = "restricted" | "avoid" | "allow";
/** `id` is the robot's zone_id: saving it back keeps what the map editor added (virtual wall, goal). */
export type ZoneDraft = { id?: string; behavior: ZoneBehavior; name: string; points: Array<[number, number]> };
/** The robot's Zone file for the saved map in use, uploaded with that map. */
export type ZoneDocument = { map: string | null; editable: boolean; zones: ZoneDraft[]; message: string };

type Keyed<T> = T & { deviceId: string; mapId: string };
type ZoneEdit = {
  deviceId: string;
  map: string;
  zones: ZoneDraft[];
  drawing: Array<[number, number]>;
  selected: number | null;
  behavior: ZoneBehavior;
};

const BEHAVIORS: ZoneBehavior[] = ["restricted", "avoid", "allow"];
const ZONE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;

/** Map clicks, the operator's pose and Zone edits for the managed robot view. */
export function useManagedRobotWorkspace(deviceId: string, snapshot: RobotSnapshot | null, enabled: boolean) {
  const [goal, setGoal] = useState<Keyed<MapPoint> | null>(null);
  const [pose, setPose] = useState<Keyed<MapPose> | null>(null);
  const [loaded, setLoaded] = useState<{ deviceId: string; revision: string; document: ZoneDocument } | null>(null);
  const [edit, setEdit] = useState<ZoneEdit | null>(null);
  const mapId = snapshot?.map?.mapId ?? "";
  const revision = snapshot?.map?.finalized ? snapshot.map.revision : "";

  useEffect(() => {
    if (!enabled || !deviceId || !revision) return;
    const controller = new AbortController();
    void fetch(`/api/devices/${encodeURIComponent(deviceId)}/robot/semantic`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() as Promise<Record<string, unknown>> : null)
      .then((payload) => {
        const document = parseZoneDocument(payload?.zones);
        // The saved-map row can be replaced between the map and this request.
        if (!controller.signal.aborted && document && payload?.revision === revision) {
          setLoaded({ deviceId, revision, document });
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [deviceId, enabled, revision]);

  const document = loaded && loaded.deviceId === deviceId && loaded.revision === revision ? loaded.document : null;
  const zoneEdit = edit && document?.editable && edit.deviceId === deviceId && edit.map === document.map ? edit : null;
  const zones = zoneEdit?.zones ?? document?.zones ?? [];
  const dirty = Boolean(zoneEdit && document && JSON.stringify(zoneEdit.zones) !== JSON.stringify(document.zones));

  function current(): ZoneEdit | null {
    if (zoneEdit) return zoneEdit;
    if (!document?.editable || !document.map) return null;
    return {
      deviceId, map: document.map, zones: document.zones.map(copyZone),
      drawing: [], selected: null, behavior: "restricted",
    };
  }

  function change(update: (value: ZoneEdit) => ZoneEdit) {
    const value = current();
    if (value) setEdit(update(value));
  }

  function pick(point: MapPoint) {
    change((value) => {
      const vertex: [number, number] = [round(point.x), round(point.y)];
      if (value.drawing.length > 0) return { ...value, drawing: [...value.drawing, vertex] };
      const index = value.zones.findIndex((zone) => contains(zone.points, point));
      return index >= 0 ? { ...value, selected: index } : { ...value, drawing: [vertex], selected: null };
    });
  }

  return {
    goal: goal && goal.deviceId === deviceId && goal.mapId === mapId ? { x: goal.x, y: goal.y } : null,
    pose: pose && pose.deviceId === deviceId && pose.mapId === mapId ? { x: pose.x, y: pose.y, yaw: pose.yaw } : null,
    selectGoal: (point: MapPoint) => setGoal({ ...point, deviceId, mapId }),
    selectPose: (value: MapPose) => setPose({ ...value, deviceId, mapId }),
    zones: {
      document,
      zones,
      dirty,
      drawing: zoneEdit?.drawing ?? [],
      selected: zoneEdit?.selected ?? null,
      behavior: zoneEdit?.behavior ?? "restricted",
      pick,
      setBehavior: (behavior: ZoneBehavior) => change((value) => ({
        ...value,
        behavior,
        zones: value.selected === null ? value.zones : value.zones.map((zone, index) => (
          index === value.selected ? { ...zone, behavior } : zone
        )),
      })),
      select: (index: number) => change((value) => ({ ...value, selected: index, drawing: [] })),
      finishDrawing: () => change((value) => value.drawing.length < 3 ? value : {
        ...value,
        zones: [...value.zones, { behavior: value.behavior, name: "", points: value.drawing }],
        drawing: [],
        selected: value.zones.length,
      }),
      undoPoint: () => change((value) => ({ ...value, drawing: value.drawing.slice(0, -1) })),
      cancelDrawing: () => change((value) => ({ ...value, drawing: [] })),
      removeSelected: () => change((value) => value.selected === null ? value : {
        ...value,
        zones: value.zones.filter((_zone, index) => index !== value.selected),
        selected: null,
      }),
      discard: () => setEdit(null),
      payload: () => document?.map
        ? {
          map: document.map,
          zones: zones.map(({ id, behavior, name, points }) => ({ ...(id ? { id } : {}), behavior, name, points })),
        }
        : null,
    },
  };
}

export type ManagedWorkspace = ReturnType<typeof useManagedRobotWorkspace>;

export function parseZoneDocument(value: unknown): ZoneDocument | null {
  // The robot uploads the map editor's Zone FeatureCollection (SWM25-237); older robots the zone list.
  if (isRecord(value) && value.type === "FeatureCollection" && Array.isArray(value.features)) {
    return parseZoneDocument({
      map: value.map,
      editable: value.editable,
      message: value.message,
      zones: value.features.flatMap((feature) => {
        if (!isRecord(feature) || !isRecord(feature.properties) || !isRecord(feature.geometry)) return [];
        const ring = Array.isArray(feature.geometry.coordinates) ? feature.geometry.coordinates[0] : null;
        if (feature.geometry.type !== "Polygon" || !Array.isArray(ring)) return [];
        return [{
          id: feature.properties.zone_id,
          behavior: feature.properties.behavior,
          name: feature.properties.name,
          points: ring.slice(0, -1),
        }];
      }),
    });
  }
  if (!isRecord(value) || !Array.isArray(value.zones)) return null;
  const zones = value.zones.flatMap((zone): ZoneDraft[] => {
    if (!isRecord(zone) || !BEHAVIORS.includes(zone.behavior as ZoneBehavior) || !Array.isArray(zone.points)) {
      return [];
    }
    const points = zone.points.filter((point): point is [number, number] => (
      Array.isArray(point) && point.length >= 2 && Number.isFinite(point[0]) && Number.isFinite(point[1])
    )).map((point) => [Number(point[0]), Number(point[1])] as [number, number]);
    return points.length >= 3
      ? [{
        ...(typeof zone.id === "string" && ZONE_ID.test(zone.id) ? { id: zone.id } : {}),
        behavior: zone.behavior as ZoneBehavior, name: typeof zone.name === "string" ? zone.name : "", points,
      }]
      : [];
  });
  return {
    map: typeof value.map === "string" ? value.map : null,
    editable: value.editable === true,
    zones,
    message: typeof value.message === "string" ? value.message : "",
  };
}

function copyZone(zone: ZoneDraft): ZoneDraft {
  return { ...zone, points: zone.points.map((point) => [point[0], point[1]]) };
}

function round(value: number) {
  return Math.round(value * 1000) / 1000;
}

/** Ray casting on the map-frame polygon. */
function contains(points: Array<[number, number]>, point: MapPoint) {
  let inside = false;
  for (let index = 0, previous = points.length - 1; index < points.length; previous = index++) {
    const [x1, y1] = points[index];
    const [x2, y2] = points[previous];
    if ((y1 > point.y) !== (y2 > point.y) && point.x < (x2 - x1) * (point.y - y1) / (y2 - y1) + x1) {
      inside = !inside;
    }
  }
  return inside;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
