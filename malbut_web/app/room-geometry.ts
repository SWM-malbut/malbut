/**
 * Room editing for the web map editor: split, merge and normalize User Map rooms.
 *
 * Ported from malbut_gazebo/room_editor.py (SWM25-237) so rooms can be edited while
 * the robot is off. The web server is the one place this runs, for the simulator and
 * the real robot alike; the robot only stores the final rooms (rooms_save).
 *
 * The method is the simulator's: rasterize the room on a local grid, cut it along the
 * divider, label the two sides, and trace the sides back to polygons through pixel
 * centers, so neighboring rooms share their boundary exactly. The small image
 * operations OpenCV provided there (fillPoly, thick polylines, 3x3 morphology,
 * connected components, distance transform, findContours) are implemented below.
 */

import { createHash } from "node:crypto";

type Point = [number, number];
type Ring = Point[];
type PolygonCoordinates = Ring[];
export type RoomGeometry =
  | { type: "Polygon"; coordinates: PolygonCoordinates }
  | { type: "MultiPolygon"; coordinates: PolygonCoordinates[] };
export type RoomFeature = {
  type: "Feature";
  id?: string;
  properties: Record<string, unknown>;
  geometry: RoomGeometry;
};

export class RoomGeometryError extends Error {}

const SPLIT_COLORS = [
  "#dce8ff", "#f9e1c7", "#d9f0e3", "#eadffd", "#f8dce3", "#d9edf2", "#f3eabf", "#dfe4eb",
];
const WALL_SNAP_DISTANCE_METERS = 0.25;
// A room this large at 5 cm cells is a 200 m x 200 m floor: far beyond a home.
const MAX_PIXELS = 16_000_000;

// ------------------------------------------------------------------ numbers (Python parity)

/** Python's round(): halves go to the even neighbor. */
function pyRound(value: number) {
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

function round4(value: number) {
  const rounded = Number(value.toFixed(4));
  return Object.is(rounded, -0) ? 0 : rounded;
}

function round2(value: number) {
  const rounded = Number(value.toFixed(2));
  return Object.is(rounded, -0) ? 0 : rounded;
}

function round3(value: number) {
  const rounded = Number(value.toFixed(3));
  return Object.is(rounded, -0) ? 0 : rounded;
}

// ------------------------------------------------------------------ geometry helpers

function geometryPolygons(geometry: RoomGeometry): PolygonCoordinates[] {
  if (geometry?.type === "Polygon") return [geometry.coordinates];
  if (geometry?.type === "MultiPolygon") return geometry.coordinates;
  throw new RoomGeometryError("Room geometry must be Polygon or MultiPolygon");
}

function geometryPoints(geometry: RoomGeometry): Point[] {
  return geometryPolygons(geometry).flatMap((polygon) => polygon.flat());
}

function ringArea(ring: Ring) {
  let sum = 0;
  for (let index = 0; index + 1 < ring.length; index += 1) {
    sum += ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1];
  }
  return Math.abs(sum) / 2;
}

export function geometryArea(geometry: RoomGeometry) {
  return geometryPolygons(geometry).reduce((total, polygon) => (
    total + ringArea(polygon[0]) - polygon.slice(1).reduce((holes, hole) => holes + ringArea(hole), 0)
  ), 0);
}

function finitePoint(point: unknown): point is Point {
  return Array.isArray(point) && point.length >= 2 &&
    typeof point[0] === "number" && typeof point[1] === "number" &&
    Number.isFinite(point[0]) && Number.isFinite(point[1]);
}

function roomId(room: RoomFeature) {
  const value = room.id ?? room.properties?.room_id;
  return typeof value === "string" ? value : String(value);
}

