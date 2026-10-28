export type MapPoint = [number, number];
export type EditGeometry = {
  width: number; height: number; resolution: number;
  originX: number; originY: number; originYaw: number;
};
export type SavedMapEdit = {
  map: string; revision: string; originalRevision: string; appliedRevision: string;
  geometry: EditGeometry; previewBase64: string;
};

/** Image coordinates use the top-left edge; map coordinates use the saved ROS origin. */
export function pixelToMap([x, y]: MapPoint, g: EditGeometry): MapPoint {
  const lx = x * g.resolution, ly = (g.height - y) * g.resolution;
  const c = Math.cos(g.originYaw), s = Math.sin(g.originYaw);
  return [g.originX + c * lx - s * ly, g.originY + s * lx + c * ly];
}

export function polygonError(ring: MapPoint[]): string | null {
  if (ring.length < 3) return "꼭짓점을 3개 이상 선택해 주세요.";
  if (ring.length > 64 || new Set(ring.map((p) => p.join(","))).size !== ring.length) return "꼭짓점을 확인해 주세요.";
  const edges = ring.map((p, i) => [p, ring[(i + 1) % ring.length]]);
  const cross = (a: MapPoint, b: MapPoint, c: MapPoint) => (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
  const area = edges.reduce((sum, [a, b]) => sum + a[0]*b[1]-b[0]*a[1], 0);
  if (Math.abs(area) < 1e-10) return "선택 영역의 면적이 없습니다.";
  for (let i = 0; i < edges.length; i++) for (let j = i + 1; j < edges.length; j++) {
    if (j === i+1 || (i === 0 && j === edges.length-1)) continue;
    const [a,b] = edges[i], [c,d] = edges[j];
    if (Math.max(a[0],b[0]) < Math.min(c[0],d[0]) || Math.max(c[0],d[0]) < Math.min(a[0],b[0]) ||
        Math.max(a[1],b[1]) < Math.min(c[1],d[1]) || Math.max(c[1],d[1]) < Math.min(a[1],b[1])) continue;
    if (cross(a,b,c)*cross(a,b,d) <= 0 && cross(c,d,a)*cross(c,d,b) <= 0) return "영역의 선이 서로 교차합니다.";
  }
  return null;
}

export function containsCell(x: number, y: number, ring: MapPoint[]): boolean {
  let inside = false;
  for (let i = 0; i < ring.length; i++) {
    const [ax,ay] = ring[i], [bx,by] = ring[(i+1)%ring.length];
    if (Math.abs((x-ax)*(by-ay)-(y-ay)*(bx-ax)) <= 1e-9 &&
        x >= Math.min(ax,bx)-1e-9 && x <= Math.max(ax,bx)+1e-9 &&
        y >= Math.min(ay,by)-1e-9 && y <= Math.max(ay,by)+1e-9) return true;
    if ((ay > y) !== (by > y) && x < (bx-ax)*(y-ay)/(by-ay)+ax) inside = !inside;
  }
  return inside;
}

/** The robot sends a classified PNG (0 occupied, 205 unknown, 254 free). */
export function editedPixels(source: Uint8ClampedArray, width: number, height: number, rings: MapPoint[][]) {
  const pixels = new Uint8ClampedArray(source);
  let count = 0;
  for (const ring of rings) {
    const x0 = Math.max(0, Math.floor(Math.min(...ring.map(([x]) => x))));
    const x1 = Math.min(width - 1, Math.floor(Math.max(...ring.map(([x]) => x))));
    const y0 = Math.max(0, Math.floor(Math.min(...ring.map(([,y]) => y))));
    const y1 = Math.min(height - 1, Math.floor(Math.max(...ring.map(([,y]) => y))));
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const i = (y * width + x) * 4;
      if (pixels[i] !== 0 || source[i] !== 0 || !containsCell(x+.5, y+.5, ring)) continue;
      pixels[i] = 67; pixels[i+1] = 180; pixels[i+2] = 153;
      count++;
    }
  }
  return { pixels, count };
}
