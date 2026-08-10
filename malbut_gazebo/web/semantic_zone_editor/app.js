"use strict";

const $ = (selector) => document.querySelector(selector);
const svg = $("#mapCanvas");
const state = {
  userMap: null,
  sourceRooms: [],
  rooms: [],
  zones: [],
  selectedRoomId: null,
  selectedId: null,
  splitLines: [],
  pendingSplitPoint: null,
  splitValidation: "idle",
  splitValidationMessage: "",
  splitValidationTimer: null,
  splitValidationRevision: 0,
  zoneApplyEnabled: false,
  mode: "idle",
  roomActionPending: false,
  dragging: null,
  viewBox: null,
};

const ROOM_CATEGORY_LABELS = {
  unassigned: "미지정", living_room: "거실", bedroom: "침실",
  kitchen: "주방", dining_room: "식당", bathroom: "욕실",
  entrance: "현관", hallway: "복도", workspace: "작업 공간",
  storage: "수납 공간", utility: "다용도실", custom: "기타",
};
const BEHAVIOR_LABELS = {
  allow: "통행 허용", avoid: "우회 권장", restricted: "진입 금지",
};

function roomStorageKey(mapId) {
  return `malbut-rooms:v2:${mapId}`;
}

function splitErrorMessage(message = "") {
  const translations = {
    "at least one split divider is required": "분할선을 하나 이상 만드세요.",
    "each split divider must contain at least two finite points":
      "각 분할선의 양 끝점을 지정하세요.",
    "split divider points must be near a Room wall":
      "분할선의 점을 방 벽 근처에 놓으세요.",
    "split divider endpoints must be near a Room wall":
      "분할선의 두 끝점을 방 벽 근처에 놓으세요.",
    "split divider control points must stay in the Room":
      "분할선의 꺽임점은 방 안에 놓으세요.",
    "split divider segments are too short": "분할선이 너무 짧습니다.",
    "the divider must cut the selected Room into exactly two meaningful areas":
      "분할선을 이어서 선택한 방을 정확히 두 공간으로 나누세요.",
  };
  return translations[message] || message || "분할선을 확인할 수 없습니다.";
}

function clearSplitDraft() {
  if (state.splitValidationTimer) clearTimeout(state.splitValidationTimer);
  state.splitLines = [];
  state.pendingSplitPoint = null;
  state.splitValidation = "idle";
  state.splitValidationMessage = "";
  state.splitValidationTimer = null;
  state.splitValidationRevision += 1;
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function ringPath(ring) {
  return ring.map((point, index) => `${index ? "L" : "M"}${point[0]},${-point[1]}`).join(" ") + " Z";
}

function geometryPath(geometry) {
  if (geometry.type === "Polygon") return geometry.coordinates.map(ringPath).join(" ");
  if (geometry.type === "MultiPolygon") return geometry.coordinates.flatMap((polygon) => polygon.map(ringPath)).join(" ");
  return "";
}

function allGeometryPoints(geometry) {
  if (geometry.type === "Polygon") return geometry.coordinates.flat();
  if (geometry.type === "MultiPolygon") return geometry.coordinates.flat(2);
  if (geometry.type === "MultiLineString") return geometry.coordinates.flat();
  return [];
}

function geometryRings(geometry) {
  if (geometry.type === "Polygon") return geometry.coordinates;
  if (geometry.type === "MultiPolygon") return geometry.coordinates.flat();
  return [];
}

function pointSegmentDistance(point, first, second) {
  const dx = second[0] - first[0], dy = second[1] - first[1];
  const lengthSquared = dx * dx + dy * dy;
  if (!lengthSquared) return Math.hypot(point[0] - first[0], point[1] - first[1]);
  const amount = Math.max(0, Math.min(1,
    ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / lengthSquared));
  return Math.hypot(
    point[0] - (first[0] + amount * dx),
    point[1] - (first[1] + amount * dy),
  );
}

function pointNearRoomWall(point, geometry, tolerance = .25) {
  return geometryRings(geometry).some((ring) => ring.slice(1).some(
    (second, index) => pointSegmentDistance(point, ring[index], second) <= tolerance
  ));
}

function orthogonalCorner(point, previous, next, geometry = null) {
  const candidates = [
    [previous[0], next[1]],
    [next[0], previous[1]],
  ];
  const usable = geometry
    ? candidates.filter((candidate) =>
      pointInGeometry(candidate, geometry) || pointNearRoomWall(candidate, geometry)
    )
    : candidates;
  const choices = usable.length ? usable : candidates;
  return choices.reduce((closest, candidate) =>
    Math.hypot(point[0] - candidate[0], point[1] - candidate[1]) <
      Math.hypot(point[0] - closest[0], point[1] - closest[1])
      ? candidate : closest
  );
}

function walkableFeature() {
  return state.userMap?.features.find((feature) => feature.properties?.role === "walkable_area");
}

function pointInRing(point, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i], b = ring[j];
    const crosses = ((a[1] > point[1]) !== (b[1] > point[1])) &&
      (point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]);
    if (crosses) inside = !inside;
  }
  return inside;
}

function pointInPolygon(point, polygon) {
  return pointInRing(point, polygon[0]) && !polygon.slice(1).some((hole) => pointInRing(point, hole));
}

function pointInGeometry(point, geometry) {
  if (geometry.type === "Polygon") return pointInPolygon(point, geometry.coordinates);
  if (geometry.type === "MultiPolygon") {
    return geometry.coordinates.some((polygon) => pointInPolygon(point, polygon));
  }
  return false;
}

function roomById(id) {
  return state.rooms.find((room) => room.id === id);
}

function segmentInsideGeometry(first, second, geometry) {
  const distance = Math.hypot(second[0] - first[0], second[1] - first[1]);
  const resolution = state.userMap?.source?.resolution || .05;
  const steps = Math.max(1, Math.ceil(distance / Math.max(.025, resolution / 2)));
  for (let index = 0; index <= steps; index += 1) {
    const ratio = index / steps;
    const point = [
      first[0] + (second[0] - first[0]) * ratio,
      first[1] + (second[1] - first[1]) * ratio,
    ];
    if (!pointInGeometry(point, geometry)) return false;
  }
  return true;
}

