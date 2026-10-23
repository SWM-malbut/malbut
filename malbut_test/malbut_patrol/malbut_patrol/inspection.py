"""Credit planar visibility only from calibrated, analyzed photo evidence."""

import hashlib
import json
import math
from pathlib import Path
import sqlite3


class InspectionCalibration:
    """Bind an empirical presence range to one camera, model and environment."""

    def __init__(self, path, simulated):
        """Load one calibration and refuse another runtime environment."""
        data = Path(path).expanduser().read_bytes()
        self.digest = hashlib.sha256(data).hexdigest()
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get('camera'), dict):
            raise ValueError('Invalid inspection calibration')
        camera = value.get('camera', {})
        if (value.get('version') != 1 or value.get('validated') is not True
                or type(value.get('simulated')) is not bool
                or value['simulated'] != simulated
                or value.get('calibration_kind') != 'object_presence'
                or value.get('analysis_batch') != 'patrol_mosaic_v1'
                or not isinstance(value.get('model'), str) or not value['model'].strip()
                or not self._positive(value.get('range_m'))
                or any(type(camera.get(k)) is not int or not 0 < camera[k] <= 1280
                       for k in ('width', 'height'))
                or not self._positive(camera.get('hfov_rad'))
                or camera['hfov_rad'] >= math.pi
                or not self._positive(camera.get('height_m'))):
            raise ValueError('Invalid inspection calibration, batch policy or environment')
        self.range_m = float(value['range_m'])
        self.model = value['model']
        self.simulated = simulated
        self.camera = camera
        # Presence calibration never promotes category recognition claims.
        self.recognition_validated = False

    @staticmethod
    def _positive(value):
        return type(value) in (int, float) and math.isfinite(value) and value > 0

    def camera_record(self, width, height, fov, height_m):
        """Reject camera changes before capturing or accepting their evidence."""
        expected = self.camera
        if (width != expected['width'] or height != expected['height']
                or not self._positive(fov) or abs(fov - expected['hfov_rad']) > 0.01
                or not self._positive(height_m)
                or abs(height_m - expected['height_m']) > 0.02):
            raise ValueError('Camera does not match inspection calibration')
        return {'calibration_sha256': self.digest, 'width': width, 'height': height,
                'hfov_rad': fov, 'height_m': height_m}


