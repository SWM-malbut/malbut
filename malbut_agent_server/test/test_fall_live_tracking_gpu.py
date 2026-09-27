"""Explicit local GPU smoke/parity test. Never contacts Cloud or a live camera."""

import asyncio
import json
import os
from pathlib import Path
import time

import pytest

from malbut_agent_server.adapters.outbound.sam_tracking import (
    SamTrackingSettings, SamTrackingWorker,
)
from malbut_agent_server.domain.fall_monitoring import RgbFrame

BUNDLE = os.environ.get('MALBUT_SAM_TEST_BUNDLE')
pytestmark = pytest.mark.skipif(not BUNDLE, reason='opt-in local GPU/assets required')


def test_gpu_worker_matches_recorded_incremental_boxes_without_blocking_loop():
    bundle = json.loads(Path(BUNDLE).read_text())
    settings = SamTrackingSettings.parse(bundle['tracking'])
    case = json.loads(Path(bundle['plan']).read_text())['cases'][bundle['case_id']]
    rows = json.loads(Path(bundle['pose']).read_text())
    expected = json.loads(Path(bundle['reference']).read_text())['samples']
    selected = [row for row in rows if row['source_frame'] >= case['seed']['source_frame']]
    assert [r['source_frame'] for r in selected] == [r['source_frame'] for r in expected]
    async def run():
        worker = SamTrackingWorker(
            settings, seed_time=selected[0]['captured_at'], seed_box=tuple(case['seed']['box']))
        times, heartbeat = [], []
        async def ticker():
            while True:
                heartbeat.append(time.monotonic())
                await asyncio.sleep(.01)
        tick = asyncio.create_task(ticker())
        try:
            for row, reference in zip(selected, expected):
                jpeg = (Path(bundle['images']) / f"{row['source_frame']:05d}.jpg").read_bytes()
                started = time.monotonic()
                box = await worker.step(RgbFrame(row['captured_at'], jpeg))
                times.append(time.monotonic() - started)
                assert box == pytest.approx(reference['box'], abs=1e-7)
        finally:
            await worker.close()
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
        assert worker.process.returncode is not None
        assert len(heartbeat) > 10
        gaps = [b - a for a, b in zip(heartbeat, heartbeat[1:])]
        assert max(gaps) < 1  # Scheduling smoke check, not a robot timing guarantee.
        print(json.dumps(dict(frames=len(times), startup_and_first_s=times[0],
                              subsequent_min_s=min(times[1:]),
                              subsequent_max_s=max(times[1:]), max_loop_gap_s=max(gaps))))
    asyncio.run(run())

