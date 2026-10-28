"""
Apply empirical inspection limits and credit only completed photo evidence.

The resulting coverage remains a planar visibility proxy. A successful image
analysis does not prove that every object or every 3D surface was recognized.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class InspectionCalibration:
    """Keep an explicitly selected model/camera/environment calibration."""

    range_m: float
    model: str
    width: int
    height: int
    fov_rad: float
    simulated: bool
    supported_classes: tuple = ()
    unsupported_classes: tuple = ()
    calibration_kind: str = 'unspecified'
    recognition_validated: bool = False

    @classmethod
    def load(cls, path, *, simulated, width, height, fov_rad, camera_height_m=None):
        """Refuse applying a different camera or simulated result to real hardware."""
        source = Path(path).expanduser()
        if source.stat().st_size > 65536:
            raise ValueError('inspection calibration too large')
        value = json.loads(source.read_text(encoding='utf-8'))
        if value.get('version') != 1 or value.get('validated') is not True:
            raise ValueError('inspection calibration must contain validated measurements')
        camera = value['camera']
        result = cls(float(value['range_m']), value['model'], camera['width'],
                     camera['height'], float(camera['hfov_rad']), value['simulated'],
                     tuple(value.get('supported_classes', ())),
                     tuple(value.get('unsupported_classes', ())),
                     value.get('calibration_kind', 'unspecified'),
                     value.get('recognition_validated') is True)
        if 'height_m' in camera and (
                camera_height_m is None
                or not math.isfinite(float(camera['height_m']))
                or not math.isfinite(camera_height_m)
                or abs(float(camera['height_m'])-camera_height_m) > .025):
            raise ValueError('inspection calibration does not match the camera mounting height')
        if (not math.isfinite(result.range_m) or result.range_m <= 0
                or not isinstance(result.model, str) or not result.model.strip()
                or type(result.simulated) is not bool
                or not math.isfinite(result.fov_rad)
                or not 0 < result.fov_rad < math.pi
                or (result.width, result.height) != (width, height)
                or abs(result.fov_rad-fov_rad) > .02
                or result.simulated != simulated):
            raise ValueError('inspection calibration does not match this camera/environment')
        return result


class PhotoCoverage:
    """Read completed evidence without performing inference or creating a DB."""

    def __init__(self, directory, source, calibration=None):
        """Initialize one run's evidence ledger; callers provide only its captures."""
        if source not in ('saved_photos', 'analyzed_photos'):
            raise ValueError('unsupported photo coverage source')
        self.directory = Path(directory).expanduser()
        self.source = source
        self.calibration = calibration
        self._credited = set()
        self._rejected = set()
        self._failed = set()
        self.pending = 0
        self.last_error = ''

    @property
    def settled_count(self):
        """Return captures whose analysis completed, failed, or was rejected."""
        return len(self._credited | self._rejected | self._failed)

    def status(self):
        """Keep pending and rejected evidence visible to the caller."""
        calibration = self.calibration
        return {'source': self.source, 'credited': len(self._credited),
                'pending': self.pending, 'rejected': len(self._rejected),
                'failed': len(self._failed), 'last_error': self.last_error,
                'supported_classes': list(calibration.supported_classes) if calibration else [],
                'unsupported_classes': (list(calibration.unsupported_classes)
                                        if calibration else []),
                'calibration_kind': calibration.calibration_kind if calibration else None,
                'recognition_validated': bool(calibration and calibration.recognition_validated),
                'metric': 'analyzed_planar_visibility' if self.source == 'analyzed_photos'
                else 'saved_planar_visibility'}

    def update(self, planner, captures):
        """Credit new durable photos, matching their exact successful DB analysis."""
        settled = self._credited | self._rejected | self._failed
        remaining = {item['observation_id']: item for item in captures
                     if item['observation_id'] not in settled}
        self.pending = len(remaining)
        if not remaining:
            return
        rows = {}
        if self.source == 'analyzed_photos':
            path = self.directory / 'observations.sqlite3'
            try:
                connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                                             uri=True, timeout=.05)
                try:
                    placeholders = ','.join('?' for _ in remaining)
                    rows = {row[0]: row[1:] for row in connection.execute(
                        'SELECT id,status,model,metadata,analysis FROM observations '
                        f'WHERE id IN ({placeholders})', tuple(remaining))}
                finally:
                    connection.close()
            except sqlite3.Error:
                self.last_error = 'analysis_database_unavailable'
                return
        self.last_error = ''
        for identifier, metadata in remaining.items():
            if self.source == 'analyzed_photos':
                row = rows.get(identifier)
                if row is None or row[0] == 'pending':
                    continue
                if row[0] != 'ready':
                    self._failed.add(identifier)
                    continue
            try:
                if self.source == 'analyzed_photos':
                    if json.loads(row[2]) != metadata:
                        raise ValueError('analysis_metadata_mismatch')
                    analysis = json.loads(row[3])
                    if (not isinstance(analysis, dict)
                            or not isinstance(analysis.get('summary'), str)
                            or not analysis['summary'].strip()):
                        raise ValueError('empty_analysis')
                    if self.calibration and row[1] != self.calibration.model:
                        raise ValueError('analysis_model_mismatch')
                if self.calibration and (
                        metadata.get('image_size') != [
                            self.calibration.width, self.calibration.height]
                        or metadata.get('simulated') != self.calibration.simulated
                        or abs(metadata['camera_fov_rad'] - self.calibration.fov_rad) > .02):
                    raise ValueError('capture_calibration_mismatch')
                photo = self.directory / 'captures' / (identifier + '.jpg')
                manifest = photo.with_suffix('.json')
                if (photo.is_symlink() or manifest.is_symlink()
                        or not 0 < photo.stat().st_size <= 4*1024*1024
                        or manifest.stat().st_size > 16384
                        or json.loads(manifest.read_text()) != metadata
                        or hashlib.sha256(photo.read_bytes()).hexdigest()
                        != metadata['image_sha256']):
                    raise ValueError('capture_integrity_failed')
                pose = metadata['camera_pose']
                planner.mark_observed(
                    pose['x'], pose['y'], pose['yaw'], metadata['camera_fov_rad'])
            except (OSError, ValueError, KeyError, TypeError) as error:
                self._rejected.add(identifier)
                self.last_error = (str(error) if isinstance(error, ValueError)
                                   else 'invalid_capture')
                continue
            self._credited.add(identifier)
        self.pending = len(set(remaining) - self._credited - self._rejected - self._failed)