function orientation(first, second, third) {
  return (second[0] - first[0]) * (third[1] - first[1]) -
    (second[1] - first[1]) * (third[0] - first[0]);
}

function pointOnSegment(point, first, second) {
  const epsilon = 1e-9;
  return Math.abs(orientation(first, second, point)) <= epsilon &&
    point[0] >= Math.min(first[0], second[0]) - epsilon &&
    point[0] <= Math.max(first[0], second[0]) + epsilon &&
    point[1] >= Math.min(first[1], second[1]) - epsilon &&
    point[1] <= Math.max(first[1], second[1]) + epsilon;
}

function segmentsIntersect(first, second, third, fourth) {
  const one = orientation(first, second, third);
  const two = orientation(first, second, fourth);
  const three = orientation(third, fourth, first);
  const four = orientation(third, fourth, second);
  if (((one > 0 && two < 0) || (one < 0 && two > 0)) &&
      ((three > 0 && four < 0) || (three < 0 && four > 0))) return true;
  return pointOnSegment(third, first, second) ||
    pointOnSegment(fourth, first, second) ||
    pointOnSegment(first, third, fourth) ||
    pointOnSegment(second, third, fourth);
}

function ringSelfIntersects(ring) {
  const edgeCount = ring.length - 1;
  for (let first = 0; first < edgeCount; first += 1) {
    for (let second = first + 1; second < edgeCount; second += 1) {
      const adjacent = Math.abs(first - second) === 1 ||
        (first === 0 && second === edgeCount - 1);
      if (adjacent) continue;
      if (segmentsIntersect(
        ring[first], ring[first + 1], ring[second], ring[second + 1]
      )) return true;
    }
  }
  return false;
}

function ringMetrics(ring) {
  let twiceArea = 0;
  let centerX = 0;
  let centerY = 0;
  for (let index = 0; index < ring.length - 1; index += 1) {
    const first = ring[index], second = ring[index + 1];
    const cross = first[0] * second[1] - second[0] * first[1];
    twiceArea += cross;
    centerX += (first[0] + second[0]) * cross;
    centerY += (first[1] + second[1]) * cross;
  }
  if (Math.abs(twiceArea) < 1e-6) return {area: 0, centroid: null};
  return {
    area: Math.abs(twiceArea) / 2,
    centroid: [centerX / (3 * twiceArea), centerY / (3 * twiceArea)],
  };
}

function validateZoneRing(ring, boundary) {
  if (!boundary) return "지도의 주행 가능 영역을 찾을 수 없습니다.";
  if (ring.length < 4) return "Zone은 세 점 이상으로 그려야 합니다.";
  if (ringSelfIntersects(ring)) return "Zone 경계가 서로 교차할 수 없습니다.";
  const metrics = ringMetrics(ring);
  if (metrics.area < .1) return "Zone 면적은 0.1 m² 이상이어야 합니다.";
  for (let index = 0; index < ring.length - 1; index += 1) {
    if (!segmentInsideGeometry(
      ring[index], ring[index + 1], boundary.geometry
    )) return "Zone 경계 전체가 지도의 주행 가능 영역 안에 있어야 합니다.";
  }
  return null;
}

function rectangleRing(firstX, firstY, secondX, secondY) {
  const minX = Math.min(firstX, secondX), maxX = Math.max(firstX, secondX);
  const minY = Math.min(firstY, secondY), maxY = Math.max(firstY, secondY);
  return [
    [minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY], [minX, minY],
  ];
}

function rectangleBounds(ring) {
  const points = ring.slice(0, -1);
  return {
    minX: Math.min(...points.map((point) => point[0])),
    maxX: Math.max(...points.map((point) => point[0])),
    minY: Math.min(...points.map((point) => point[1])),
    maxY: Math.max(...points.map((point) => point[1])),
  };
}

function isRectangleRing(ring) {
  if (!Array.isArray(ring) || ring.length !== 5 ||
      ring[0][0] !== ring[4][0] || ring[0][1] !== ring[4][1]) return false;
  const bounds = rectangleBounds(ring);
  const corners = new Set(ring.slice(0, -1).map((point) => `${point[0]},${point[1]}`));
  const expected = new Set([
    `${bounds.minX},${bounds.minY}`, `${bounds.maxX},${bounds.minY}`,
    `${bounds.maxX},${bounds.maxY}`, `${bounds.minX},${bounds.maxY}`,
  ]);
  return corners.size === 4 && [...corners].every((corner) => expected.has(corner));
}

