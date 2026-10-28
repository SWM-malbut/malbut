"""Versioned occupancy-only edits; saving never changes the navigation map."""

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

import cv2
import numpy as np
import yaml


_LOCKS = {}
_LOCK_GUARD = threading.Lock()
_HELD = threading.local()
MAP_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.ya?ml')
REVISION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


@contextmanager
def map_lock(path):
    """Serialize cooperating readers/writers across threads and ROS processes."""
    path = Path(path).absolute()
    key = str(path)
    with _LOCK_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_HELD, 'paths', set())
        if key in held:
            yield
            return
        directory = path.parent / '.map-edit-locks'
        directory.mkdir(exist_ok=True)
        with (directory / (hashlib.sha256(key.encode()).hexdigest() + '.lock')).open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            _HELD.paths = held | {key}
            try:
                yield
            finally:
                _HELD.paths = held
                fcntl.flock(stream, fcntl.LOCK_UN)


def locked_map(function):
    @wraps(function)
    def wrapped(path, *args, **kwargs):
        with map_lock(path):
            return function(path, *args, **kwargs)
    return wrapped


def locked_edit(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with map_lock(self.path):
            return function(self, *args, **kwargs)
    return wrapped


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _store(path):
    return path.parent / '.map-edits' / path.name


def _identity(path):
    from .zones import map_identity
    return map_identity(path)


def _read(path):
    from .user_map import load_slam_map
    result = load_slam_map(path)
    if result.free_threshold <= 0:
        raise ValueError('빈 공간으로 바꿀 수 없는 지도 임계값입니다 (free_thresh > 0 필요).')
    if result.image.ndim != 2 or max(result.image.shape) > 8192:
        raise ValueError('편집 가능한 지도 크기를 초과했습니다.')
    return result


def occupancy(slam):
    probability = slam.image.astype(float) / 255.0
    if not slam.negate:
        probability = 1.0 - probability
    occupied = probability > slam.occupied_threshold
    free = probability < slam.free_threshold
    # Match the existing trinary reader: 205 is always unobserved.
    occupied &= slam.image != 205
    free &= slam.image != 205
    return occupied, free


def _polygons(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError('선택 영역은 1~64개여야 합니다.')
    result = []
    for ring in value:
        if not isinstance(ring, list) or not 3 <= len(ring) <= 64:
            raise ValueError('선택 영역은 3~64개의 꼭짓점이 필요합니다.')
        if any(not isinstance(p, list) or len(p) != 2 or any(
                type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 100000
                for v in p) for p in ring):
            raise ValueError('잘못된 지도 좌표입니다.')
        if ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3 or len(set(map(tuple, ring))) != len(ring):
            raise ValueError('영역의 꼭짓점이 중복됩니다.')
        area = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(ring, ring[1:]+ring[:1]))
        if abs(area) < 1e-10:
            raise ValueError('선택 영역의 면적이 없습니다.')
        def cross(a, b, c):
            return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        def intersects(a, b, c, d):
            if (max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0])
                    or max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1])):
                return False
            return cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0
        edges = list(zip(ring, ring[1:]+ring[:1]))
        for i, (a, b) in enumerate(edges):
            for j in range(i+1, len(edges)):
                if j == i+1 or (i == 0 and j == len(edges)-1):
                    continue
                if intersects(a, b, *edges[j]):
                    raise ValueError('영역의 선이 서로 교차합니다.')
        result.append(ring)
    return result


def erase(slam, polygons):
    """Rasterize cell centers in map coordinates, including polygon boundaries."""
    rings = _polygons(polygons)
    occupied, _ = occupancy(slam)
    rows, columns = np.nonzero(occupied)
    transform = slam.transform
    x = (columns + .5) * transform.resolution
    y = (slam.image.shape[0] - rows - .5) * transform.resolution
    cosine, sine = math.cos(transform.origin_yaw), math.sin(transform.origin_yaw)
    wx = transform.origin_x + cosine*x - sine*y
    wy = transform.origin_y + sine*x + cosine*y
    selected = np.zeros(len(rows), dtype=bool)
    for ring in rings:
        inside = np.zeros(len(rows), dtype=bool)
        boundary = inside.copy()
        for (ax, ay), (bx, by) in zip(ring, ring[1:]+ring[:1]):
            cross = (wx-ax)*(by-ay)-(wy-ay)*(bx-ax)
            boundary |= ((np.abs(cross) <= 1e-9) & (wx >= min(ax,bx)-1e-9)
                         & (wx <= max(ax,bx)+1e-9) & (wy >= min(ay,by)-1e-9)
                         & (wy <= max(ay,by)+1e-9))
            if ay != by:
                inside ^= ((ay > wy) != (by > wy)) & (wx < (bx-ax)*(wy-ay)/(by-ay)+ax)
        selected |= inside | boundary
    image = slam.image.copy()
    image[rows[selected], columns[selected]] = 0 if slam.negate else 255
    return image, int(selected.sum())


