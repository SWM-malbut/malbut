"""Bounded, asynchronous JPEG evidence capture; never call a model from patrol."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
import time
from uuid import uuid4

import cv2
import numpy as np

from malbut_patrol.spatial_capture import encode_spatial, room_snapshot


DEFAULT_DIRECTORY = '~/.local/share/malbut/patrol-observations'


def encode_image(message):
    """Encode supported ROS byte images, respecting padded rows and RGB order."""
    channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}
    count = channels.get(message.encoding)
    width, height, step = message.width, message.height, message.step
    if (count is None or not 0 < width <= 8192 or not 0 < height <= 8192
            or width * height > 16000000 or step < width * count
            or len(message.data) != step * height):
        raise ValueError('unsupported or invalid camera image')
    frame = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(height, step)
    frame = frame[:, :width * count].reshape(height, width, count)
    conversion = {'rgb8': cv2.COLOR_RGB2BGR, 'rgba8': cv2.COLOR_RGBA2BGR,
                  'bgra8': cv2.COLOR_BGRA2BGR}
    if message.encoding in conversion:
        frame = cv2.cvtColor(frame, conversion[message.encoding])
    if max(width, height) > 1280:
        scale = 1280 / max(width, height)
        frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok or len(encoded) > 4 * 1024 * 1024:
        raise ValueError('camera JPEG encoding failed')
    return encoded.tobytes()


def _atomic_write(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PhotoRecorder:
    """Sample distinct views and publish a manifest only after its photo is durable."""

    def __init__(self, directory, *, max_photos=None, max_bytes=512 * 1024 * 1024,
                 inspection=False):
        self.directory = Path(directory).expanduser()
        self.inspection = inspection
        self.max_photos = (160 if inspection else 24) if max_photos is None else max_photos
        self.max_bytes = max_bytes
        self._queue = Queue(maxsize=4)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._stats = {}
        self._poses = []
        self._last_capture = 0.0
        self._run = None
        self._batches = {}
        self._writer_owned = False
        self._thread = threading.Thread(target=self._work, name='patrol-photo-writer', daemon=True)
        self._thread.start()

    def begin(self, grid, frame, room_map_file, rooms=None):
        """Start a new observation run with a stable map identity."""
        geometry = [grid.width, grid.height, grid.resolution, grid.origin_x,
                    grid.origin_y, grid.origin_yaw]
        digest = hashlib.sha256(json.dumps(geometry).encode() + grid.cells.tobytes()).hexdigest()
        with self._lock:
            self.finish('interrupted')
            self._run = {'patrol_id': uuid4().hex, 'map_id': digest, 'map_frame': frame,
                         'room_map': Path(room_map_file).name if room_map_file else '',
                         '_regions': room_snapshot(rooms)}
            if not self.inspection:
                self._run['analysis_batch'] = 'patrol_mosaic_v1'
            self._stats = {'queued': 0, 'saved': 0, 'failed': 0, 'last_error': '',
                           'batch_ready': False}
            self._batches[self._run['patrol_id']] = {
                'stats': self._stats, 'captures': [], 'outcome': None,
            }
            self._poses = []
            self._last_capture = 0.0

    def offer(self, image, *, x, y, yaw, room_names, phase, simulated=False, spatial=None,
              inspection=None):
        """Queue a fresh frame already matched to its TF; return without disk I/O."""
        with self._lock:
            if (self._stop.is_set() or self._run is None
                    or self._batches.get(self._run['patrol_id'], {}).get('outcome') is not None
                    or self._run['patrol_id'] not in self._batches):
                return False
            # Capture the initial view even if the map is already covered.
            if self.inspection and phase not in ('PLANNING', 'NAVIGATING', 'OBSERVING'):
                return False
            if not self.inspection and self._poses and phase != 'OBSERVING':
                return False
            if self.inspection and inspection is None:
                raise ValueError('Inspection capture requires camera calibration evidence')
            now = time.monotonic()
            interval = 0.25 if self.inspection else 1.5
            angle = inspection['hfov_rad'] / 2 if self.inspection else math.pi / 3
            spacing = 0.25 if self.inspection else 0.6
            if self._stats['queued'] >= self.max_photos or now - self._last_capture < interval:
                return False
            if any(math.hypot(x - px, y - py) < spacing
                   and abs(math.atan2(math.sin(yaw - pa), math.cos(yaw - pa))) < angle
                   for px, py, pa in self._poses):
                return False
            captured = (datetime.now(timezone.utc) if simulated else datetime.fromtimestamp(
                image.header.stamp.sec + image.header.stamp.nanosec / 1e9, timezone.utc))
            metadata = dict(self._run, version=2, observation_id=uuid4().hex,
                            captured_at=captured.isoformat(),
                            image_stamp={'sec': image.header.stamp.sec,
                                         'nanosec': image.header.stamp.nanosec},
                            camera_frame=image.header.frame_id, simulated=simulated,
                            camera_pose={'x': x, 'y': y, 'yaw': yaw},
                            capture_rooms=list(room_names), _spatial=spatial)
            if self.inspection:
                metadata['inspection'] = inspection
            try:
                self._queue.put_nowait((image, metadata, self._stats))
            except Full:
                return False
            self._stats['queued'] += 1
            self._poses.append((x, y, yaw))
            self._last_capture = now
            return True

    def status(self):
        """Report accepted versus durably written evidence, never inferred success."""
        with self._lock:
            return {'patrol_id': self._run['patrol_id'] if self._run else None, **self._stats}

    def evidence(self):
        """Return this producer's run identity and immutable committed-photo hashes."""
        with self._lock:
            run = {k: self._run[k] for k in ('patrol_id', 'map_id', 'map_frame')}
            captures = self._batches.get(run['patrol_id'], {}).get('captures', [])
            return run, [dict(item) for item in captures]

    def finish(self, outcome='completed'):
        """Seal this patrol without waiting for its accepted disk writes."""
        with self._lock:
            batch = self._batches.get(self._run['patrol_id']) if self._run else None
            if batch is not None and batch['outcome'] is None:
                batch['outcome'] = outcome

    def _flush_batches(self):
        """Publish a complete batch only after every accepted photo has settled."""
        if not self._writer_owned:
            return
        with self._lock:
            ready = [(identity, batch) for identity, batch in self._batches.items()
                     if batch['outcome'] is not None and batch['stats']['queued']
                     == batch['stats']['saved'] + batch['stats']['failed']]
        for identity, batch in ready:
            try:
                folder = self.directory / 'runs'
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                manifest = {'version': 1, 'patrol_id': identity,
                            'outcome': batch['outcome'], 'captures': batch['captures'],
                            'capture_failures': batch['stats']['failed']}
                _atomic_write(folder / (identity + '.json'),
                              json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
            except OSError:
                with self._lock:
                    batch['stats']['last_error'] = 'batch_manifest_failed'
                continue
            with self._lock:
                batch['stats']['batch_ready'] = True
                del self._batches[identity]

    def _save(self, image, metadata):
        folder = self.directory / 'captures'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = encode_image(image)
        metadata = dict(metadata)
        regions = metadata.pop('_regions')
        spatial, depth = encode_spatial(image, metadata.pop('_spatial'))
        metadata['spatial'] = spatial
        metadata['regions_sha256'] = hashlib.sha256(regions).hexdigest()
        metadata['capture_regions'] = [
            {'id': room['id'], 'name': room['name']}
            for room in json.loads(regions)['regions']
            if room['name'] in metadata['capture_rooms']]
        used = sum(path.stat().st_size for path in folder.iterdir() if path.is_file())
        if used + len(data) + len(regions) + len(depth or b'') + 16384 > self.max_bytes:
            raise OSError('photo storage limit reached')
        identifier = metadata['observation_id']
        metadata = dict(metadata, image_sha256=hashlib.sha256(data).hexdigest())
        files = {'.jpg': data, '.regions.json': regions}
        if depth is not None:
            files['.depth.npy'] = depth
            spatial['depth_sha256'] = hashlib.sha256(depth).hexdigest()
        try:
            for suffix, contents in files.items():
                _atomic_write(folder / (identifier + suffix), contents)
            _atomic_write(folder / (identifier + '.json'),
                          json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
        except Exception:
            for suffix in files:
                (folder / (identifier + suffix)).unlink(missing_ok=True)
            raise

    def _work(self):
        """One producer owns this directory; restart seals its interrupted runs."""
        lock = None
        try:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = (self.directory / 'producer.lock').open('a')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._writer_owned = True
                self._recover_batches()
            except OSError:
                self._writer_owned = False
            self._write_photos()
        finally:
            if lock is not None:
                lock.close()

    def _recover_batches(self):
        """After a producer crash, retain only the photos whose manifests were committed."""
        runs = {}
        for path in (self.directory / 'captures').glob('*.json'):
            if not re.fullmatch('[a-f0-9]{32}', path.stem):
                continue
            try:
                if path.is_symlink() or path.stat().st_size > 16384:
                    continue
                data = path.read_bytes()
                metadata = json.loads(data)
                identity = metadata.get('patrol_id', '')
                if (metadata.get('analysis_batch') != 'patrol_mosaic_v1'
                        or metadata.get('observation_id') != path.stem
                        or not isinstance(identity, str)
                        or not re.fullmatch('[a-f0-9]{32}', identity)
                        or (self.directory / 'runs' / (identity + '.json')).exists()):
                    continue
                runs.setdefault(identity, []).append({
                    'observation_id': path.stem,
                    'manifest_sha256': hashlib.sha256(data).hexdigest(),
                })
            except (OSError, ValueError, AttributeError):
                continue
        folder = self.directory / 'runs'
        for identity, captures in runs.items():
            if len(captures) > 24:
                continue
            folder.mkdir(exist_ok=True, mode=0o700)
            # Exclusive producer ownership makes an earlier process's temporary
            # completion file stale; do not strand the recovered run on it.
            (folder / (identity + '.json.tmp')).unlink(missing_ok=True)
            _atomic_write(folder / (identity + '.json'), json.dumps({
                'version': 1, 'patrol_id': identity, 'outcome': 'interrupted',
                'captures': captures, 'capture_failures': None,
            }).encode('utf-8'))

    def _write_photos(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                image, metadata, stats = self._queue.get(timeout=0.2)
            except Empty:
                self._flush_batches()
                continue
            try:
                if not self._writer_owned:
                    raise OSError('photo directory producer unavailable')
                self._save(image, metadata)
                path = self.directory / 'captures' / (metadata['observation_id'] + '.json')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                with self._lock:
                    stats['failed'] += 1
                    stats['last_error'] = 'capture_failed'
            else:
                with self._lock:
                    stats['saved'] += 1
                    self._batches[metadata['patrol_id']]['captures'].append({
                        'observation_id': metadata['observation_id'], 'manifest_sha256': digest,
                    })
            finally:
                self._queue.task_done()
            self._flush_batches()
        self._flush_batches()

    def close(self):
        """Drain accepted photos after navigation has settled on shutdown."""
        self.finish('shutdown')
        self._stop.set()
        self._thread.join(timeout=5.0)