function defaultZoneRing() {
  const floor = walkableFeature();
  if (!floor) return null;
  const points = allGeometryPoints(floor.geometry);
  const xs = points.map((point) => point[0]), ys = points.map((point) => point[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const centers = [[(minX + maxX) / 2, (minY + maxY) / 2]];
  for (let row = 1; row < 10; row += 1) {
    for (let column = 1; column < 10; column += 1) {
      centers.push([
        minX + (maxX - minX) * column / 10,
        minY + (maxY - minY) * row / 10,
      ]);
    }
  }
  const span = Math.min(maxX - minX, maxY - minY);
  const sizes = [...new Set([Math.min(1.2, span * .15), .8, .5, .4]
    .map((size) => Number(size.toFixed(3))))].filter((size) => size >= .4);
  for (const size of sizes) {
    const valid = centers.map((center) => rectangleRing(
      center[0] - size / 2, center[1] - size / 2,
      center[0] + size / 2, center[1] + size / 2,
    )).filter((ring) => !validateZoneRing(ring, floor));
    if (valid.length) return valid[state.zones.length % valid.length];
  }
  return null;
}

function worldPoint(event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const local = point.matrixTransform(svg.getScreenCTM().inverse());
  return [Number(local.x.toFixed(3)), Number((-local.y).toFixed(3))];
}

function setViewBox(box) {
  state.viewBox = box;
  svg.setAttribute("viewBox", box.join(" "));
}

function fitMap() {
  if (!state.userMap) return;
  const points = state.userMap.features.flatMap((feature) => allGeometryPoints(feature.geometry));
  const xs = points.map((point) => point[0]);
  const ys = points.map((point) => -point[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const margin = Math.max(maxX - minX, maxY - minY) * 0.1 + 0.3;
  setViewBox([minX - margin, minY - margin, maxX - minX + margin * 2, maxY - minY + margin * 2]);
}

function renderFloor() {
  const layer = $("#floorLayer");
  const wallLayer = $("#wallLayer");
  layer.replaceChildren();
  wallLayer.replaceChildren();
  for (const feature of state.userMap.features) {
    if (feature.properties?.role === "walkable_area") {
      layer.append(svgElement("path", {d: geometryPath(feature.geometry), class: "floor", "fill-rule": "evenodd"}));
    } else if (feature.properties?.role === "wall_outline") {
      for (const line of feature.geometry.coordinates) {
        const d = line.map((point, index) => `${index ? "L" : "M"}${point[0]},${-point[1]}`).join(" ");
        wallLayer.append(svgElement("path", {d, class: "wall-line"}));
      }
    }
  }
}

function selectedRoom() {
  return state.rooms.find((room) => room.id === state.selectedRoomId);
}

function roomDisplayName(room) {
  const sourceNames = room.properties.merged_from_names;
  if (Array.isArray(sourceNames) && sourceNames.length === 2) {
    return `${room.properties.name} (${sourceNames.join(" + ")})`;
  }
  return room.properties.name;
}

function chooseRoom(id) {
  if (state.mode === "merging") {
    mergeSelectedRoomWith(id);
    return;
  }
  selectRoom(id);
}

function renderRooms() {
  const layer = $("#roomLayer");
  layer.replaceChildren();
  for (const room of state.rooms) {
    const selected = room.id === state.selectedRoomId;
    const mergeCandidate = state.mode === "merging" && !selected;
    const path = svgElement("path", {
      d: geometryPath(room.geometry),
      class: `room-shape${selected ? " selected" : ""}${mergeCandidate ? " merge-candidate" : ""}`,
      fill: room.properties.color || "#dce8ff",
      "fill-rule": "evenodd",
      "data-room-id": room.id,
    });
    path.addEventListener("click", (event) => {
      if (state.mode === "splitting") return;
      event.stopPropagation();
      chooseRoom(room.id);
    });
    layer.append(path);
    const centroid = room.properties.centroid;
    if (centroid) {
      const label = svgElement("text", {
        x: centroid[0], y: -centroid[1], class: "room-label",
      });
      label.textContent = room.properties.name || room.properties.room_id;
      layer.append(label);
    }
  }
  renderRoomList();
}

function renderRoomList() {
  const list = $("#roomList");
  list.replaceChildren();
  $("#roomCount").textContent = state.rooms.length;
  if (!state.rooms.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "편집할 방이 없습니다.";
    list.append(empty);
    return;
  }
  for (const room of state.rooms) {
    const item = document.createElement("button");
    item.className = `room-item${room.id === state.selectedRoomId ? " selected" : ""}`;
    item.innerHTML = `<i class="room-dot"></i><span><strong></strong><small></small></span><em>›</em>`;
    item.querySelector("i").style.background = room.properties.color;
    item.querySelector("strong").textContent = roomDisplayName(room);
    const category = ROOM_CATEGORY_LABELS[room.properties.category] || "기타";
    item.querySelector("small").textContent = `${room.properties.area_m2.toFixed(2)} m² · ${category}`;
    item.addEventListener("click", () => chooseRoom(room.id));
    list.append(item);
  }
}

function selectRoom(id) {
  state.selectedRoomId = id;
  state.selectedId = null;
  state.mode = "idle";
  clearSplitDraft();
  $("#zoneForm").classList.add("hidden");
  const room = selectedRoom();
  $("#roomSummary").classList.toggle("hidden", !room);
  $("#roomForm").classList.toggle("hidden", !room);
  if (room) {
    const category = room.properties.category || "unassigned";
    $("#roomSummaryColor").style.background = room.properties.color;
    $("#roomSummaryName").textContent = roomDisplayName(room);
    $("#roomSummaryDetail").textContent = `${room.properties.area_m2.toFixed(2)} m² · ${ROOM_CATEGORY_LABELS[category] || "기타"}`;
    $("#roomSummaryBadge").textContent = room.properties.edited ? "편집됨" : "초기 공간";
    $("#roomName").value = room.properties.name;
    $("#roomCategory").value = ROOM_CATEGORY_LABELS[category] ? category : "custom";
  }
  setModeBanner();
  renderRooms();
  renderZones();
}

function zonePath(zone) { return ringPath(zone.geometry.coordinates[0]); }
function selectedZone() { return state.zones.find((zone) => zone.id === state.selectedId); }

function updateZoneGeometryMetadata(zone) {
  const ring = zone.geometry.coordinates[0];
  const metrics = ringMetrics(ring);
  zone.properties.area_m2 = Number(metrics.area.toFixed(2));
  zone.properties.centroid = metrics.centroid?.map((value) => Number(value.toFixed(3))) || null;
  if (zone.properties.preferred_goal && !pointInPolygon(
    zone.properties.preferred_goal, zone.geometry.coordinates
  )) delete zone.properties.preferred_goal;
}

function normalizeZones(zones = state.zones) {
  for (const zone of zones) {
    if (!zone.properties) continue;
    delete zone.properties.room_id;
    delete zone.properties.room_name;
    delete zone.properties.needs_review;
    if (zone.geometry?.type === "Polygon" && zone.geometry.coordinates?.[0]) {
      updateZoneGeometryMetadata(zone);
    }
  }
}

function renderZones() {
  const layer = $("#zoneLayer");
  layer.replaceChildren();
  for (const zone of state.zones) {
    const color = zone.properties.color;
    const path = svgElement("path", {
      d: zonePath(zone),
      class: `zone-shape${zone.id === state.selectedId ? " selected" : ""}`,
      fill: `${color}42`, stroke: color, "data-zone-id": zone.id,
    });
    path.addEventListener("click", (event) => {
      if (["splitting", "merging"].includes(state.mode)) return;
      event.stopPropagation(); selectZone(zone.id);
    });
    path.addEventListener("pointerdown", (event) => {
      if (state.mode !== "idle") return;
      event.stopPropagation();
      if (state.selectedId !== zone.id) selectZone(zone.id);
      state.dragging = {
        type: "zone-move", zoneId: zone.id,
        origin: worldPoint(event),
        ring: zone.geometry.coordinates[0].map((point) => [...point]),
      };
      svg.setPointerCapture(event.pointerId);
    });
    layer.append(path);
  }
  renderHandles();
  renderZoneList();
}

function renderHandles() {
  const layer = $("#handleLayer");
  layer.replaceChildren();
  const zone = selectedZone();
  if (!zone || state.mode !== "idle") return;
  const ring = zone.geometry.coordinates[0].slice(0, -1);
  const rectangular = isRectangleRing(zone.geometry.coordinates[0]);
  if (rectangular) ring.forEach((point, index) => {
    const handle = svgElement("circle", {
      cx: point[0], cy: -point[1], r: .11,
      class: "zone-corner",
    });
    handle.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      state.dragging = {
        type: "zone-corner", zoneId: zone.id, opposite: ring[(index + 2) % 4],
      };
      svg.setPointerCapture(event.pointerId);
    });
    layer.append(handle);
  });
  if (rectangular) {
    const bounds = rectangleBounds(zone.geometry.coordinates[0]);
    const edges = [
      {side: "minY", point: [(bounds.minX + bounds.maxX) / 2, bounds.minY]},
      {side: "maxX", point: [bounds.maxX, (bounds.minY + bounds.maxY) / 2]},
      {side: "maxY", point: [(bounds.minX + bounds.maxX) / 2, bounds.maxY]},
      {side: "minX", point: [bounds.minX, (bounds.minY + bounds.maxY) / 2]},
    ];
    for (const edge of edges) {
      const handle = svgElement("rect", {
        x: edge.point[0] - .09, y: -edge.point[1] - .09,
        width: .18, height: .18, class: "zone-edge",
      });
      handle.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
        state.dragging = {
          type: "zone-edge", zoneId: zone.id, side: edge.side, bounds,
        };
        svg.setPointerCapture(event.pointerId);
      });
      layer.append(handle);
    }
  }
  const goal = zone.properties.preferred_goal;
  if (goal) {
    layer.append(svgElement("circle", {cx: goal[0], cy: -goal[1], r: .13, class: "goal"}));
  }
}

