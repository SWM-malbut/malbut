"""Bounded asynchronous SAM worker. No model or GPU imports in the ROS process."""

import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path

from malbut_agent_server.domain.fall_monitoring import CloudPersonRegion, timestamp


@dataclass(frozen=True)
class SamTrackingSettings:
    python_executable: str
    source_path: str
    checkpoint_path: str
    python_paths: tuple = ()

    @classmethod
    def parse(cls, data):
        required = {'python_executable', 'source_path', 'checkpoint_path'}
        if (not isinstance(data, dict) or not required <= set(data)
                or set(data) - required - {'python_paths'}):
            raise ValueError('invalid tracking configuration')
        values = dict(data)
        paths = values.get('python_paths', [])
        if not isinstance(paths, list) or len(paths) > 8:
            raise ValueError('invalid tracking Python paths')
        for value in [*(values[k] for k in required), *paths]:
            if (not isinstance(value, str) or not Path(value).is_absolute()
                    or any(c in value for c in ('\n', '\r', '\x00', os.pathsep))):
                raise ValueError('tracking paths must be absolute')
        values['python_paths'] = tuple(paths)
        return cls(**values)


def worker_environment(settings):
    """Same explicit import paths for real inference and local readiness probes."""
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'CUDA_VISIBLE_DEVICES')
           if k in os.environ}
    package_root = str(Path(__file__).resolve().parents[3])
    env.update(PYTHONPATH=os.pathsep.join(
        (package_root, settings.source_path, *settings.python_paths)),
        PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='4')
    return env


class SamTrackingWorker:
    """Only JPEGs and a visual seed cross this pipe; no key, Pose ID or answer."""

    def __init__(self, settings, *, seed_time, seed_box):
        timestamp(seed_time)
        CloudPersonRegion(0, seed_box)
        self.settings = settings
        self.seed_time, self.seed_box = seed_time, seed_box
        self.process = None
        self.closed = False
        self.last_time = None
        self.count = 0

    async def _start(self):
        # Do not inherit Cloud credentials, LD_PRELOAD, ROS settings or arbitrary
        # Python search paths into the model process.
        cfg = self.settings
        package_root = str(Path(__file__).resolve().parents[3])
        # Shield launch only, so cancellation cannot orphan a just-spawned child.
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            cfg.python_executable, '-m',
            'malbut_agent_server.adapters.outbound.sam_tracking_worker',
            '--source', cfg.source_path, '--checkpoint', cfg.checkpoint_path,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=worker_environment(cfg), cwd=package_root,
            limit=4096))
        try:
            self.process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            self.process = await launch
            await self.close()
            raise
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=30)
        if line != b'{"ready":true}\n':
            raise ValueError('tracking worker unavailable')

    async def step(self, frame):
        timestamp(frame.captured_at)
        if (self.closed or self.count >= 64 or not isinstance(frame.jpeg, bytes)
                or not 0 < len(frame.jpeg) <= 1024 * 1024):
            raise ValueError('invalid tracking frame')
        if ((self.last_time is None and frame.captured_at != self.seed_time)
                or (self.last_time is not None
                    and not 0 < frame.captured_at - self.last_time <= .5 + 1e-9)):
            raise ValueError('invalid tracking sequence')
        if self.process is None:
            await self._start()
        payload = {'captured_at': frame.captured_at,
                   'jpeg': base64.b64encode(frame.jpeg).decode('ascii')}
        if self.last_time is None:
            payload['seed_box'] = self.seed_box

        async def exchange():
            self.process.stdin.write(json.dumps(payload, allow_nan=False).encode() + b'\n')
            await self.process.stdin.drain()
            return await self.process.stdout.readline()

        line = await asyncio.wait_for(exchange(), timeout=2)
        result = json.loads(line)
        if (not isinstance(result, dict) or set(result) != {'captured_at', 'box'}
                or type(result['captured_at']) not in (int, float)
                or result['captured_at'] != frame.captured_at):
            raise ValueError('invalid tracking reply')
        box = result['box']
        if box is not None:
            if not isinstance(box, list):
                raise ValueError('invalid tracking box')
            box = tuple(box)
            CloudPersonRegion(0, box)
        self.last_time = frame.captured_at
        self.count += 1
        return box

    async def close(self):
        self.closed = True
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
