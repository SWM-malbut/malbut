"use strict";

const elements = {
  canvas: document.querySelector("#map"),
  empty: document.querySelector("#empty-map"),
  connection: document.querySelector("#connection"),
  state: document.querySelector("#state-label"),
  message: document.querySelector("#message"),
  knownArea: document.querySelector("#known-area"),
  freeArea: document.querySelector("#free-area"),
  frontiers: document.querySelector("#frontiers"),
  start: document.querySelector("#start"),
  finish: document.querySelector("#finish"),
  cancel: document.querySelector("#cancel"),
  error: document.querySelector("#action-error"),
  saved: document.querySelector("#saved-revision"),
};

const labels = {
  idle: "시작 전", waiting_for_map: "지도 준비 중",
  waiting_for_navigation: "자율주행 준비 중", exploring: "탐색 중",
  navigating: "이동 중", review: "확인 필요", saving: "저장 중",
  ready: "저장 완료", canceled: "중단됨", failed: "저장 실패",
};

let csrfToken = "";
let latest = null;
let mapImage = null;
let loadedRevision = -1;

function active(state) {
  return ["waiting_for_map", "waiting_for_navigation", "exploring", "navigating", "review"].includes(state);
}

function updateButtons(value) {
  const isActive = active(value.state);
  elements.start.disabled = isActive || value.state === "saving";
  elements.start.textContent = ["canceled", "failed"].includes(value.state)
    ? "다시 시도" : (value.active_revision ? "지도 다시 만들기" : "지도 만들기 시작");
  elements.finish.disabled = !isActive || !value.map || value.map.free_area_m2 < 1;
  elements.cancel.disabled = !isActive;
}

function canvasTransform(metadata) {
  const canvas = elements.canvas;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const scale = Math.min(canvas.width / metadata.width, canvas.height / metadata.height);
  return {
    scale,
    left: (canvas.width - metadata.width * scale) / 2,
    top: (canvas.height - metadata.height * scale) / 2,
  };
}

function pointToCanvas(point, metadata, transform) {
  const dx = point.x - metadata.origin_x;
  const dy = point.y - metadata.origin_y;
  const cosine = Math.cos(metadata.origin_yaw || 0);
  const sine = Math.sin(metadata.origin_yaw || 0);
  const localX = cosine * dx + sine * dy;
  const localY = -sine * dx + cosine * dy;
  const column = localX / metadata.resolution;
  const rowFromTop = metadata.height - localY / metadata.resolution;
  return [transform.left + column * transform.scale, transform.top + rowFromTop * transform.scale];
}

function drawMap() {
  const canvas = elements.canvas;
  const context = canvas.getContext("2d");
  if (!latest || !latest.map || !mapImage) {
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  const transform = canvasTransform(latest.map);
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.imageSmoothingEnabled = false;
  context.drawImage(
    mapImage, transform.left, transform.top,
    latest.map.width * transform.scale, latest.map.height * transform.scale,
  );
  if (latest.target) {
    const [x, y] = pointToCanvas(latest.target, latest.map, transform);
    context.strokeStyle = "#ff6b35";
    context.lineWidth = Math.max(3, transform.scale * 1.5);
    context.beginPath();
    context.arc(x, y, Math.max(8, transform.scale * 3), 0, Math.PI * 2);
    context.stroke();
  }
  if (latest.pose) {
    const [x, y] = pointToCanvas(latest.pose, latest.map, transform);
    const radius = Math.max(8, transform.scale * 3.5);
    context.save();
    context.translate(x, y);
    context.rotate(-latest.pose.yaw);
    context.fillStyle = "#176fdb";
    context.strokeStyle = "white";
    context.lineWidth = 3;
    context.beginPath();
    context.moveTo(radius * 1.35, 0);
    context.lineTo(-radius, radius * .8);
    context.lineTo(-radius, -radius * .8);
    context.closePath();
    context.fill();
    context.stroke();
    context.restore();
  }
}

function loadMap(revision) {
  if (revision === loadedRevision) return;
  const image = new Image();
  image.onload = () => {
    mapImage = image;
    loadedRevision = revision;
    elements.empty.hidden = true;
    drawMap();
  };
  image.src = `/api/mapping/map.png?revision=${revision}`;
}

function render(value) {
  latest = value;
  elements.state.textContent = labels[value.state] || value.state;
  elements.message.textContent = value.message;
  elements.knownArea.textContent = `${value.map?.known_area_m2?.toFixed(1) || "0.0"} m²`;
  elements.freeArea.textContent = `${value.map?.free_area_m2?.toFixed(1) || "0.0"} m²`;
  elements.frontiers.textContent = String(value.frontier_count || 0);
  elements.saved.textContent = value.state === "ready" && value.active_revision
    ? `저장 버전: ${value.active_revision.revision}` : "";
  if (value.map) loadMap(value.map_revision);
  updateButtons(value);
  drawMap();
}

async function command(path, body = {}) {
  elements.error.textContent = "";
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "요청을 처리하지 못했습니다.");
  render(value);
}

async function initialize() {
  try {
    const configResponse = await fetch("/api/editor-config", {cache: "no-store"});
    const config = await configResponse.json();
    csrfToken = config.csrf_token;
    const statusResponse = await fetch("/api/mapping/status", {cache: "no-store"});
    render(await statusResponse.json());
    const stream = new EventSource("/api/mapping/stream");
    stream.addEventListener("mapping", event => {
      elements.connection.textContent = "로봇과 연결됨";
      elements.connection.classList.add("online");
      render(JSON.parse(event.data));
    });
    stream.onerror = () => {
      elements.connection.textContent = "다시 연결 중";
      elements.connection.classList.remove("online");
    };
  } catch (error) {
    elements.error.textContent = error.message;
  }
}

elements.start.addEventListener("click", () => command(
  "/api/mapping/start", {replace: Boolean(latest?.active_revision)},
).catch(error => { elements.error.textContent = error.message; }));
elements.finish.addEventListener("click", () => command(
  "/api/mapping/finish",
).catch(error => { elements.error.textContent = error.message; }));
elements.cancel.addEventListener("click", () => command(
  "/api/mapping/cancel",
).catch(error => { elements.error.textContent = error.message; }));
window.addEventListener("resize", drawMap);
initialize();