function renderDraft() {
  const layer = $("#draftLayer");
  layer.replaceChildren();
  state.splitLines.forEach((line, lineIndex) => {
    if (line.length >= 2) {
      const points = line.map((point) => `${point[0]},${-point[1]}`).join(" ");
      const divider = svgElement("polyline", {
        points, class: `split-line ${state.splitValidation}`,
      });
      divider.addEventListener("click", (event) => event.stopPropagation());
      layer.append(divider);
    }
    line.forEach((point, index) => {
      const handle = svgElement("circle", {
        cx: point[0], cy: -point[1], r: .12, class: "split-point",
      });
      handle.addEventListener("click", (event) => event.stopPropagation());
      handle.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
        state.dragging = {type: "split", lineIndex, index};
        svg.setPointerCapture(event.pointerId);
      });
      layer.append(handle);
    });
    if (line.length === 2) {
      const center = [
        (line[0][0] + line[1][0]) / 2,
        (line[0][1] + line[1][1]) / 2,
      ];
      const bendHandle = svgElement("circle", {
        cx: center[0], cy: -center[1], r: .1,
        class: "split-bend-point",
      });
      bendHandle.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
        line.splice(1, 0, center);
        state.dragging = {type: "split", lineIndex, index: 1};
        svg.setPointerCapture(event.pointerId);
        scheduleSplitValidation();
      });
      layer.append(bendHandle);
    }
  });
  if (state.pendingSplitPoint) {
    layer.append(svgElement("circle", {
      cx: state.pendingSplitPoint[0], cy: -state.pendingSplitPoint[1],
      r: .12, class: "split-point pending",
    }));
  }
}

function renderZoneList() {
  const list = $("#zoneList");
  list.replaceChildren();
  if (!state.zones.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "아직 지정한 공간이 없습니다.";
    list.append(empty);
    return;
  }
  for (const zone of state.zones) {
    const item = document.createElement("button");
    item.className = `zone-item${zone.id === state.selectedId ? " selected" : ""}`;
    item.innerHTML = `<i class="zone-dot"></i><span><strong></strong><small></small></span><em>›</em>`;
    item.querySelector("i").style.background = zone.properties.color;
    item.querySelector("strong").textContent = zone.properties.name;
    item.querySelector("small").textContent =
      BEHAVIOR_LABELS[zone.properties.behavior] ?? "통행 허용";
    item.addEventListener("click", () => selectZone(zone.id));
    list.append(item);
  }
}

function selectZone(id) {
  state.selectedRoomId = null;
  state.selectedId = id;
  state.mode = "idle";
  clearSplitDraft();
  const zone = selectedZone();
  $("#zoneForm").classList.toggle("hidden", !zone);
  if (zone) {
    $("#zoneName").value = zone.properties.name;
    $("#zoneBehavior").value = zone.properties.behavior;
    $("#zoneColor").value = zone.properties.color;
    $("#zoneArea").textContent = `${(zone.properties.area_m2 || 0).toFixed(2)} m²`;
  }
  setModeBanner();
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  renderRooms();
  renderZones();
}

