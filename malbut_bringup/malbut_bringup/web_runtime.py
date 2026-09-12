"""List saved maps and asynchronously supervise only web-owned Bringup launches."""

from concurrent.futures import Future, ThreadPoolExecutor
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

import yaml


class SavedMapCatalog:
    """Resolve map IDs inside one directory without modifying any saved file."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()

    def _file(self, path):
        path = path.resolve(strict=True)
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError('Map files must remain inside the configured map directory')
        return path

    def resolve(self, map_id):
        """Validate a YAML filename, its Nav2 metadata, and its readable image."""
        if (not isinstance(map_id, str) or Path(map_id).name != map_id
                or Path(map_id).suffix.lower() not in ('.yaml', '.yml')):
            raise ValueError('Map ID must be a YAML filename from the map list')
        try:
            path = self._file(self.directory / map_id)
            with path.open(encoding='utf-8') as stream:
                metadata = yaml.safe_load(stream)
            if not isinstance(metadata, dict):
                raise ValueError('Map YAML must contain an object')
            image = metadata.get('image')
            if not isinstance(image, str) or not image:
                raise ValueError('Map YAML needs an image filename')
            image_path = self._file(path.parent / image)
            resolution = metadata.get('resolution')
            origin = metadata.get('origin')
            free = metadata.get('free_thresh')
            occupied = metadata.get('occupied_thresh')
            if (not _number(resolution) or resolution <= 0
                    or not isinstance(origin, list) or len(origin) != 3
                    or not all(_number(value) for value in origin)
                    or not _number(free) or not _number(occupied)
                    or not 0 <= free < occupied <= 1
                    or metadata.get('negate') not in (0, 1)
                    or metadata.get('mode', 'trinary') not in ('trinary', 'scale', 'raw')):
                raise ValueError('Map YAML has invalid Nav2 map metadata')
            import cv2
            pixels = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
            if pixels is None or pixels.size == 0:
                raise ValueError('Map image cannot be decoded')
            return path
        except (OSError, RuntimeError, yaml.YAMLError) as error:
            raise ValueError(f'Cannot read saved map: {error}') from error

    def list_maps(self):
        """List valid maps only; an absent map directory is an empty catalog."""
        if not self.directory.is_dir():
            return []
        result = []
        for path in sorted(self.directory.iterdir()):
            if path.suffix.lower() not in ('.yaml', '.yml'):
                continue
            try:
                resolved = self.resolve(path.name)
            except ValueError:
                continue
            result.append({'id': path.name, 'name': path.stem, 'path': str(resolved)})
        return result


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class RuntimeSupervisor:
    """
    Serialize launches and retain ownership until their process group exits.

    RUNNING means the launch process is alive, not that ROS nodes are ready.
    The caller checks ROS conflicts before start and cancels missions before stop.
    Neither snapshots nor submitted start/stop requests wait for process shutdown.
    """

    def __init__(self, catalog, log_directory=None, shutdown_stages=None):
        self.catalog = catalog
        self.log_directory = Path(log_directory or (
            Path.home() / '.ros/malbut/web_runtime')).expanduser()
        # AutoSLAM's owned-child cleanup may take 38 seconds before its parent
        # launch completes. Allow that cleanup before escalating this parent.
        self.shutdown_stages = shutdown_stages or (
            (signal.SIGINT, 45.0), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0))
        self._lock = threading.RLock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='web-bringup')
        self._process = None
        self._log = None
        self._stop_future = None
        self._closing = False
        self._closed = False
        self._status = {'state': 'STOPPED', 'mode': None, 'map': None,
                        'message': '', 'log_path': None}

    def snapshot(self):
        """Report process status; never discard ownership after a leader exits."""
        with self._lock:
            if (self._process is not None
                    and self._status['state'] not in ('STOPPING', 'ERROR')):
                code = self._process.poll()
                if code is not None:
                    self._status.update(
                        state='ERROR', message=f'Bringup exited ({code}); stop before retrying')
            return dict(self._status)

    def start(self, mode, map_id=None, start_hardware=True):
        """Queue one fixed Bringup command, rejecting overlapping transitions."""
        if mode not in ('mapping', 'navigation') or type(start_hardware) is not bool:
            raise ValueError('Only mapping/navigation and a boolean start_hardware are allowed')
        if mode == 'mapping' and map_id is not None:
            raise ValueError('Mapping does not load a saved map')
        if mode == 'navigation':
            self.catalog.resolve(map_id)
        with self._lock:
            if self._closed or self._closing:
                raise RuntimeError('Bringup supervisor is closing')
            if (self._process is not None
                    or self._status['state'] not in ('STOPPED', 'ERROR')):
                raise RuntimeError('Stop the current Bringup before starting another')
            self._status.update(state='STARTING', mode=mode, map=map_id,
                                message='Starting Bringup', log_path=None)
            return self._worker.submit(self._start, mode, map_id, start_hardware)

    def _start(self, mode, map_id, start_hardware):
        try:
            with self._lock:
                if self._status['state'] == 'STOPPING':
                    raise RuntimeError('Bringup start canceled before launch')
            command = ['ros2', 'launch', 'malbut_bringup', 'robot.launch.py',
                       f'mode:={mode}', 'web_panel:=false',
                       f'start_hardware:={str(start_hardware).lower()}']
            if mode == 'navigation':
                command += [f'map:={self.catalog.resolve(map_id)}',
                            'publish_debug_image:=true']
            else:
                command.append(f'map_directory:={self.catalog.directory}')
            self.log_directory.mkdir(parents=True, exist_ok=True)
            self._log = tempfile.NamedTemporaryFile(
                mode='w', prefix=f'{mode}-', suffix='.log',
                dir=self.log_directory, delete=False)
            with self._lock:
                self._status['log_path'] = self._log.name
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT, start_new_session=True)
            with self._lock:
                self._process = process
                if self._status['state'] != 'STOPPING':
                    self._status.update(
                        state='RUNNING',
                        message='Bringup process running; ROS readiness is separate')
            return self.snapshot()
        except Exception as error:
            with self._lock:
                if self._process is None and self._log is not None:
                    self._log.close()
                    self._log = None
                if self._status['state'] != 'STOPPING':
                    self._status.update(state='ERROR', message=str(error))
            raise

    def stop(self):
        """Queue owned process-group shutdown after the caller has stopped missions."""
        with self._lock:
            if self._stop_future is not None and not self._stop_future.done():
                return self._stop_future
            if self._closed or self._status['state'] == 'STOPPED':
                result = Future()
                result.set_result(dict(self._status))
                return result
            self._status.update(state='STOPPING', message='Stopping owned Bringup')
            self._stop_future = self._worker.submit(self._stop)
            return self._stop_future

    def _group_alive(self):
        if self._process is None:
            return False
        self._process.poll()  # Reap the leader even when descendants still exist.
        try:
            os.killpg(self._process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _stop(self):
        try:
            for sig, timeout in self.shutdown_stages:
                if not self._group_alive():
                    break
                try:
                    os.killpg(self._process.pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + timeout
                while self._group_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
            if self._group_alive():
                raise RuntimeError(
                    'Owned Bringup processes remain; ownership retained, retry stop')
            with self._lock:
                self._process = None
                if self._log is not None:
                    self._log.close()
                    self._log = None
                self._status.update(state='STOPPED', mode=None, map=None,
                                    message='Owned Bringup stopped')
                return dict(self._status)
        except Exception as error:
            with self._lock:
                self._status.update(state='ERROR', message=str(error))
            raise

    def close(self):
        """Stop owned processes and join the worker, outside HTTP/ROS callbacks."""
        with self._lock:
            self._closing = True
        self.stop().result()
        self._worker.shutdown(wait=True)
        with self._lock:
            self._closed = True
