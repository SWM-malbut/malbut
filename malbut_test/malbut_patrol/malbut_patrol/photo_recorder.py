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

from malbut_patrol.spatial_capture import (
    camera_calibration, encode_lidar, encode_spatial, room_snapshot,
)
from malbut_patrol.transient_media import RETENTION_SECONDS, TransientMedia


DEFAULT_DIRECTORY = '~/.local/share/malbut/patrol-observations'


class _CaptureDiscarded(Exception):
    """An accepted capture was abandoned before its handoff could be published."""


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
    """Keep pixels in bounded RAM; persist only capture metadata for the Agent."""

    def __init__(self, directory, *, max_photos=24, max_bytes=512 * 1024 * 1024,
                 inspection=False):
        self.directory = Path(directory).expanduser()
        self.inspection = inspection
        self.max_photos = max_photos
        self.max_bytes = max_bytes
        self._queue = Queue(maxsize=4)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._commit_lock = threading.RLock()
        self._stats = {}
        self._poses = []
        self._last_capture = 0.0
        self._run = None
        self._batches = {}
        self._analysis_held = False
        self._writer_owned = False
        self._media = None
        self._thread = threading.Thread(target=self._work, name='patrol-photo-writer', daemon=True)
        self._thread.start()

    def begin(self, grid, frame, room_map_file, rooms=None, *, reobserve_of=None,
              reobserve_round=0, capture_limit=None, inspection_of=None):
        """Start a new observation run with a stable map identity."""
        geometry = [grid.width, grid.height, grid.resolution, grid.origin_x,
                    grid.origin_y, grid.origin_yaw]
        digest = hashlib.sha256(json.dumps(geometry).encode() + grid.cells.tobytes()).hexdigest()
        with self._lock:
            self.finish('interrupted')
            self._run = {'patrol_id': uuid4().hex, 'map_id': digest, 'map_frame': frame,
                         'room_map': Path(room_map_file).name if room_map_file else '',
                         'analysis_batch': 'patrol_images_v1',
                         '_regions': room_snapshot(rooms, grid)}
            if reobserve_of is not None:
                self._run.update(reobserve_of=reobserve_of, reobserve_round=reobserve_round)
            if inspection_of is not None:
                self._run['inspection_of'] = inspection_of
            self._limit = min(self.max_photos, capture_limit or self.max_photos)
            self._stats = {'queued': 0, 'saved': 0, 'failed': 0, 'discarded': 0, 'last_error': '',
                           'batch_ready': False}
            self._batches[self._run['patrol_id']] = {
                'stats': self._stats, 'captures': [], 'outcome': None,
                'accepted': [], 'analysis_held': self._analysis_held, 'discarded': False,
            }
            self._references = self._batches[self._run['patrol_id']]['captures']
            self._poses = []
            self._last_capture = 0.0

    def offer(self, image, *, x, y, yaw, room_names, phase, simulated=False, spatial=None,
              lidar=None, reobserve_target=None, inspection=None):
        """Queue a fresh frame already matched to its TF; return without disk I/O."""
        with self._lock:
            if (self._stop.is_set() or self._run is None
                    or self._batches.get(self._run['patrol_id'], {}).get('outcome') is not None
                    or self._run['patrol_id'] not in self._batches):
                return False
            # Capture the initial view even if the map is already covered.
            if self._run.get('reobserve_of'):
                if phase != 'REOBSERVING' or reobserve_target is None:
                    return False
            elif self.inspection and phase not in ('PLANNING', 'NAVIGATING', 'OBSERVING'):
                return False
            elif not self.inspection and self._poses and phase != 'OBSERVING':
                return False
            if self.inspection and inspection is None:
                raise ValueError('Inspection capture requires camera calibration evidence')
            now = time.monotonic()
            interval = .25 if self.inspection else 1.5
            angle = inspection['hfov_rad']/2 if self.inspection else math.pi/3
            spacing = .25 if self.inspection else .6
            if (self._stats['queued'] >= min(self._limit, self.max_photos)
                    or now - self._last_capture < interval):
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
                            capture_rooms=list(room_names), _spatial=spatial, _lidar=lidar)
            if reobserve_target is not None:
                metadata['reobserve_target'] = dict(reobserve_target)
            if self.inspection:
                metadata['inspection'] = inspection
            try:
                self._queue.put_nowait((image, metadata, self._stats))
            except Full:
                return False
            self._stats['queued'] += 1
            self._batches[self._run['patrol_id']]['accepted'].append(metadata['observation_id'])
            self._poses.append((x, y, yaw))
            self._last_capture = now
            return True

    def status(self):
        """Report accepted versus RAM-ready captures, never inferred analysis success."""
        with self._lock:
            return {'patrol_id': self._run['patrol_id'] if self._run else None, **self._stats,
                    'analysis_held': self._analysis_held,
                    'analysis_pending_batches': sum(
                        batch['analysis_held'] for batch in self._batches.values())}

    def identity(self):
        """Expose the current run identity without mutable sensor metadata."""
        with self._lock:
            return {k: self._run[k] for k in ('patrol_id', 'map_id')} if self._run else None

    def context(self):
        """Identify the exact map and room boundaries used for this run."""
        with self._lock:
            if self._run is None:
                return None
            return {**self.identity(), 'map_frame': self._run['map_frame'],
                    'regions_sha256': hashlib.sha256(self._run['_regions']).hexdigest()}

    def evidence(self):
        """Keep producer references available after the batch has been sealed and flushed."""
        with self._lock:
            run = {k: self._run[k] for k in ('patrol_id', 'map_id', 'map_frame')}
            return run, [dict(item) for item in self._references]

    def finish(self, outcome='completed'):
        """Seal this patrol without waiting for its accepted encodes."""
        with self._lock:
            batch = self._batches.get(self._run['patrol_id']) if self._run else None
            if batch is not None and batch['outcome'] is None:
                batch['outcome'] = outcome

    def hold_analysis(self):
        """Keep new batches private until the manager confirms movement has stopped."""
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError('photo recorder is closed')
            self._analysis_held = True

    def release_analysis(self, outcome='completed'):
        """Allow held batches to publish once their accepted encodes have settled."""
        with self._lock:
            self._analysis_held = False
            for batch in self._batches.values():
                if batch['analysis_held']:
                    batch['analysis_held'] = False
                    if batch['outcome'] is None:
                        batch['outcome'] = outcome

    def discard_held_analysis(self):
        """Drop unapproved batches, including queued encodes and restart metadata."""
        with self._commit_lock:
            discarded = []
            with self._lock:
                self._analysis_held = False
                for batch in self._batches.values():
                    if not batch['analysis_held']:
                        continue
                    batch['analysis_held'] = False
                    batch['discarded'] = True
                    batch['outcome'] = 'discarded'
                    discarded.extend(batch['accepted'])
                    batch['captures'].clear()
                    batch['stats']['discarded'] += batch['stats']['saved']
                    batch['stats']['saved'] = 0
            for identifier in discarded:
                if self._media is not None:
                    self._media.discard(identifier)
                (self.directory / 'captures' / (identifier + '.json')).unlink(missing_ok=True)

    def _flush_batches(self):
        """Publish a complete batch only after every accepted photo has settled."""
        if not self._writer_owned:
            return
        # Disk commits and discard serialize independently from the capture lock,
        # so slow fsync never stalls offer() on the navigation thread.
        with self._commit_lock:
            with self._lock:
                ready = [(identity, batch) for identity, batch in self._batches.items()
                         if not batch['analysis_held'] and batch['outcome'] is not None
                         and batch['stats']['queued'] == batch['stats']['saved']
                         + batch['stats']['failed'] + batch['stats']['discarded']]
            for identity, batch in ready:
                if batch['discarded']:
                    with self._lock:
                        del self._batches[identity]
                    continue
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
        bundle = metadata.pop('_spatial')
        spatial, depth = encode_spatial(image, bundle)
        if bundle:
            try:
                intrinsics, _ = camera_calibration(
                    bundle['info'], bundle.get('image_rectified', False))
                metadata['camera_hfov_rad'] = 2*math.atan2(image.width, 2*intrinsics['fx'])
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        lidar, scan = encode_lidar(image, metadata.pop('_lidar', None))
        if lidar is not None:
            metadata['lidar'] = lidar
        metadata['spatial'] = spatial
        metadata['regions_sha256'] = hashlib.sha256(regions).hexdigest()
        metadata['capture_regions'] = [
            {'id': room['id'], 'name': room['name']}
            for room in json.loads(regions)['regions']
            if room['name'] in metadata['capture_rooms']]
        identifier = metadata['observation_id']
        metadata = dict(metadata, image_sha256=hashlib.sha256(data).hexdigest(),
                        media_storage='volatile_v1',
                        media_expires_at=time.time() + RETENTION_SECONDS)
        files = {'.jpg': data, '.regions.json': regions}
        if scan is not None:
            files['.lidar.npy'] = scan
            lidar['sha256'] = hashlib.sha256(scan).hexdigest()
        if depth is not None:
            files['.depth.npy'] = depth
            spatial['depth_sha256'] = hashlib.sha256(depth).hexdigest()
        with self._commit_lock:
            with self._lock:
                if self._batches[metadata['patrol_id']]['discarded']:
                    raise _CaptureDiscarded()
            try:
                self._media.put(identifier, files)
                _atomic_write(folder / (identifier + '.json'),
                              json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
            except Exception:
                self._media.discard(identifier)
                raise

    def _work(self):
        """One producer owns this directory; restart seals its interrupted runs."""
        lock = None
        try:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = (self.directory / 'producer.lock').open('a')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._media = TransientMedia(self.directory, min(self.max_bytes, 128*1024*1024))
                self._writer_owned = True
                self._recover_batches()
            except OSError:
                self._writer_owned = False
            self._write_photos()
        finally:
            if self._media is not None:
                self._media.close()
            if lock is not None:
                lock.close()

    def _recover_batches(self):
        """Seal interrupted manifests; expired or lost RAM pixels cannot be recovered."""
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
                if (metadata.get('analysis_batch') not in ('patrol_images_v1', 'patrol_mosaic_v1')
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
                with self._lock:
                    if self._batches[metadata['patrol_id']]['discarded']:
                        raise _CaptureDiscarded()
                self._save(image, metadata)
                with self._commit_lock:
                    with self._lock:
                        batch = self._batches[metadata['patrol_id']]
                        if batch['discarded']:
                            raise _CaptureDiscarded()
                    path = self.directory / 'captures' / (metadata['observation_id'] + '.json')
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    with self._lock:
                        stats['saved'] += 1
                        batch['captures'].append({
                            'observation_id': metadata['observation_id'],
                            'manifest_sha256': digest,
                        })
            except _CaptureDiscarded:
                with self._lock:
                    stats['discarded'] += 1
            except Exception:
                with self._lock:
                    stats['failed'] += 1
                    stats['last_error'] = 'capture_failed'
            finally:
                self._queue.task_done()
                # An idle writer must not retain the last RGB/depth message outside
                # the bounded transient store after analysis or expiry.
                image = metadata = stats = None
            self._flush_batches()
        self._flush_batches()

    def close(self):
        """Drain released captures; never authorize held analysis during shutdown."""
        self.discard_held_analysis()
        with self._lock:
            self.finish('shutdown')
            self._stop.set()
        self._thread.join(timeout=5.0)