function setModeBanner() {
  const banner = $("#modeBanner");
  const splitStatus = {
    idle: "",
    checking: " · 확인 중",
    valid: " · 분할 가능",
    invalid: ` · 분할 불가${state.splitValidationMessage
      ? `: ${state.splitValidationMessage}` : ""}`,
  }[state.splitValidation];
  const messages = {
    goal: "선택한 영역 안에서 로봇의 대표 도착 위치를 누르세요.",
    splitting: `벽 두 곳을 누르면 직선이 생깁니다. 선 중앙의 작은 점을 끌면 ㄱ자 직각선으로 바뀝니다. (${state.splitLines.length}개 선${state.pendingSplitPoint ? " · 끝점 선택 중" : splitStatus})`,
    merging: "현재 방과 합칠 다른 방을 지도나 방 목록에서 선택하세요.",
  };
  banner.textContent = messages[state.mode] ?? "";
  banner.classList.toggle("hidden", !messages[state.mode]);
  $("#newZone").textContent = "＋";
  $("#newZone").title = "새 사각형 Zone";
  $("#splitRoom").textContent = state.mode === "splitting" ? "분할 적용" : "방 나누기";
  $("#mergeRoom").textContent = state.mode === "merging" ? "합치기 취소" : "방 합치기";
  updateRoomActionButtons();
}

function updateRoomActionButtons() {
  const hasRoom = Boolean(selectedRoom());
  $("#resetRooms").disabled = !state.userMap || state.roomActionPending;
  if (state.roomActionPending) {
    $("#splitRoom").disabled = true;
    $("#mergeRoom").disabled = true;
  } else if (state.mode === "splitting") {
    $("#splitRoom").disabled = state.splitValidation !== "valid";
    $("#mergeRoom").disabled = true;
  } else if (state.mode === "merging") {
    $("#splitRoom").disabled = true;
    $("#mergeRoom").disabled = false;
  } else {
    $("#splitRoom").disabled = !hasRoom;
    $("#mergeRoom").disabled = !hasRoom || state.rooms.length < 2;
  }
}

function persist() {
  if (!state.userMap) return;
  localStorage.setItem(`malbut-zones:${state.userMap.map_id}`, JSON.stringify(state.zones));
  $("#saveState").textContent = "이 브라우저에 자동 저장됨";
}

function persistRooms() {
  if (!state.userMap) return;
  state.userMap.features = [
    ...state.userMap.features.filter((feature) => feature.properties?.role !== "room"),
    ...state.rooms,
  ];
  if (state.userMap.room_segmentation) {
    state.userMap.room_segmentation.room_count = state.rooms.length;
    state.userMap.room_segmentation.edited = true;
  }
  localStorage.setItem(roomStorageKey(state.userMap.map_id), JSON.stringify(state.rooms));
  $("#saveState").textContent = "방 편집 내용이 이 브라우저에 저장됨";
}

function updateSelectedProperty(name, value) {
  const zone = selectedZone();
  if (!zone) return;
  zone.properties[name] = value;
  persist();
  renderZones();
}

function updateSelectedRoomProperty(name, value) {
  const room = selectedRoom();
  if (!room) return;
  room.properties[name] = value;
  if (name === "name") {
    room.properties.base_name = value;
    delete room.properties.merged_from_names;
    delete room.properties.split_path;
  }
  room.properties.edited = true;
  room.properties.semantic_edited = true;
  persistRooms();
  const category = room.properties.category || "unassigned";
  $("#roomSummaryName").textContent = roomDisplayName(room);
  $("#roomSummaryDetail").textContent = `${room.properties.area_m2.toFixed(2)} m² · ${ROOM_CATEGORY_LABELS[category] || "기타"}`;
  $("#roomSummaryBadge").textContent = "편집됨";
  renderRooms();
}

function createDefaultZone() {
  const ring = defaultZoneRing();
  if (!ring) {
    alert("사각형 Zone을 배치할 수 있는 주행 가능 공간이 없습니다.");
    return;
  }
  const id = crypto.randomUUID ? crypto.randomUUID() : `zone-${Date.now()}`;
  const metrics = ringMetrics(ring);
  state.zones.push({
    type: "Feature", id,
    properties: {
      role: "semantic_zone",
      zone_id: id,
      name: `Zone ${state.zones.length + 1}`,
      behavior: "allow",
      color: "#5c6cf2",
      area_m2: Number(metrics.area.toFixed(2)),
      centroid: metrics.centroid.map((value) => Number(value.toFixed(3))),
    },
    geometry: {type: "Polygon", coordinates: [ring]},
  });
  state.mode = "idle";
  persist();
  selectZone(id);
  renderDraft();
}

function loadUserMap(value, filename) {
  const floor = value.features?.find((feature) => feature.properties?.role === "walkable_area");
  if (value.type !== "FeatureCollection" || !value.map_id || !floor || !["Polygon", "MultiPolygon"].includes(floor.geometry?.type)) {
    throw new Error("Malbut User Map 형식이 아닙니다.");
  }
  state.userMap = value;
  const importedRooms = value.features.filter((feature) => feature.properties?.role === "room");
  if (!importedRooms.length) throw new Error("Room이 없는 User Map입니다.");
  state.sourceRooms = JSON.parse(JSON.stringify(importedRooms));
  state.rooms = refreshGeneratedInitialRoom(readStoredArray(
    roomStorageKey(value.map_id), importedRooms, "방"
  ), importedRooms);
  localStorage.setItem(
    roomStorageKey(value.map_id), JSON.stringify(state.rooms)
  );
  state.userMap.features = [
    ...value.features.filter((feature) => feature.properties?.role !== "room"),
    ...state.rooms,
  ];
  state.zones = readStoredArray(
    `malbut-zones:${value.map_id}`, [], "Zone"
  );
  normalizeZones();
  state.selectedRoomId = null;
  state.selectedId = null;
  clearSplitDraft();
  state.mode = "idle";
  state.roomActionPending = false;
  $("#mapName").textContent = floor.properties.name || filename;
  $("#saveState").textContent = `방 ${state.rooms.length}개 · 지도 ID ${value.map_id}`;
  $("#emptyState").classList.add("hidden");
  svg.classList.remove("hidden");
  $("#newZone").disabled = false;
  $("#exportMap").disabled = false;
  $("#exportZones").disabled = false;
  $("#applyZones").disabled = !state.zoneApplyEnabled;
  $("#zoneForm").classList.add("hidden");
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  updateRoomActionButtons();
  renderFloor(); renderRooms(); renderZones(); fitMap();
}

