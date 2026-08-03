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
  draft: [],
  draftRoomId: null,
  splitPoints: [],
  mode: "idle",
  roomActionPending: false,
  dragging: null,
  viewBox: null,
};

const CATEGORY_LABELS = {
  living_room: "거실", bedroom: "침실", kitchen: "주방",
  bathroom: "욕실", entrance: "현관", workspace: "작업 공간",
  custom: "기타",
};
const ROOM_CATEGORY_LABELS = {
  unassigned: "미지정", living_room: "거실", bedroom: "침실",
  kitchen: "주방", dining_room: "식당", bathroom: "욕실",
  entrance: "현관", hallway: "복도", workspace: "작업 공간",
  storage: "수납 공간", utility: "다용도실", custom: "기타",
};
const BEHAVIOR_LABELS = {
  allow: "진입 허용", avoid: "가급적 회피", restricted: "진입 금지",
};

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

function roomContainingPoint(point) {
  return state.rooms.find((room) => pointInGeometry(point, room.geometry));
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

function validateZoneRing(ring, room) {
  if (!room) return "Zone이 속할 Room을 찾을 수 없습니다.";
  if (ring.length < 4) return "Zone은 세 점 이상으로 그려야 합니다.";
  if (ringSelfIntersects(ring)) return "Zone 경계가 서로 교차할 수 없습니다.";
  const metrics = ringMetrics(ring);
  if (metrics.area < .1) return "Zone 면적은 0.1 m² 이상이어야 합니다.";
  for (let index = 0; index < ring.length - 1; index += 1) {
    if (!segmentInsideGeometry(
      ring[index], ring[index + 1], room.geometry
    )) return "Zone 경계 전체가 하나의 Room 안에 있어야 합니다.";
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
    empty.textContent = "자동 생성된 방 후보가 없습니다.";
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
  state.splitPoints = [];
  $("#zoneForm").classList.add("hidden");
  const room = selectedRoom();
  $("#roomSummary").classList.toggle("hidden", !room);
  $("#roomForm").classList.toggle("hidden", !room);
  if (room) {
    const category = room.properties.category || "unassigned";
    $("#roomSummaryColor").style.background = room.properties.color;
    $("#roomSummaryName").textContent = roomDisplayName(room);
    $("#roomSummaryDetail").textContent = `${room.properties.area_m2.toFixed(2)} m² · ${ROOM_CATEGORY_LABELS[category] || "기타"}`;
    $("#roomSummaryBadge").textContent = room.properties.edited ? "편집됨" : "자동 생성";
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

function reconcileZoneRooms(zones = state.zones) {
  for (const zone of zones) {
    if (zone.geometry?.type !== "Polygon") {
      zone.properties.room_id = null;
      zone.properties.room_name = null;
      zone.properties.needs_review = true;
      continue;
    }
    const ring = zone.geometry.coordinates[0];
    const previous = roomById(zone.properties.room_id);
    const room = previous && !validateZoneRing(ring, previous)
      ? previous
      : state.rooms.find((candidate) => !validateZoneRing(ring, candidate));
    zone.properties.room_id = room?.id || null;
    zone.properties.room_name = room ? room.properties.name : null;
    zone.properties.needs_review = !room;
    updateZoneGeometryMetadata(zone);
  }
}

function renderZones() {
  const layer = $("#zoneLayer");
  layer.replaceChildren();
  for (const zone of state.zones) {
    const color = zone.properties.color;
    const path = svgElement("path", {
      d: zonePath(zone),
      class: `zone-shape${zone.id === state.selectedId ? " selected" : ""}${zone.properties.needs_review ? " needs-review" : ""}`,
      fill: `${color}42`, stroke: color, "data-zone-id": zone.id,
    });
    path.addEventListener("click", (event) => {
      if (["splitting", "merging"].includes(state.mode)) return;
      event.stopPropagation(); selectZone(zone.id);
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
  ring.forEach((point, index) => {
    const handle = svgElement("circle", {cx: point[0], cy: -point[1], r: .11, class: "vertex"});
    handle.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      state.dragging = {type: "vertex", index};
      handle.setPointerCapture(event.pointerId);
    });
    layer.append(handle);
  });
  const goal = zone.properties.preferred_goal;
  if (goal) {
    layer.append(svgElement("circle", {cx: goal[0], cy: -goal[1], r: .13, class: "goal"}));
  }
}

function renderDraft() {
  const layer = $("#draftLayer");
  layer.replaceChildren();
  if (state.draft.length) {
    const points = state.draft.map((point) => `${point[0]},${-point[1]}`).join(" ");
    layer.append(svgElement("polygon", {points, class: "draft-line"}));
    for (const point of state.draft) {
      layer.append(svgElement("circle", {cx: point[0], cy: -point[1], r: .09, class: "vertex"}));
    }
  }
  if (state.splitPoints.length) {
    if (state.splitPoints.length === 2) {
      const [first, second] = state.splitPoints;
      layer.append(svgElement("line", {
        x1: first[0], y1: -first[1], x2: second[0], y2: -second[1], class: "split-line",
      }));
    }
    for (const point of state.splitPoints) {
      layer.append(svgElement("circle", {cx: point[0], cy: -point[1], r: .12, class: "split-point"}));
    }
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
    item.className = `zone-item${zone.id === state.selectedId ? " selected" : ""}${zone.properties.needs_review ? " needs-review" : ""}`;
    item.innerHTML = `<i class="zone-dot"></i><span><strong></strong><small></small></span><em>›</em>`;
    item.querySelector("i").style.background = zone.properties.color;
    item.querySelector("strong").textContent = zone.properties.name;
    const roomName = zone.properties.room_name || "소속 방 확인 필요";
    item.querySelector("small").textContent = `${CATEGORY_LABELS[zone.properties.category] ?? "기타"} · ${BEHAVIOR_LABELS[zone.properties.behavior] ?? "진입 허용"} · ${roomName}`;
    item.addEventListener("click", () => selectZone(zone.id));
    list.append(item);
  }
}

function selectZone(id) {
  state.selectedRoomId = null;
  state.selectedId = id;
  state.mode = "idle";
  state.splitPoints = [];
  const zone = selectedZone();
  $("#zoneForm").classList.toggle("hidden", !zone);
  if (zone) {
    $("#zoneName").value = zone.properties.name;
    $("#zoneCategory").value = zone.properties.category;
    $("#zoneBehavior").value = zone.properties.behavior;
    $("#zoneColor").value = zone.properties.color;
    $("#zoneRoom").textContent = zone.properties.room_name || "확인 필요";
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
  const draftRoom = roomById(state.draftRoomId);
  const messages = {
    drawing: `${draftRoom ? `${draftRoom.properties.name} 안에서 ` : ""}Zone 꼭짓점을 순서대로 누르세요. 마지막에는 ‘그리기 완료’를 누르세요.`,
    goal: "선택한 영역 안에서 로봇의 대표 도착 위치를 누르세요.",
    splitting: "선택한 방을 가로지르도록 두 지점을 누르세요. 두 점을 지나는 직선으로 방이 나뉩니다.",
    merging: "현재 방과 합칠 다른 방을 지도나 방 목록에서 선택하세요.",
  };
  banner.textContent = messages[state.mode] ?? "";
  banner.classList.toggle("hidden", !messages[state.mode]);
  $("#newZone").textContent = state.mode === "drawing" ? "✓" : "＋";
  $("#newZone").title = state.mode === "drawing" ? "그리기 완료" : "새 영역";
  $("#splitRoom").textContent = state.mode === "splitting" ? "나누기 취소" : "방 나누기";
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
    $("#splitRoom").disabled = false;
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
  reconcileZoneRooms();
  localStorage.setItem(`malbut-rooms:${state.userMap.map_id}`, JSON.stringify(state.rooms));
  localStorage.setItem(`malbut-zones:${state.userMap.map_id}`, JSON.stringify(state.zones));
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

function finishDraft() {
  if (state.draft.length < 3) {
    alert("영역은 세 점 이상으로 그려야 합니다.");
    return;
  }
  const room = roomById(state.draftRoomId);
  const ring = [...state.draft, state.draft[0]];
  const error = validateZoneRing(ring, room);
  if (error) {
    alert(error);
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
      category: "custom",
      behavior: "allow",
      color: "#5c6cf2",
      room_id: room.id,
      room_name: room.properties.name,
      area_m2: Number(metrics.area.toFixed(2)),
      centroid: metrics.centroid.map((value) => Number(value.toFixed(3))),
      needs_review: false,
    },
    geometry: {type: "Polygon", coordinates: [ring]},
  });
  state.draft = [];
  state.draftRoomId = null;
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
  const generatedRooms = value.features.filter((feature) => feature.properties?.role === "room");
  state.sourceRooms = JSON.parse(JSON.stringify(generatedRooms));
  state.rooms = readStoredArray(
    `malbut-rooms:${value.map_id}`, generatedRooms, "방"
  );
  state.userMap.features = [
    ...value.features.filter((feature) => feature.properties?.role !== "room"),
    ...state.rooms,
  ];
  state.zones = readStoredArray(
    `malbut-zones:${value.map_id}`, [], "Zone"
  );
  reconcileZoneRooms();
  state.selectedRoomId = null;
  state.selectedId = null;
  state.draftRoomId = null;
  state.splitPoints = [];
  state.mode = "idle";
  state.roomActionPending = false;
  $("#mapName").textContent = floor.properties.name || filename;
  $("#saveState").textContent = `방 후보 ${state.rooms.length}개 · 지도 ID ${value.map_id}`;
  $("#emptyState").classList.add("hidden");
  svg.classList.remove("hidden");
  $("#newZone").disabled = false;
  $("#exportMap").disabled = false;
  $("#exportZones").disabled = false;
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
    reconcileZoneRooms(candidateZones);
    if (candidateZones.some((zone) => zone.properties.needs_review)) {
      throw new Error("현재 Room 경계 안에 들어오지 않는 Zone이 있습니다.");
    }
    state.zones = candidateZones;
    state.selectedId = null; persist(); renderZones();
  } catch (error) { alert(`영역을 열 수 없습니다: ${error.message}`); }
  event.target.value = "";
});

$("#newZone").addEventListener("click", () => {
  if (state.mode === "drawing") { finishDraft(); return; }
  state.draftRoomId = state.selectedRoomId;
  state.selectedRoomId = null; state.selectedId = null; state.draft = []; state.mode = "drawing";
  $("#zoneForm").classList.add("hidden");
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  setModeBanner(); renderRooms(); renderZones(); renderDraft();
});

$("#splitRoom").addEventListener("click", () => {
  if (state.mode === "splitting") {
    state.mode = "idle";
    state.splitPoints = [];
    setModeBanner(); renderDraft();
    return;
  }
  if (!selectedRoom()) return;
  state.mode = "splitting";
  state.splitPoints = [];
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
  state.splitPoints = [];
  setModeBanner(); renderRooms(); renderZones(); renderDraft();
});

$("#resetRooms").addEventListener("click", () => {
  if (!state.userMap || !confirm("이름·유형·분할·병합을 모두 초기 상태로 되돌릴까요?")) return;
  state.rooms = JSON.parse(JSON.stringify(state.sourceRooms));
  state.selectedRoomId = null;
  state.mode = "idle";
  state.draftRoomId = null;
  state.splitPoints = [];
  persistRooms();
  localStorage.removeItem(`malbut-rooms:${state.userMap.map_id}`);
  if (state.userMap.room_segmentation) {
    state.userMap.room_segmentation.edited = false;
  }
  $("#roomSummary").classList.add("hidden");
  $("#roomForm").classList.add("hidden");
  $("#saveState").textContent = "자동 생성 방 구성으로 초기화됨";
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

async function splitSelectedRoom() {
  const room = selectedRoom();
  if (!room || state.splitPoints.length !== 2) return;
  state.roomActionPending = true;
  updateRoomActionButtons();
  try {
    const response = await fetch("/api/split-room", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        room,
        line: state.splitPoints,
        resolution: state.userMap.source?.resolution || .05,
        minimum_room_area: 1.0,
      }),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    const index = state.rooms.findIndex((candidate) => candidate.id === room.id);
    if (index < 0 || !Array.isArray(value.rooms) || value.rooms.length !== 2) {
      throw new Error("분할 결과 형식이 올바르지 않습니다.");
    }
    state.rooms.splice(index, 1, ...value.rooms);
    state.splitPoints = [];
    state.mode = "idle";
    persistRooms();
    selectRoom(value.rooms[0].id);
    renderDraft();
  } catch (error) {
    state.splitPoints = [];
    state.mode = "idle";
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
  if (state.mode === "drawing") {
    const room = roomById(state.draftRoomId) || roomContainingPoint(point);
    if (!room || !pointInGeometry(point, room.geometry)) {
      alert("Zone 꼭짓점은 하나의 Room 안에 지정하세요.");
      return;
    }
    if (state.draft.length && !segmentInsideGeometry(
      state.draft[state.draft.length - 1], point, room.geometry
    )) {
      alert("Zone 경계가 Room 밖으로 나갈 수 없습니다.");
      return;
    }
    state.draftRoomId = room.id;
    state.draft.push(point); setModeBanner(); renderDraft();
  } else if (state.mode === "splitting") {
    const room = selectedRoom();
    if (!room || !pointInGeometry(point, room.geometry)) {
      alert("분할 지점은 선택한 방 안에 지정하세요.");
      return;
    }
    state.splitPoints.push(point);
    renderDraft();
    if (state.splitPoints.length === 2) splitSelectedRoom();
  } else if (state.mode === "merging") {
    alert("합칠 다른 방을 지도나 방 목록에서 선택하세요.");
  } else if (state.mode === "goal") {
    const zone = selectedZone();
    if (!pointInPolygon(point, zone.geometry.coordinates)) { alert("대표 위치는 선택한 영역 안에 있어야 합니다."); return; }
    zone.properties.preferred_goal = point; state.mode = "idle"; persist(); setModeBanner(); renderZones();
  } else {
    state.selectedRoomId = null;
    state.selectedId = null;
    state.draftRoomId = null;
    state.splitPoints = [];
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
  const point = worldPoint(event);
  const zone = selectedZone();
  if (!zone) return;
  const ring = zone.geometry.coordinates[0].map((vertex) => [...vertex]);
  ring[state.dragging.index] = point;
  if (state.dragging.index === 0) ring[ring.length - 1] = point;
  let room = roomById(zone.properties.room_id);
  if (zone.properties.needs_review || !room) {
    const floor = {geometry: walkableFeature().geometry};
    if (validateZoneRing(ring, floor)) return;
    room = state.rooms.find((candidate) => !validateZoneRing(ring, candidate));
  } else if (validateZoneRing(ring, room)) {
    return;
  }
  zone.geometry.coordinates[0] = ring;
  zone.properties.room_id = room?.id || null;
  zone.properties.room_name = room?.properties.name || null;
  zone.properties.needs_review = !room;
  updateZoneGeometryMetadata(zone);
  $("#zoneRoom").textContent = zone.properties.room_name || "확인 필요";
  $("#zoneArea").textContent = `${zone.properties.area_m2.toFixed(2)} m²`;
  renderZones();
});
window.addEventListener("pointerup", () => { if (state.dragging) { state.dragging = null; persist(); renderZones(); } });

$("#zoneName").addEventListener("input", (event) => updateSelectedProperty("name", event.target.value || "이름 없는 공간"));
$("#zoneCategory").addEventListener("change", (event) => updateSelectedProperty("category", event.target.value));
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

$("#exportMap").addEventListener("click", () => {
  persistRooms();
  downloadJson(state.userMap, `${state.userMap.map_id}-user-map.geojson`);
});

$("#exportZones").addEventListener("click", () => {
  reconcileZoneRooms();
  if (state.zones.some((zone) => zone.properties.needs_review)) {
    alert("소속 방을 확인해야 하는 Zone을 수정하거나 삭제한 뒤 내보내세요.");
    renderZones();
    return;
  }
  const value = {type: "FeatureCollection", format: "malbut-semantic-zones-v1", map_id: state.userMap.map_id, frame_id: state.userMap.frame_id, features: state.zones};
  downloadJson(value, `${state.userMap.map_id}-zones.geojson`);
});

window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !["drawing", "goal", "splitting", "merging"].includes(state.mode)) return;
  state.mode = "idle";
  state.draft = [];
  state.draftRoomId = null;
  state.splitPoints = [];
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

loadMapFromUrl();
