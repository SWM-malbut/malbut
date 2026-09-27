"""Opt-in received-RGB tracking; no model imports or identity decisions here."""

import asyncio
import time


class LiveDiscoveryTracking:
    """One bounded session. Busy discoveries remain unidentified, not guessed.

    Frames are fetched one at a time from the existing bounded RGB buffer.
    The backend receives neither a Pose ID nor an incident/answer. Only the
    monitor may decide whether measured tracking boxes establish a link.
    """

    def __init__(self, monitor, tracker_factory, *, clock=time.monotonic, report=None):
        self.monitor, self.factory, self.clock = monitor, tracker_factory, clock
        self.report = report or (lambda reason: None)
        self.task = None
        self.session_id = None
        self.closed = False
        self.stopping = False
        self.last_reason = None

    def _report(self, reason):
        self.last_reason = reason
        self.report('discovery_tracking_' + reason)

    def offer(self, discovery):
        if self.closed or discovery.subject_key is not None:
            return
        if self.task is not None:
            if not self.task.done():
                self._report('busy')
                return
            self._completed()
        if not discovery.finding.regions:
            self._report('seed_unavailable')
            return
        sid = None
        try:
            sid = self.monitor.begin_discovery_tracking(discovery.discovery_id)
            region = discovery.finding.regions[0]
            seed_time = discovery.sample_times[region.frame_index]
            # Do not start a model when the exact seed has already been evicted.
            self.monitor.buffer.tracking_frame(seed_time, now=self.clock(), first=True)
        except ValueError:
            if sid is not None:
                self.monitor.end_discovery_tracking(sid)
            self._report('seed_unavailable')
            return
        self.session_id = sid
        self.stopping = False
        self.task = asyncio.create_task(self._run(sid, seed_time, region.box))

    def _completed(self):
        try:
            self.task.result()
        except asyncio.CancelledError:
            pass

    def _stop(self):
        if not self.stopping:
            self.stopping = True
            # Also covers cancellation before the coroutine has started.
            self.monitor.end_discovery_tracking(self.session_id)
            self.task.cancel()

    def maintain(self, *, accepting_images):
        if self.task is not None and self.task.done():
            self._completed()  # Persistence errors must still stop the runtime.
        elif self.task is not None:
            if not accepting_images or self.monitor.tracking_session_reason(self.session_id):
                self._stop()

    async def _run(self, sid, seed_time, seed_box):
        tracker = None
        reason = 'canceled'
        try:
            try:
                tracker = self.factory(seed_time=seed_time, seed_box=seed_box)
            except (ValueError, RuntimeError, OSError):
                reason = 'backend_failed'
                return
            last_time = seed_time
            first = True
            for _ in range(64):
                reason = self.monitor.tracking_session_reason(sid)
                if reason:
                    return
                # Waiting for a new received frame does not invent repeated images.
                while True:
                    frame = self.monitor.buffer.tracking_frame(
                        last_time, now=self.clock(), first=first)
                    if frame is not None:
                        break
                    if self.clock() - last_time > .5:
                        reason = 'capture_gap'
                        return
                    await asyncio.sleep(.01)
                try:
                    box = await tracker.step(frame)
                except (ValueError, RuntimeError, OSError, asyncio.TimeoutError):
                    reason = 'backend_failed'
                    return
                reason = self.monitor.tracking_session_reason(sid)
                if reason:
                    return
                # ROS image and Pose callbacks may arrive in either order.
                # Wait only while an exact Pose could still be a fresh input.
                while (not self.monitor.pose_observation_ready(frame.captured_at)
                       and self.clock() - frame.captured_at
                       < self.monitor.policy.max_person_observation_age_s):
                    await asyncio.sleep(.01)
                    reason = self.monitor.tracking_session_reason(sid)
                    if reason:
                        return
                result = self.monitor.ingest_discovery_track(
                    sid, observed_at=frame.captured_at, box=box)
                reason = result.reason
                if result.incident_id is not None or reason in {
                    'visual_track_broken', 'rgb_sample_unavailable',
                    'unknown_tracking_session', 'tracking_session_expired',
                    'source_incident_changed', 'target_claimed_by_other_finding',
                    'target_incident_closed', 'incident_capacity',
                }:
                    return
                last_time, first = frame.captured_at, False
            reason = 'frame_limit'
        except ValueError:
            reason = 'input_unavailable'
        except asyncio.CancelledError:
            reason = 'canceled'
            raise
        finally:
            self.stopping = True
            try:
                if tracker is not None:
                    await tracker.close()
            finally:
                self.monitor.end_discovery_tracking(sid)
                self._report(reason or 'stopped')

    async def close(self):
        self.closed = True
        if self.task is not None:
            if not self.task.done():
                self._stop()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