async function readJsonFile(file) {
  return JSON.parse(await file.text());
}

function readStoredArray(key, fallback, label) {
  const serialized = localStorage.getItem(key);
  if (serialized === null) return fallback;
  try {
    const value = JSON.parse(serialized);
    if (!Array.isArray(value)) throw new Error("배열 형식이 아닙니다.");
    return value;
  } catch (error) {
    localStorage.removeItem(key);
    alert(`${label} 자동 저장 데이터가 손상되어 초기 상태로 복구했습니다.`);
    return fallback;
  }
}

function refreshGeneratedInitialRoom(storedRooms, importedRooms) {
  if (
    storedRooms.length !== 1 || importedRooms.length !== 1
    || storedRooms[0].id !== importedRooms[0].id
    || storedRooms[0].properties?.generated !== true
  ) return storedRooms;
  const imported = importedRooms[0];
  const refreshed = JSON.parse(JSON.stringify(storedRooms[0]));
  refreshed.geometry = JSON.parse(JSON.stringify(imported.geometry));
  refreshed.properties.area_m2 = imported.properties.area_m2;
  refreshed.properties.centroid = imported.properties.centroid;
  return [refreshed];
}

$("#mapFile").addEventListener("change", async (event) => {
  try { loadUserMap(await readJsonFile(event.target.files[0]), event.target.files[0].name); }
  catch (error) { alert(`지도를 열 수 없습니다: ${error.message}`); }
  event.target.value = "";
});

$("#zoneFile").addEventListener("change", async (event) => {
  try {
    if (!state.userMap) throw new Error("User Map을 먼저 여세요.");
    const value = await readJsonFile(event.target.files[0]);
    if (value.type !== "FeatureCollection" || value.format !== "malbut-semantic-zones-v1" || !Array.isArray(value.features)) {
      throw new Error("Malbut 영역 파일 형식이 아닙니다.");
    }
    if (value.map_id !== state.userMap.map_id) throw new Error("현재 집과 다른 map_id입니다.");
    const zones = value.features.filter((feature) => feature.properties?.role === "semantic_zone");
    if (!zones.length) throw new Error("불러올 Zone이 없습니다.");
    if (zones.length !== value.features.length) {
      throw new Error("Zone이 아닌 Feature가 포함되어 있습니다.");
    }
    const invalidZone = zones.some((zone) => {
      const ring = zone.geometry?.coordinates?.[0];
      return zone.type !== "Feature" || zone.geometry?.type !== "Polygon" ||
        !Array.isArray(ring) || ring.length < 4 || ring.some((point) =>
          !Array.isArray(point) || point.length < 2 ||
          !Number.isFinite(point[0]) || !Number.isFinite(point[1])
        );
    });
    if (invalidZone) throw new Error("올바르지 않은 Polygon Zone이 포함되어 있습니다.");
    const candidateZones = JSON.parse(JSON.stringify(zones));
    const floor = walkableFeature();
    for (const zone of candidateZones) {
      const error = validateZoneRing(zone.geometry.coordinates[0], floor);
      if (error) throw new Error(error);
    }
    normalizeZones(candidateZones);
    state.zones = candidateZones;
    state.selectedId = null; persist(); renderZones();
  } catch (error) { alert(`영역을 열 수 없습니다: ${error.message}`); }
  event.target.value = "";
});

$("#newZone").addEventListener("click", () => {
  if (!state.userMap) return;
  state.selectedRoomId = null;
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  createDefaultZone();
});

$("#splitRoom").addEventListener("click", () => {
  if (state.mode === "splitting") {
    if (state.pendingSplitPoint) {
      alert("선택 중인 시작점과 연결할 두 번째 벽을 지정하세요.");
      return;
    }
    if (!state.splitLines.length) {
      alert("벽 두 곳을 눌러 분할선을 하나 이상 만드세요.");
      return;
    }
    const room = selectedRoom();
    if (state.splitLines.some((line) =>
      !pointNearRoomWall(line[0], room.geometry) ||
      !pointNearRoomWall(line[line.length - 1], room.geometry)
    )) {
      alert("분할선의 시작점과 끝점은 벽에서 0.25m 이내에 두세요.");
      return;
    }
    splitSelectedRoom();
    return;
  }
  if (!selectedRoom()) return;
  state.mode = "splitting";
  clearSplitDraft();
  $("#zoneForm").classList.add("hidden");
  setModeBanner(); renderZones(); renderDraft();
});

$("#mergeRoom").addEventListener("click", () => {
  if (state.mode === "merging") {
    state.mode = "idle";
    setModeBanner(); renderRooms(); renderZones();
    return;
  }
  if (!selectedRoom() || state.rooms.length < 2) return;
  state.mode = "merging";
  clearSplitDraft();
  setModeBanner(); renderRooms(); renderZones(); renderDraft();
});

$("#resetRooms").addEventListener("click", () => {
  if (!state.userMap || !confirm("이름·유형·분할·병합을 모두 초기 상태로 되돌릴까요?")) return;
  state.rooms = JSON.parse(JSON.stringify(state.sourceRooms));
  state.selectedRoomId = null;
  state.mode = "idle";
  clearSplitDraft();
  persistRooms();
  localStorage.removeItem(roomStorageKey(state.userMap.map_id));
  if (state.userMap.room_segmentation) {
    state.userMap.room_segmentation.edited = false;
  }
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  $("#saveState").textContent = "초기 공간 1개로 되돌림";
  setModeBanner(); renderRooms(); renderZones(); renderDraft();
});