class InspectionEvidence:
    """Read the existing observation DB without importing or starting the Agent."""

    def __init__(self, directory, calibration, run):
        """Start an empty evidence set bound to the current producer run."""
        self.directory = Path(directory).expanduser()
        self.calibration = calibration
        self.run = run
        self.runs = {run['patrol_id']: dict(run)}
        self.references = {}
        self.credited = set()
        self.pending = 0
        self.rejected = 0
        self.failed = 0
        self.last_error = ''

    def register_batch(self, run):
        """Bind another producer batch to this patrol's original map and frame."""
        if any(run[k] != self.run[k] for k in ('map_id', 'map_frame')):
            raise ValueError('Inspection batch changed map or frame')
        self.runs[run['patrol_id']] = dict(run)

    def poll(self, references, *, verify_all=False):
        """Return newly verified camera views; missing analysis earns no area."""
        for reference in references:
            identity = reference['observation_id']
            if identity in self.references and self.references[identity] != reference:
                raise ValueError('Credited inspection reference changed')
            self.references[identity] = dict(reference)
        references = list(self.references.values())
        self.pending = len(references) - len(self.credited)
        database = self.directory / 'observations.sqlite3'
        if not database.exists():
            self.pending = len(references)
            return []
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=0.05)
            connection.row_factory = sqlite3.Row
            with connection:
                dataset = connection.execute('SELECT simulated FROM dataset WHERE id=1').fetchone()
                owner = connection.execute('SELECT user_id FROM owner WHERE id=1').fetchone()
                if dataset is None or owner is None:
                    # The Agent creates tables before committing their identity rows.
                    self.pending = len(references)
                    return []
                if bool(dataset[0]) != self.calibration.simulated:
                    raise ValueError('Inspection observation database environment mismatch')
                views = []
                ready = set()
                for reference in references:
                    identity = reference['observation_id']
                    if identity in self.credited and not verify_all:
                        continue
                    row = connection.execute('SELECT * FROM observations WHERE id=? AND user_id=?',
                                             (identity, owner[0])).fetchone()
                    if row is None or row['status'] == 'pending':
                        if identity in self.credited:
                            raise ValueError('Credited inspection analysis changed')
                        continue
                    if row['status'] != 'ready':
                        self.failed += 1
                        raise ValueError('Inspection photo analysis failed or was excluded')
                    view = self._verify(reference, row)
                    if identity not in self.credited:
                        views.append(view)
                        ready.add(identity)
                # Validate the complete poll before allowing any of its new credit.
                self.credited.update(ready)
                self.pending = len(references) - len(self.credited)
                return views
        except sqlite3.OperationalError:
            # A new/locked DB may not yet expose its schema. The action timeout
            # remains in charge; database unavailability can never mean success.
            self.pending = len(references)
            self.last_error = 'Inspection analysis database unavailable'
            return []
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.rejected += 1
            self.last_error = str(error)
            raise ValueError('Inspection evidence rejected: ' + str(error)) from error
        finally:
            if 'connection' in locals():
                connection.close()

    def _verify(self, reference, row):
        identity = reference['observation_id']
        path = self.directory / 'captures' / (identity + '.json')
        data = path.read_bytes()
        if (path.is_symlink() or len(data) > 16384
                or hashlib.sha256(data).hexdigest() != reference['manifest_sha256']):
            raise ValueError('Photo metadata integrity mismatch')
        metadata = json.loads(data)
        run = self.runs.get(metadata.get('patrol_id'))
        if (metadata != json.loads(row['metadata'])
                or metadata['observation_id'] != identity
                or run is None
                or any(metadata[k] != run[k] for k in ('patrol_id', 'map_id', 'map_frame'))
                or metadata.get('analysis_batch') != 'patrol_mosaic_v1'
                or metadata['simulated'] != self.calibration.simulated
                or row['model'] != self.calibration.model
                or row['error'] is not None or row['analysis'] is None):
            raise ValueError('Photo analysis source or model mismatch')
        manifest = self.directory / 'runs' / (run['patrol_id'] + '.json')
        if manifest.is_symlink() or manifest.stat().st_size > 16384:
            raise ValueError('Invalid inspection batch manifest')
        sealed = json.loads(manifest.read_bytes())
        if (sealed.get('patrol_id') != run['patrol_id']
                or sealed.get('capture_failures') != 0
                or not 1 <= len(sealed['captures']) <= 24
                or reference not in sealed['captures']):
            raise ValueError('Inspection batch evidence mismatch')
        camera = metadata['inspection']
        expected = self.calibration.camera_record(camera['width'], camera['height'],
                                                  camera['hfov_rad'], camera['height_m'])
        if camera != expected:
            raise ValueError('Photo calibration mismatch')
        photo = path.with_suffix('.jpg')
        if (photo.is_symlink() or not 0 < photo.stat().st_size <= 4 * 1024 * 1024
                or hashlib.sha256(photo.read_bytes()).hexdigest() != metadata['image_sha256']):
            raise ValueError('Photo integrity mismatch')
        # The worker validates these sidecars too. Recheck after analysis so a
        # changed/deleted evidence file cannot retain previously earned credit.
        for suffix, digest in [('.regions.json', metadata.get('regions_sha256')),
                               ('.depth.npy', metadata.get('spatial', {}).get('depth_sha256')),
                               ('.lidar.npy', metadata.get('lidar', {}).get('sha256'))]:
            if digest is not None:
                sidecar = path.with_suffix(suffix)
                if (sidecar.is_symlink()
                        or hashlib.sha256(sidecar.read_bytes()).hexdigest() != digest):
                    raise ValueError('Photo spatial evidence mismatch')
        pose = metadata['camera_pose']
        if (set(pose) != {'x', 'y', 'yaw'}
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in pose.values())):
            raise ValueError('Invalid photo pose')
        return pose['x'], pose['y'], pose['yaw'], camera['hfov_rad']

    def status(self):
        """Describe evidence counts without claiming object recognition."""
        return {'source': 'analyzed_photo_batches', 'credited': len(self.credited),
                'batches': len(self.runs),
                'pending': self.pending, 'rejected': self.rejected, 'failed': self.failed,
                'last_error': self.last_error, 'calibration_kind': 'object_presence',
                'recognition_validated': False, 'metric': 'analyzed_planar_visibility'}