class MapEdits:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if not MAP_NAME.fullmatch(self.path.name):
            raise ValueError('저장 지도 이름이 올바르지 않습니다.')
        self.directory = _store(self.path)

    @locked_edit
    def state(self):
        recover(self.path)
        filename = self.directory / 'state.json'
        current = _identity(self.path)
        if filename.exists():
            state = json.loads(filename.read_text())
            if state['sourceIdentity'] != current:
                raise ValueError('지도 원본이 외부에서 교체됐습니다. 지도 정리를 다시 열어 주세요.')
            return state
        _read(self.path)  # Reject unsupported map modes before offering editing.
        revision = 'source-' + current[:32]
        return {'original': revision, 'latest': revision, 'applied': revision,
                'sourceIdentity': current, 'receipts': {}}

    @locked_edit
    def reset_external(self):
        """Start a new generation only on explicit open, keeping old backups."""
        recover(self.path)
        filename = self.directory / 'state.json'
        if filename.exists() and json.loads(filename.read_text())['sourceIdentity'] != _identity(self.path):
            archive = self.directory / 'history' / (uuid.uuid4().hex + '.json')
            archive.parent.mkdir(exist_ok=True)
            os.replace(filename, archive)

    def revision_path(self, revision):
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise ValueError('잘못된 지도 버전입니다.')
        return self.directory / 'versions' / (revision + '.yaml')

    def _snapshot(self, revision, image, metadata):
        path = self.revision_path(revision)
        height, width = image.shape
        atomic_bytes(path.with_suffix('.pgm'), f'P5\n{width} {height}\n255\n'.encode() + image.tobytes())
        atomic_bytes(path, yaml.safe_dump({**metadata, 'image': path.with_suffix('.pgm').name}).encode())

    def _initialize(self, state):
        if not (self.directory / 'state.json').exists():
            slam = _read(self.path)
            self._snapshot(state['original'], slam.image, yaml.safe_load(self.path.read_text()))
            _json(self.directory / 'state.json', state)

    @locked_edit
    def preview(self):
        state = self.state()
        path = self.revision_path(state['latest'])
        slam = _read(path if path.exists() else self.path)
        occupied, free = occupancy(slam)
        display = np.full(slam.image.shape, 205, dtype=np.uint8)
        display[free], display[occupied] = 254, 0
        ok, png = cv2.imencode('.png', display)
        if not ok or len(png) * 4 / 3 > 1500000:
            raise ValueError('지도 미리보기 크기를 초과했습니다.')
        t = slam.transform
        return {'map': self.path.name, 'revision': state['latest'],
                'originalRevision': state['original'], 'appliedRevision': state['applied'],
                'geometry': {'width': int(display.shape[1]), 'height': int(display.shape[0]),
                             'resolution': t.resolution, 'originX': t.origin_x,
                             'originY': t.origin_y, 'originYaw': t.origin_yaw},
                'previewBase64': base64.b64encode(png).decode()}

    @locked_edit
    def save(self, expected, polygons, command_id, restore=False):
        state = self.state()
        if command_id in state['receipts']:
            return state['receipts'][command_id]
        if expected != state['latest']:
            raise ValueError('다른 수정본이 저장됐습니다. 지도를 다시 불러와 주세요.')
        self._initialize(state)
        source = self.revision_path(state['original'] if restore else state['latest'])
        slam = _read(source)
        image, count = (slam.image, 0) if restore else erase(slam, polygons)
        revision = 'edit-' + uuid.uuid4().hex
        self._snapshot(revision, image, yaml.safe_load(source.read_text()))
        result = {'map': self.path.name, 'revision': revision, 'changedCells': count}
        state['latest'] = revision
        state['receipts'][command_id] = result
        _json(self.directory / 'state.json', state)
        return result

    @locked_edit
    def ticket(self, expected, revision, command_id):
        state = self.state()
        if expected != state['latest'] or revision != state['latest']:
            raise ValueError('저장된 수정본이 바뀌었습니다. 다시 불러와 주세요.')
        if not self.revision_path(revision).is_file():
            raise ValueError('먼저 수정본을 저장해 주세요.')
        if not re.fullmatch(r'[0-9a-f-]{36}', command_id):
            raise ValueError('잘못된 명령 ID입니다.')
        ticket = self.directory / 'requests' / (command_id + '.yaml')
        _json(ticket, {'map': self.path.name, 'expected': expected, 'revision': revision,
                       'sourceIdentity': state['sourceIdentity'], 'commandId': command_id})
        return ticket