async function mergeSelectedRoomWith(targetId) {
  const source = selectedRoom();
  const target = state.rooms.find((room) => room.id === targetId);
  if (!source || !target) return;
  if (source.id === target.id) {
    alert("현재 방이 아닌 다른 방을 선택하세요.");
    return;
  }
  state.roomActionPending = true;
  updateRoomActionButtons();
  try {
    const response = await fetch("/api/merge-rooms", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        rooms: [source, target],
        resolution: state.userMap.source?.resolution || .05,
      }),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    if (!value.room || value.room.properties?.role !== "room") {
      throw new Error("병합 결과 형식이 올바르지 않습니다.");
    }
    const sourceIndex = state.rooms.findIndex((room) => room.id === source.id);
    const targetIndex = state.rooms.findIndex((room) => room.id === target.id);
    const insertAt = Math.min(sourceIndex, targetIndex);
    state.rooms = state.rooms.filter((room) => ![source.id, target.id].includes(room.id));
    state.rooms.splice(insertAt, 0, value.room);
    state.mode = "idle";
    persistRooms();
    selectRoom(value.room.id);
  } catch (error) {
    alert(`방을 합칠 수 없습니다: ${error.message}`);
    setModeBanner(); renderRooms(); renderZones();
  } finally {
    state.roomActionPending = false;
    updateRoomActionButtons();
  }
}

function scheduleSplitValidation() {
  if (state.splitValidationTimer) clearTimeout(state.splitValidationTimer);
  const revision = ++state.splitValidationRevision;
  if (!state.splitLines.length || state.pendingSplitPoint || !selectedRoom()) {
    state.splitValidation = "idle";
    state.splitValidationMessage = "";
    state.splitValidationTimer = null;
    setModeBanner(); renderDraft();
    return;
  }
  state.splitValidation = "checking";
  state.splitValidationMessage = "";
  setModeBanner(); renderDraft();
  state.splitValidationTimer = setTimeout(async () => {
    try {
      const response = await fetch("/api/split-room", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          room: selectedRoom(),
          lines: state.splitLines,
          resolution: state.userMap.source?.resolution || .05,
          minimum_room_area: 1.0,
        }),
      });
      const value = await response.json();
      if (revision !== state.splitValidationRevision) return;
      state.splitValidation = response.ok ? "valid" : "invalid";
      state.splitValidationMessage = response.ok
        ? "" : splitErrorMessage(value.error || `HTTP ${response.status}`);
    } catch (error) {
      if (revision !== state.splitValidationRevision) return;
      state.splitValidation = "invalid";
      state.splitValidationMessage = splitErrorMessage(error.message);
    }
    state.splitValidationTimer = null;
    setModeBanner(); renderDraft();
  }, 180);
}

async function splitSelectedRoom() {
  const room = selectedRoom();
  if (!room || !state.splitLines.length || state.pendingSplitPoint) return;
  state.roomActionPending = true;
  updateRoomActionButtons();
  try {
    const response = await fetch("/api/split-room", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        room,
        lines: state.splitLines,
        resolution: state.userMap.source?.resolution || .05,
        minimum_room_area: 1.0,
      }),
    });
    const value = await response.json();
    if (!response.ok) {
      throw new Error(splitErrorMessage(value.error || `HTTP ${response.status}`));
    }
    const index = state.rooms.findIndex((candidate) => candidate.id === room.id);
    if (index < 0 || !Array.isArray(value.rooms) || value.rooms.length !== 2) {
      throw new Error("분할 결과 형식이 올바르지 않습니다.");
    }
    state.rooms.splice(index, 1, ...value.rooms);
    clearSplitDraft();
    state.mode = "idle";
    persistRooms();
    selectRoom(value.rooms[0].id);
    renderDraft();
  } catch (error) {
    setModeBanner(); renderDraft();
    alert(`방을 나눌 수 없습니다: ${error.message}`);
  } finally {
    state.roomActionPending = false;
    updateRoomActionButtons();
  }
}

svg.addEventListener("click", (event) => {
  if (!state.userMap) return;
  const point = worldPoint(event);
  if (state.mode === "splitting") {
    const room = selectedRoom();
    const nearWall = room && pointNearRoomWall(point, room.geometry);
    if (!nearWall) {
      alert("분할선의 두 끝점은 벽에서 0.25m 이내에 지정하세요.");
      return;
    }
    if (state.pendingSplitPoint) {
      state.splitLines.push([state.pendingSplitPoint, point]);
      state.pendingSplitPoint = null;
    } else {
      state.pendingSplitPoint = point;
    }
    scheduleSplitValidation();
  } else if (state.mode === "merging") {
    alert("합칠 다른 방을 지도나 방 목록에서 선택하세요.");
  } else if (state.mode === "goal") {
    const zone = selectedZone();
    if (!pointInPolygon(point, zone.geometry.coordinates)) { alert("대표 위치는 선택한 영역 안에 있어야 합니다."); return; }
    zone.properties.preferred_goal = point; state.mode = "idle"; persist(); setModeBanner(); renderZones();
  } else {
    state.selectedRoomId = null;
    state.selectedId = null;
    clearSplitDraft();
    $("#zoneForm").classList.add("hidden");
    $("#roomSummary").classList.add("hidden");
    $("#roomForm").classList.add("hidden");
    setModeBanner();
    renderRooms();
    renderZones();
  }
});