/** JSON with sorted keys: a stable identity for one room's geometry. */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>).sort().map((key) => (
      `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}

// ------------------------------------------------------------------ raster grid

class LocalTransform {
  constructor(readonly minimumX: number, readonly maximumY: number, readonly resolution: number) {}

  pixel(point: Point): [number, number] {
    return [
      pyRound((point[0] - this.minimumX) / this.resolution),
      pyRound((this.maximumY - point[1]) / this.resolution),
    ];
  }

  world(x: number, y: number): Point {
    return [round4(this.minimumX + x * this.resolution), round4(this.maximumY - y * this.resolution)];
  }
}

class Mask {
  readonly data: Uint8Array;

  constructor(readonly width: number, readonly height: number, data?: Uint8Array) {
    this.data = data ?? new Uint8Array(width * height);
  }

  get(x: number, y: number) {
    return x >= 0 && y >= 0 && x < this.width && y < this.height ? this.data[y * this.width + x] : 0;
  }

  set(x: number, y: number, value: number) {
    if (x >= 0 && y >= 0 && x < this.width && y < this.height) this.data[y * this.width + x] = value;
  }

  clone() {
    return new Mask(this.width, this.height, this.data.slice());
  }
}

/** One-pixel 8-connected line (OpenCV LINE_8). */
function drawLine(mask: Mask, from: [number, number], to: [number, number], value: number) {
  let [x0, y0] = from;
  const [x1, y1] = to;
  const dx = Math.abs(x1 - x0);
  const dy = -Math.abs(y1 - y0);
  const sx = x0 < x1 ? 1 : -1;
  const sy = y0 < y1 ? 1 : -1;
  let error = dx + dy;
  for (;;) {
    mask.set(x0, y0, value);
    if (x0 === x1 && y0 === y1) return;
    const twice = 2 * error;
    if (twice >= dy) { error += dy; x0 += sx; }
    if (twice <= dx) { error += dx; y0 += sy; }
  }
}

function polylines(mask: Mask, points: Array<[number, number]>, closed: boolean, value: number, thickness = 1) {
  const count = closed ? points.length : points.length - 1;
  for (let index = 0; index < count; index += 1) {
    const from = points[index];
    const to = points[(index + 1) % points.length];
    if (thickness <= 1) drawLine(mask, from, to, value);
    else drawThickSegment(mask, from, to, value, thickness);
  }
}

/** A thick segment with round ends: every pixel center within thickness / 2. */
function drawThickSegment(mask: Mask, from: [number, number], to: [number, number], value: number, thickness: number) {
  const radius = thickness / 2;
  const minimumX = Math.floor(Math.min(from[0], to[0]) - radius);
  const maximumX = Math.ceil(Math.max(from[0], to[0]) + radius);
  const minimumY = Math.floor(Math.min(from[1], to[1]) - radius);
  const maximumY = Math.ceil(Math.max(from[1], to[1]) + radius);
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const length2 = dx * dx + dy * dy;
  for (let y = minimumY; y <= maximumY; y += 1) {
    for (let x = minimumX; x <= maximumX; x += 1) {
      const t = length2 === 0 ? 0 : Math.max(0, Math.min(1, ((x - from[0]) * dx + (y - from[1]) * dy) / length2));
      const px = from[0] + t * dx - x;
      const py = from[1] + t * dy - y;
      if (px * px + py * py <= radius * radius) mask.set(x, y, value);
    }
  }
}

/** Fill a polygon including its boundary pixels, like cv2.fillPoly. */
function fillPoly(mask: Mask, points: Array<[number, number]>, value: number) {
  const edges = points.map((point, index) => [point, points[(index + 1) % points.length]] as const);
  const ys = points.map((point) => point[1]);
  const top = Math.max(0, Math.min(...ys));
  const bottom = Math.min(mask.height - 1, Math.max(...ys));
  for (let y = top; y <= bottom; y += 1) {
    const crossings: number[] = [];
    for (const [a, b] of edges) {
      if (a[1] === b[1]) continue;
      const [low, high] = a[1] < b[1] ? [a, b] : [b, a];
      if (y < low[1] || y >= high[1]) continue;
      crossings.push(low[0] + (y - low[1]) * (high[0] - low[0]) / (high[1] - low[1]));
    }
    crossings.sort((first, second) => first - second);
    for (let index = 0; index + 1 < crossings.length; index += 2) {
      for (let x = Math.ceil(crossings[index]); x <= Math.floor(crossings[index + 1]); x += 1) {
        mask.set(x, y, value);
      }
    }
  }
  polylines(mask, points, true, value, 1);
}

function morphology(mask: Mask, erode: boolean) {
  const result = new Mask(mask.width, mask.height);
  for (let y = 0; y < mask.height; y += 1) {
    for (let x = 0; x < mask.width; x += 1) {
      let value = erode ? 255 : 0;
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const nx = x + dx;
          const ny = y + dy;
          // Outside the grid never erodes (OpenCV's default border for morphology).
          const neighbor = nx < 0 || ny < 0 || nx >= mask.width || ny >= mask.height
            ? (erode ? 255 : 0) : mask.data[ny * mask.width + nx];
          value = erode ? Math.min(value, neighbor) : Math.max(value, neighbor);
        }
      }
      result.data[y * mask.width + x] = value;
    }
  }
  return result;
}

const erode3 = (mask: Mask) => morphology(mask, true);
const dilate3 = (mask: Mask) => morphology(mask, false);
const close3 = (mask: Mask) => erode3(dilate3(mask));

const NEIGHBORS_8: Array<[number, number]> = [
  [-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [-1, 1], [1, -1], [1, 1],
];

/** 8-connected components: labels 1..count-1 (0 is background) and each area. */
function connectedComponents(mask: Mask) {
  const labels = new Int32Array(mask.width * mask.height);
  const areas = [0];
  let count = 1;
  const queue = new Int32Array(mask.width * mask.height);
  for (let start = 0; start < labels.length; start += 1) {
    if (mask.data[start] === 0 || labels[start] !== 0) continue;
    let head = 0;
    let tail = 0;
    queue[tail++] = start;
    labels[start] = count;
    let area = 0;
    while (head < tail) {
      const index = queue[head++];
      area += 1;
      const x = index % mask.width;
      const y = (index - x) / mask.width;
      for (const [dy, dx] of NEIGHBORS_8) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= mask.width || ny >= mask.height) continue;
        const next = ny * mask.width + nx;
        if (mask.data[next] === 0 || labels[next] !== 0) continue;
        labels[next] = count;
        queue[tail++] = next;
      }
    }
    areas.push(area);
    count += 1;
  }
  return { labels, areas, count };
}

/** Exact Euclidean distance (in cells) from every foreground pixel to the nearest background. */
function distanceTransform(mask: Mask) {
  const { width, height } = mask;
  const infinity = 1e20;
  const grid = new Float64Array(width * height);
  for (let index = 0; index < grid.length; index += 1) grid[index] = mask.data[index] ? infinity : 0;
  const transform1d = (values: Float64Array, length: number) => {
    const output = new Float64Array(length);
    const hulls = new Int32Array(length);
    const boundaries = new Float64Array(length + 1);
    let k = 0;
    hulls[0] = 0;
    boundaries[0] = -infinity;
    boundaries[1] = infinity;
    for (let q = 1; q < length; q += 1) {
      let s = ((values[q] + q * q) - (values[hulls[k]] + hulls[k] * hulls[k])) / (2 * q - 2 * hulls[k]);
      while (s <= boundaries[k]) {
        k -= 1;
        s = ((values[q] + q * q) - (values[hulls[k]] + hulls[k] * hulls[k])) / (2 * q - 2 * hulls[k]);
      }
      k += 1;
      hulls[k] = q;
      boundaries[k] = s;
      boundaries[k + 1] = infinity;
    }
    k = 0;
    for (let q = 0; q < length; q += 1) {
      while (boundaries[k + 1] < q) k += 1;
      output[q] = (q - hulls[k]) * (q - hulls[k]) + values[hulls[k]];
    }
    return output;
  };
  const column = new Float64Array(height);
  for (let x = 0; x < width; x += 1) {
    for (let y = 0; y < height; y += 1) column[y] = grid[y * width + x];
    const result = transform1d(column, height);
    for (let y = 0; y < height; y += 1) grid[y * width + x] = result[y];
  }
  const row = new Float64Array(width);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) row[x] = grid[y * width + x];
    const result = transform1d(row, width);
    for (let x = 0; x < width; x += 1) grid[y * width + x] = Math.sqrt(result[x]);
  }
  return grid;
}

// ------------------------------------------------------------------ contours (Suzuki-Abe)

type Contour = { points: Array<[number, number]>; hole: boolean; parent: number };

const DIRECTIONS: Array<[number, number]> = [
  [1, 0], [1, -1], [0, -1], [-1, -1], [-1, 0], [-1, 1], [0, 1], [1, 1],
];

/**
 * Outer borders and hole borders through foreground pixel centers (cv2.findContours with
 * RETR_CCOMP and CHAIN_APPROX_SIMPLE): each hole knows the outer border it belongs to.
 */
function findContours(mask: Mask): Contour[] {
  const width = mask.width + 2;
  const height = mask.height + 2;
  const image = new Int32Array(width * height);
  for (let y = 0; y < mask.height; y += 1) {
    for (let x = 0; x < mask.width; x += 1) {
      if (mask.data[y * mask.width + x]) image[(y + 1) * width + x + 1] = 1;
    }
  }
  const contours: Contour[] = [];
  // Border number NBD -> contour index; 1 is the frame.
  const borders = new Map<number, number>();
  let nbd = 1;
  const at = (x: number, y: number) => image[y * width + x];
  const direction = (from: [number, number], to: [number, number]) => (
    DIRECTIONS.findIndex(([dx, dy]) => from[0] + dx === to[0] && from[1] + dy === to[1])
  );
  for (let y = 1; y < height - 1; y += 1) {
    let lnbd = 1;
    for (let x = 1; x < width - 1; x += 1) {
      const value = at(x, y);
      let start: [number, number] | null = null;
      let hole = false;
      if (value === 1 && at(x - 1, y) === 0) {
        start = [x - 1, y];
      } else if (value >= 1 && at(x + 1, y) === 0) {
        start = [x + 1, y];
        hole = true;
        if (value > 1) lnbd = value;
      }
      if (start) {
        nbd += 1;
        const previous = borders.get(Math.abs(lnbd));
        const previousHole = previous === undefined ? true : contours[previous].hole;
        let parent = -1;
        if (hole) {
          // A hole belongs to the outer border around it.
          parent = previous === undefined ? -1 : previousHole ? contours[previous].parent : previous;
        }
        const points = followBorder(image, width, [x, y], start, nbd, at, direction);
        borders.set(nbd, contours.length);
        contours.push({ points: points.map(([px, py]) => [px - 1, py - 1]), hole, parent });
      }
      const current = at(x, y);
      if (current !== 0 && current !== 1) lnbd = current;
    }
  }
  return contours;
}

function followBorder(
  image: Int32Array,
  width: number,
  origin: [number, number],
  start: [number, number],
  nbd: number,
  at: (x: number, y: number) => number,
  direction: (from: [number, number], to: [number, number]) => number,
) {
  const points: Array<[number, number]> = [];
  // Step 3.1: clockwise from start for a nonzero pixel.
  let startDirection = direction(origin, start);
  let first: [number, number] | null = null;
  for (let step = 0; step < 8; step += 1) {
    const d = (startDirection + 8 - step) % 8;
    const candidate: [number, number] = [origin[0] + DIRECTIONS[d][0], origin[1] + DIRECTIONS[d][1]];
    if (at(candidate[0], candidate[1]) !== 0) {
      first = candidate;
      break;
    }
  }
  if (!first) {
    image[origin[1] * width + origin[0]] = -nbd;
    return [origin];
  }
  let previous = first;
  let current = origin;
  let lastDirection = -1;
  for (;;) {
    // Step 3.3: counterclockwise from previous for the next nonzero pixel.
    startDirection = direction(current, previous);
    let next: [number, number] = current;
    let rightIsZero = false;
    for (let step = 1; step <= 8; step += 1) {
      const d = (startDirection + step) % 8;
      const candidate: [number, number] = [current[0] + DIRECTIONS[d][0], current[1] + DIRECTIONS[d][1]];
      if (d === 0 && at(candidate[0], candidate[1]) === 0) rightIsZero = true;
      if (at(candidate[0], candidate[1]) !== 0) {
        next = candidate;
        break;
      }
    }
    // Step 3.4: mark the pixel.
    const index = current[1] * width + current[0];
    if (rightIsZero) image[index] = -nbd;
    else if (image[index] === 1) image[index] = nbd;
    // CHAIN_APPROX_SIMPLE: keep a point only where the chain turns.
    const move = direction(current, next);
    if (move !== lastDirection) points.push(current);
    lastDirection = move;
    // Step 3.5: back at the start going the same way.
    if (next[0] === origin[0] && next[1] === origin[1] && current[0] === first[0] && current[1] === first[1]) {
      break;
    }
    previous = current;
    current = next;
  }
  return points;
}

// ------------------------------------------------------------------ rasterize rooms

function rasterizeGeometries(geometries: RoomGeometry[], resolution: number) {
  const points = geometries.flatMap(geometryPoints);
  if (!points.length) throw new RoomGeometryError("Room geometry contains no points");
  const margin = resolution * 4;
  // Put the grid on the rooms' own lattice: User Maps from a SLAM map have corners on cell
  // centers (half a cell off whole multiples), and a grid half a cell away would pull every
  // edge inward by half a cell on the first split.
  const offset = (value: number) => ((value % resolution) + resolution) % resolution;
  const offsetX = offset(points[0][0]);
  const offsetY = offset(points[0][1]);
  const minimumX = Math.floor((Math.min(...points.map((p) => p[0])) - margin - offsetX) / resolution) * resolution + offsetX;
  const maximumX = Math.ceil((Math.max(...points.map((p) => p[0])) + margin - offsetX) / resolution) * resolution + offsetX;
  const minimumY = Math.floor((Math.min(...points.map((p) => p[1])) - margin - offsetY) / resolution) * resolution + offsetY;
  const maximumY = Math.ceil((Math.max(...points.map((p) => p[1])) + margin - offsetY) / resolution) * resolution + offsetY;
  const width = Math.ceil((maximumX - minimumX) / resolution) + 1;
  const height = Math.ceil((maximumY - minimumY) / resolution) + 1;
  if (width * height > MAX_PIXELS) throw new RoomGeometryError("Room is too large to split safely");
  const transform = new LocalTransform(minimumX, maximumY, resolution);
  const masks = geometries.map((geometry) => {
    const mask = new Mask(width, height);
    for (const polygon of geometryPolygons(geometry)) {
      fillPoly(mask, polygon[0].map((point) => transform.pixel(point)), 255);
      for (const hole of polygon.slice(1)) {
        const pixels = hole.map((point) => transform.pixel(point));
        fillPoly(mask, pixels, 0);
        // Keep the shared wall boundary: a vector -> mask -> vector round trip
        // must not grow every hole by one cell.
        polylines(mask, pixels, true, 255, 1);
      }
    }
    return mask;
  });
  return { masks, transform };
}

/** An interior point with the greatest wall clearance, and that clearance in meters. */
export function roomRepresentativePoint(geometry: RoomGeometry, resolution = 0.05): [Point, number] {
  if (!(resolution > 0)) throw new RoomGeometryError("resolution must be positive");
  const { masks: [mask], transform } = rasterizeGeometries([geometry], resolution);
  const distance = distanceTransform(mask);
  let best = 0;
  let bestIndex = -1;
  for (let index = 0; index < distance.length; index += 1) {
    if (distance[index] > best) {
      best = distance[index];
      bestIndex = index;
    }
  }
  if (bestIndex < 0) throw new RoomGeometryError("Room geometry contains no usable interior");
  const x = bestIndex % mask.width;
  return [transform.world(x, (bestIndex - x) / mask.width), round3(best * resolution)];
}

function maskGeometry(mask: Mask, transform: LocalTransform): RoomGeometry {
  const contours = findContours(mask);
  const ring = (points: Array<[number, number]>) => {
    if (points.length < 3) return null;
    const world = points.map(([x, y]) => transform.world(x, y));
    const [firstX, firstY] = world[0];
    const [lastX, lastY] = world[world.length - 1];
    if (firstX !== lastX || firstY !== lastY) world.push([firstX, firstY]);
    return world;
  };
  const polygons: PolygonCoordinates[] = [];
  contours.forEach((contour, index) => {
    if (contour.hole) return;
    const outer = ring(contour.points);
    if (!outer) return;
    const rings: PolygonCoordinates = [outer];
    for (const child of contours) {
      if (child.hole && child.parent === index) {
        const hole = ring(child.points);
        if (hole) rings.push(hole);
      }
    }
    polygons.push(rings);
  });
  if (!polygons.length) throw new RoomGeometryError("split result contains no usable polygon");
  return polygons.length === 1
    ? { type: "Polygon", coordinates: polygons[0] }
    : { type: "MultiPolygon", coordinates: polygons };
}

// ------------------------------------------------------------------ normalize

/** Validate a room and refresh its navigation metadata (area, representative point, clearance). */
export function normalizeRoomFeature(room: RoomFeature, resolution = 0.05): RoomFeature {
  if (!room || typeof room !== "object" || room.type !== "Feature") {
    throw new RoomGeometryError("every Room must be a GeoJSON Feature");
  }
  if (!room.properties || typeof room.properties !== "object" || room.properties.role !== "room") {
    throw new RoomGeometryError("every Room must have the room role");
  }
  const identity = room.id ?? room.properties.room_id;
  if (typeof identity !== "string" || !identity.trim()) {
    throw new RoomGeometryError("every Room must have a non-empty ID");
  }
  if (!room.geometry || typeof room.geometry !== "object") {
    throw new RoomGeometryError("every Room must contain geometry");
  }
  for (const polygon of geometryPolygons(room.geometry)) {
    if (!Array.isArray(polygon) || !polygon.length) {
      throw new RoomGeometryError("Room Polygon must contain an outer ring");
    }
    for (const ring of polygon) {
      if (!Array.isArray(ring) || ring.length < 4 ||
          ring[0]?.[0] !== ring[ring.length - 1]?.[0] || ring[0]?.[1] !== ring[ring.length - 1]?.[1]) {
        throw new RoomGeometryError("Room rings must be closed polygons");
      }
      if (ring.some((point) => !finitePoint(point))) {
        throw new RoomGeometryError("Room coordinates must be finite [x, y]");
      }
    }
  }
  const normalized = structuredClone(room);
  normalized.id = identity.trim();
  normalized.properties.room_id = identity.trim();
  const [representativePoint, clearance] = roomRepresentativePoint(room.geometry, resolution);
  Object.assign(normalized.properties, {
    area_m2: round2(geometryArea(room.geometry)),
    representative_point: representativePoint,
    clearance_m: clearance,
  });
  return normalized;
}

// ------------------------------------------------------------------ split

function cutRoom(roomMask: Mask, transform: LocalTransform, line: unknown, minimumRoomArea: number) {
  const validPoint = (point: unknown): point is Point => (
    Array.isArray(point) && point.length === 2 && point.every((value) => typeof value === "number" && Number.isFinite(value))
  );
  if (!Array.isArray(line) || !line.length) throw new RoomGeometryError("at least one split divider is required");
  const dividers = (validPoint(line[0]) ? [line] : line) as unknown[];
  if (dividers.some((divider) => !Array.isArray(divider) || divider.length < 2 || divider.some((point) => !validPoint(point)))) {
    throw new RoomGeometryError("each split divider must contain at least two finite points");
  }
  const { width, height } = roomMask;
  const eroded = erode3(roomMask);
  const boundary = (x: number, y: number) => roomMask.get(x, y) > 0 && eroded.get(x, y) === 0;
  const tolerance = Math.max(2, Math.ceil(WALL_SNAP_DISTANCE_METERS / transform.resolution));
  const cutMask = roomMask.clone();
  const thickness = Math.max(2, pyRound(0.08 / transform.resolution));
  for (const source of dividers as Point[][]) {
    const points = source.map((point) => transform.pixel(point));
    if (points.some(([x, y]) => !(x >= 0 && x < width && y >= 0 && y < height))) {
      throw new RoomGeometryError("split divider points must be near a Room wall");
    }
    const dividerPoints: Array<[number, number]> = points.map(([x, y], index) => {
      const endpoint = index === 0 || index === points.length - 1;
      if (!endpoint && roomMask.get(x, y) !== 0) return [x, y];
      let best: [number, number] | null = null;
      let bestDistance = Infinity;
      for (let cy = Math.max(0, y - tolerance); cy < Math.min(height, y + tolerance + 1); cy += 1) {
        for (let cx = Math.max(0, x - tolerance); cx < Math.min(width, x + tolerance + 1); cx += 1) {
          if (!boundary(cx, cy)) continue;
          const distance = (cx - x) ** 2 + (cy - y) ** 2;
          if (distance < bestDistance) {
            bestDistance = distance;
            best = [cx, cy];
          }
        }
      }
      if (!best) {
        throw new RoomGeometryError(endpoint
          ? "split divider endpoints must be near a Room wall"
          : "split divider control points must stay in the Room");
      }
      return best;
    });
    const segments = dividerPoints.slice(1).map((point, index) => [
      point[0] - dividerPoints[index][0], point[1] - dividerPoints[index][1],
    ] as const);
    const lengths = segments.map(([dx, dy]) => Math.hypot(dx, dy));
    if (lengths.some((length) => length < 2)) throw new RoomGeometryError("split divider segments are too short");
    const extension = thickness + 1;
    const [firstDx, firstDy] = segments[0];
    const [lastDx, lastDy] = segments[segments.length - 1];
    const start: [number, number] = [
      pyRound(dividerPoints[0][0] - firstDx / lengths[0] * extension),
      pyRound(dividerPoints[0][1] - firstDy / lengths[0] * extension),
    ];
    const last = dividerPoints[dividerPoints.length - 1];
    const end: [number, number] = [
      pyRound(last[0] + lastDx / lengths[lengths.length - 1] * extension),
      pyRound(last[1] + lastDy / lengths[lengths.length - 1] * extension),
    ];
    polylines(cutMask, [start, ...dividerPoints, end], false, 0, thickness);
  }
  const { labels, areas, count } = connectedComponents(cutMask);
  const minimumPixels = minimumRoomArea / transform.resolution ** 2;
  const components: number[] = [];
  for (let component = 1; component < count; component += 1) {
    if (areas[component] >= minimumPixels) components.push(component);
  }
  if (components.length !== 2) {
    throw new RoomGeometryError("the divider must cut the selected Room into exactly two meaningful areas");
  }
  const seeds = new Int32Array(labels.length);
  components.forEach((component, offset) => {
    for (let index = 0; index < labels.length; index += 1) {
      if (labels[index] === component) seeds[index] = offset + 1;
    }
  });
  return propagateLabels(roomMask, seeds);
}

/** Grow the two cut sides back over the divider, breadth first from the seeds. */
function propagateLabels(free: Mask, seeds: Int32Array) {
  const labels = seeds.slice();
  const queue = new Int32Array(labels.length);
  let head = 0;
  let tail = 0;
  for (let index = 0; index < labels.length; index += 1) if (labels[index] > 0) queue[tail++] = index;
  while (head < tail) {
    const index = queue[head++];
    const x = index % free.width;
    const y = (index - x) / free.width;
    for (const [dy, dx] of NEIGHBORS_8) {
      const nx = x + dx;
      const ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= free.width || ny >= free.height) continue;
      const next = ny * free.width + nx;
      if (free.data[next] === 0 || labels[next] !== 0) continue;
      labels[next] = labels[index];
      queue[tail++] = next;
    }
  }
  return labels;
}

function centroidOf(mask: (index: number) => boolean, width: number, length: number, transform: LocalTransform) {
  let sumX = 0;
  let sumY = 0;
  let count = 0;
  for (let index = 0; index < length; index += 1) {
    if (!mask(index)) continue;
    const x = index % width;
    sumX += x;
    sumY += (index - x) / width;
    count += 1;
  }
  return transform.world(pyRound(sumX / count), pyRound(sumY / count));
}

function splitFeature(source: RoomFeature, part: {
  geometry: RoomGeometry; suffix: string; color: string; area: number; centroid: Point;
  representativePoint: Point; clearance: number;
}, operationId: string, parentGeometry: RoomGeometry | null): RoomFeature {
  const sourceId = roomId(source);
  const id = `${sourceId}-${part.suffix}`;
  const properties: Record<string, unknown> = { ...source.properties };
  const baseName = properties.base_name ?? properties.name ?? sourceId;
  const parentPath = String(properties.split_path ?? "").trim();
  const childPath = [parentPath, part.suffix.toUpperCase()].filter(Boolean).join("-");
  for (const key of ["merged_from", "merged_from_names", "split_parent_geometry", "split_parent_properties"]) {
    delete properties[key];
  }
  Object.assign(properties, {
    room_id: id,
    name: `${baseName} ${childPath}`,
    base_name: baseName,
    split_path: childPath,
    color: part.color,
    area_m2: round2(part.area),
    centroid: part.centroid,
    representative_point: part.representativePoint,
    clearance_m: part.clearance,
    generated: false,
    edited: true,
    split_from: sourceId,
    split_operation_id: operationId,
    split_parent_id: sourceId,
    split_parent_name: source.properties.name ?? sourceId,
    split_parent_path: parentPath,
    split_parent_color: source.properties.color ?? SPLIT_COLORS[0],
  });
  if (parentGeometry) {
    properties.split_parent_geometry = structuredClone(parentGeometry);
    properties.split_parent_properties = structuredClone(source.properties);
  }
  return { type: "Feature", id, properties, geometry: part.geometry };
}

/** Split one room with wall-to-wall dividers (each a list of [x, y] points). */
export function splitRoomFeature(room: RoomFeature, line: unknown, resolution = 0.05, minimumRoomArea = 1): RoomFeature[] {
  if (room?.properties?.role !== "room") throw new RoomGeometryError("selected feature is not a Room");
  if (!(resolution > 0)) throw new RoomGeometryError("resolution must be positive");
  if (!(minimumRoomArea > 0)) throw new RoomGeometryError("minimum_room_area must be positive");
  const { masks: [roomMask], transform } = rasterizeGeometries([room.geometry], resolution);
  const labels = cutRoom(roomMask, transform, line, minimumRoomArea);
  const parts = [1, 2].map((label) => {
    let mask = new Mask(roomMask.width, roomMask.height);
    for (let index = 0; index < labels.length; index += 1) if (labels[index] === label) mask.data[index] = 255;
    if (label === 1) {
      // Adjacent raster regions meet between pixel centers: extending one side to the
      // neighboring centers gives both polygons the same shared boundary (no seam).
      const grown = dilate3(mask);
      mask = new Mask(roomMask.width, roomMask.height);
      for (let index = 0; index < labels.length; index += 1) {
        if (grown.data[index] > 0 && roomMask.data[index] > 0) mask.data[index] = 255;
      }
    }
    const geometry = maskGeometry(mask, transform);
    const centroid = centroidOf((index) => labels[index] === label, roomMask.width, labels.length, transform);
    const [representativePoint, clearance] = roomRepresentativePoint(geometry, resolution);
    return { geometry, area: geometryArea(geometry), centroid, representativePoint, clearance };
  });
  parts.sort((first, second) => first.centroid[0] - second.centroid[0] || first.centroid[1] - second.centroid[1]);
  const sourceColor = (room.properties.color as string | undefined) ?? SPLIT_COLORS[0];
  const colorIndex = [...String(room.id ?? "None")].reduce((sum, character) => sum + character.charCodeAt(0), 0);
  let nextColor = SPLIT_COLORS[(colorIndex + 1) % SPLIT_COLORS.length];
  if (nextColor === sourceColor) nextColor = SPLIT_COLORS[(colorIndex + 2) % SPLIT_COLORS.length];
  const operationId = createHash("sha1").update(stableJson({ id: room.id ?? null, geometry: room.geometry }))
    .digest("hex").slice(0, 12);
  return [
    splitFeature(room, { ...parts[0], suffix: "a", color: sourceColor }, operationId, room.geometry),
    splitFeature(room, { ...parts[1], suffix: "b", color: nextColor }, operationId, null),
  ];
}

// ------------------------------------------------------------------ merge

/** Merge exactly two adjacent rooms; two halves of one split get their original room back. */
export function mergeRoomFeatures(rooms: RoomFeature[], resolution = 0.05): RoomFeature {
  if (!Array.isArray(rooms) || rooms.length !== 2) throw new RoomGeometryError("exactly two Rooms are required for a merge");
  if (rooms.some((room) => room?.properties?.role !== "room")) {
    throw new RoomGeometryError("all selected features must be Rooms");
  }
  if (!(resolution > 0)) throw new RoomGeometryError("resolution must be positive");
  const sourceIds = rooms.map(roomId);
  if (sourceIds[0] === sourceIds[1]) throw new RoomGeometryError("two different Rooms are required for a merge");
  const { masks, transform } = rasterizeGeometries(rooms.map((room) => room.geometry), resolution);
  const combined = new Mask(masks[0].width, masks[0].height);
  for (let index = 0; index < combined.data.length; index += 1) {
    combined.data[index] = masks[0].data[index] || masks[1].data[index] ? 255 : 0;
  }
  if (connectedComponents(dilate3(combined)).count !== 2) {
    throw new RoomGeometryError("only adjacent Rooms can be merged");
  }
  const operationIds = new Set(rooms.map((room) => room.properties.split_operation_id ?? null));
  const parentGeometry = rooms.map((room) => room.properties.split_parent_geometry).find(Boolean) as RoomGeometry | undefined;
  const parentProperties = rooms.map((room) => room.properties.split_parent_properties).find(Boolean) as
    Record<string, unknown> | undefined;
  const restoresSplit = operationIds.size === 1 && !operationIds.has(null);
  const geometry = restoresSplit && parentGeometry
    ? structuredClone(parentGeometry)
    : maskGeometry(close3(combined), transform);
  const centroid = centroidOf((index) => combined.data[index] > 0, combined.width, combined.data.length, transform);
  const area = geometryArea(geometry);
  const [representativePoint, clearance] = roomRepresentativePoint(geometry, resolution);
  const id = restoresSplit
    ? String(rooms[0].properties.split_parent_id)
    : `room-merged-${createHash("sha1").update([...sourceIds].sort().join("|")).digest("hex").slice(0, 10)}`;
  const properties: Record<string, unknown> = restoresSplit && parentProperties
    ? structuredClone(parentProperties)
    : { ...rooms[0].properties };
  const baseNames = rooms.map((room, index) => room.properties.base_name ?? room.properties.name ?? sourceIds[index]);
  const sourceNames = rooms.map((room, index) => room.properties.name ?? sourceIds[index]);
  const categories = new Set(rooms.map((room) => room.properties.category ?? "unassigned"));
  let mergedName: unknown;
  let mergedBaseName: unknown;
  let parentColor: unknown;
  if (restoresSplit && parentProperties) {
    mergedName = properties.name ?? baseNames[0];
    mergedBaseName = properties.base_name ?? mergedName;
    parentColor = properties.color;
  } else {
    for (const key of ["split_from", "split_operation_id", "split_parent_id", "split_parent_geometry", "split_parent_properties"]) {
      delete properties[key];
    }
    const parentName = properties.split_parent_name;
    const parentPath = properties.split_parent_path ?? "";
    delete properties.split_parent_name;
    delete properties.split_parent_path;
    delete properties.split_path;
    parentColor = properties.split_parent_color;
    delete properties.split_parent_color;
    mergedName = restoresSplit && parentName ? parentName : baseNames[0];
    mergedBaseName = baseNames[0];
    if (restoresSplit && parentPath) properties.split_path = parentPath;
  }
  Object.assign(properties, {
    room_id: id,
    name: mergedName,
    base_name: mergedBaseName,
    area_m2: round2(area),
    centroid,
    representative_point: representativePoint,
    clearance_m: clearance,
    category: categories.size === 1 ? [...categories][0] : "unassigned",
    generated: false,
    edited: true,
    merged_from: sourceIds,
    merged_from_names: sourceNames,
  });
  if (restoresSplit && parentColor) properties.color = parentColor;
  return { type: "Feature", id, properties, geometry };
}