def _backup(path):
    return {suffix: base64.b64encode(path.with_suffix(suffix).read_bytes()).decode()
            if path.with_suffix(suffix).exists() else None
            for suffix in (path.suffix, '.user-map.geojson', '.zones.geojson', '.view.json', '.pose.yaml')}


def _restore(path, files):
    for suffix, content in files.items():
        target = path.with_suffix(suffix)
        if content is None:
            target.unlink(missing_ok=True)
        else:
            atomic_bytes(target, base64.b64decode(content))


def recover(path):
    """An interrupted apply must recover before any map provider reads the YAML."""
    path = Path(path)
    journal = _store(path) / 'applying.json'
    if journal.exists():
        value = json.loads(journal.read_text())
        _restore(path, value['files'])
        _json(_store(path) / 'state.json', value['state'])
        journal.unlink()


def apply_ticket(ticket, activate):
    """Called under the runtime's idle/transition lock; activate must confirm readiness."""
    ticket = Path(ticket).resolve(strict=True)
    if ticket.parent.name != 'requests' or ticket.parents[2].name != '.map-edits':
        raise ValueError('잘못된 수정본 적용 경로입니다.')
    request = json.loads(ticket.read_text())
    path = ticket.parents[3] / request['map']
    if ticket.parents[1].name != path.name or not MAP_NAME.fullmatch(path.name):
        raise ValueError('잘못된 지도 적용 요청입니다.')
    with map_lock(path):
        edits = MapEdits(path)
        state = edits.state()
        if request['commandId'] in state['receipts']:
            return path
        if state['latest'] != request['expected'] or state['sourceIdentity'] != request['sourceIdentity']:
            raise ValueError('지도가 변경돼 수정본을 적용할 수 없습니다.')
        from .user_map import load_or_build_user_map, build_user_map, _write_user_map
        from .zones import read_zones, write_zones
        files = _backup(path)
        journal = edits.directory / 'applying.json'
        previous_state = deepcopy(state)
        _json(journal, {'files': files, 'state': previous_state})
        try:
            old_user = load_or_build_user_map(path, 'real-' + hashlib.sha256(path.name.encode()).hexdigest()[:24])
            zones = read_zones(path)
            revision_path = edits.revision_path(request['revision'])
            metadata = yaml.safe_load(revision_path.read_text())
            metadata['image'] = os.path.relpath(revision_path.with_suffix('.pgm'), path.parent)
            atomic_bytes(path, yaml.safe_dump(metadata).encode())
            slam = _read(path)
            updated = build_user_map(slam)
            updated['map_id'] = old_user['map_id']
            updated['room_segmentation'] = deepcopy(old_user.get('room_segmentation', {}))
            updated['features'] = [f for f in updated['features'] if f.get('properties', {}).get('role') in
                                   ('walkable_area', 'wall_outline')] + [deepcopy(f) for f in old_user['features']
                                   if f.get('properties', {}).get('role') not in ('walkable_area', 'wall_outline')]
            _write_user_map(path, updated)
            write_zones(path, zones)
            path.with_suffix('.view.json').unlink(missing_ok=True)
            # The pose geometry still fits, but its image fingerprint changed.
            pose_file = path.with_suffix('.pose.yaml')
            if pose_file.exists():
                from malbut_relocalization.pose_store import map_identity
                pose = yaml.safe_load(pose_file.read_text())
                pose['map_id'] = map_identity(str(path))
                atomic_bytes(pose_file, yaml.safe_dump(pose).encode())
            activate(str(path))
            state['applied'] = request['revision']
            state['sourceIdentity'] = _identity(path)
            state['receipts'][request['commandId']] = {'applied': request['revision']}
            _json(edits.directory / 'state.json', state)
            journal.unlink()
            return path
        except Exception:
            _restore(path, files)
            _json(edits.directory / 'state.json', previous_state)
            journal.unlink(missing_ok=True)
            raise