svg.addEventListener("pointermove", (event) => {
  if (!state.dragging) return;
  let point = worldPoint(event);
  if (state.dragging.type === "split") {
    const room = selectedRoom();
    const line = state.splitLines[state.dragging.lineIndex];
    const index = state.dragging.index;
    const endpoint = index === 0 || index === line.length - 1;
    if (!endpoint) {
      point = orthogonalCorner(
        point, line[index - 1], line[index + 1], room.geometry
      );
    }
    const valid = endpoint
      ? pointNearRoomWall(point, room.geometry)
      : pointInGeometry(point, room.geometry) || pointNearRoomWall(point, room.geometry);
    if (!valid) return;
    line[index] = point;
    scheduleSplitValidation();
    return;
  }
  const zone = selectedZone();
  if (!zone) return;
  const floor = walkableFeature();
  let ring;
  if (state.dragging.type === "zone-move") {
    const deltaX = point[0] - state.dragging.origin[0];
    const deltaY = point[1] - state.dragging.origin[1];
    ring = state.dragging.ring.map((vertex) => [
      vertex[0] + deltaX, vertex[1] + deltaY,
    ]);
  } else if (state.dragging.type === "zone-corner") {
    ring = rectangleRing(
      point[0], point[1],
      state.dragging.opposite[0], state.dragging.opposite[1],
    );
  } else if (state.dragging.type === "zone-edge") {
    const bounds = {...state.dragging.bounds};
    bounds[state.dragging.side] = state.dragging.side.endsWith("X")
      ? point[0] : point[1];
    if (bounds.minX >= bounds.maxX || bounds.minY >= bounds.maxY) return;
    ring = rectangleRing(bounds.minX, bounds.minY, bounds.maxX, bounds.maxY);
  } else return;
  if (validateZoneRing(ring, floor)) return;
  zone.geometry.coordinates[0] = ring;
  updateZoneGeometryMetadata(zone);
  $("#zoneArea").textContent = `${zone.properties.area_m2.toFixed(2)} m²`;
  renderZones();
});
window.addEventListener("pointerup", () => {
  if (!state.dragging) return;
  const splitDrag = state.dragging.type === "split";
  state.dragging = null;
  if (splitDrag) renderDraft();
  else { persist(); renderZones(); }
});

$("#zoneName").addEventListener("input", (event) => updateSelectedProperty("name", event.target.value || "이름 없는 공간"));
$("#zoneBehavior").addEventListener("change", (event) => updateSelectedProperty("behavior", event.target.value));
$("#zoneColor").addEventListener("input", (event) => updateSelectedProperty("color", event.target.value));
$("#roomName").addEventListener("input", (event) => updateSelectedRoomProperty("name", event.target.value || "이름 없는 방"));
$("#roomCategory").addEventListener("change", (event) => updateSelectedRoomProperty("category", event.target.value));
$("#setGoal").addEventListener("click", () => { state.mode = "goal"; setModeBanner(); });
$("#deleteZone").addEventListener("click", () => {
  if (!selectedZone() || !confirm("이 영역을 삭제할까요?")) return;
  state.zones = state.zones.filter((zone) => zone.id !== state.selectedId);
  state.selectedId = null; $("#zoneForm").classList.add("hidden"); persist(); renderZones();
});

function downloadJson(value, filename) {
  const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], {type: "application/geo+json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob); link.download = filename; link.click();
  URL.revokeObjectURL(link.href);
}

function zoneCollection() {
  normalizeZones();
  return {
    type: "FeatureCollection",
    format: "malbut-semantic-zones-v1",
    map_id: state.userMap.map_id,
    frame_id: state.userMap.frame_id,
    features: state.zones,
  };
}

$("#exportMap").addEventListener("click", () => {
  persistRooms();
  downloadJson(state.userMap, `${state.userMap.map_id}-user-map.geojson`);
});

$("#exportZones").addEventListener("click", () => {
  downloadJson(zoneCollection(), `${state.userMap.map_id}-zones.geojson`);
});

$("#applyZones").addEventListener("click", async () => {
  if (!state.userMap || !state.zoneApplyEnabled) return;
  const button = $("#applyZones");
  button.disabled = true;
  button.textContent = "적용 중…";
  try {
    const response = await fetch("/api/apply-zones", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(zoneCollection()),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    persist();
    $("#saveState").textContent = value.nav2_reloaded
      ? "Zone이 실행 중인 Nav2에 적용됨"
      : "Zone 주행 설정 저장됨 · 다음 Nav2 실행부터 적용";
  } catch (error) {
    alert(`Zone을 주행에 적용할 수 없습니다: ${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "주행에 적용";
  }
});

window.addEventListener("keydown", (event) => {
  if (state.mode === "splitting" && event.key === "Backspace") {
    event.preventDefault();
    if (state.pendingSplitPoint) state.pendingSplitPoint = null;
    else state.splitLines.pop();
    scheduleSplitValidation();
    return;
  }
  if (event.key !== "Escape" || !["goal", "splitting", "merging"].includes(state.mode)) return;
  state.mode = "idle";
  clearSplitDraft();
  setModeBanner(); renderDraft(); renderRooms(); renderZones();
});

function zoom(factor) {
  if (!state.viewBox) return;
  const [x, y, width, height] = state.viewBox;
  const nextWidth = width * factor, nextHeight = height * factor;
  setViewBox([x + (width - nextWidth) / 2, y + (height - nextHeight) / 2, nextWidth, nextHeight]);
}
$("#zoomIn").addEventListener("click", () => zoom(.8));
$("#zoomOut").addEventListener("click", () => zoom(1.25));
$("#fitMap").addEventListener("click", fitMap);

async function loadMapFromUrl() {
  const source = new URLSearchParams(window.location.search).get("map");
  if (!source) return;
  try {
    const response = await fetch(source);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    loadUserMap(await response.json(), source.split("/").pop());
  } catch (error) {
    alert(`지도를 자동으로 열 수 없습니다: ${error.message}`);
  }
}

async function loadEditorConfig() {
  try {
    const response = await fetch("/api/editor-config");
    if (!response.ok) return;
    const value = await response.json();
    state.zoneApplyEnabled = Boolean(value.zone_apply_enabled);
    $("#applyZones").disabled = !state.userMap || !state.zoneApplyEnabled;
  } catch (_error) {
    state.zoneApplyEnabled = false;
  }
}

loadEditorConfig();
loadMapFromUrl();
