"""ROS callbacks for the fall coordinator settings relay, outside Agent missions."""

from threading import RLock

from .fall_settings import FallSettingsRelay


class FallSettingsLink:
    """Keep asynchronous Service calls independent of the one-second heartbeat."""

    def __init__(self, node, *, manager_id, bridge_id, vlm_id):
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.clock import Clock, ClockType
        from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
        from malbut_interfaces.msg import (
            FallControlHeartbeat, FallRuntimeStatus, FallSettingsSnapshot, FallSettingsReport,
        )
        from malbut_interfaces.srv import ApplyFallSettings

        self.node = node
        self.lock = RLock()
        self.relay = FallSettingsRelay(manager_id=manager_id, bridge_id=bridge_id, vlm_id=vlm_id)
        self.future = None
        self.call_id = None
        self.group = MutuallyExclusiveCallbackGroup()
        self.heartbeat_type = FallControlHeartbeat
        self.report_type = FallSettingsReport
        self.service_type = ApplyFallSettings
        self.heartbeats = node.create_publisher(
            FallControlHeartbeat, '/malbut/falls/control/heartbeat', 10)
        latched = QoSProfile(
            depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.reports = node.create_publisher(
            FallSettingsReport, '/malbut/falls/settings/report', latched)
        self.client = node.create_client(ApplyFallSettings, '/malbut/falls/settings/apply',
                                         callback_group=self.group)
        self.snapshots = node.create_subscription(
            FallSettingsSnapshot, '/malbut/falls/settings/snapshot',
            self.on_snapshot, latched, callback_group=self.group)
        self.statuses = node.create_subscription(
            FallRuntimeStatus, '/malbut/falls/status', self.on_status, 10,
            callback_group=self.group)
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(1.0, self.tick, clock=self.clock, callback_group=self.group)

    @staticmethod
    def fields(message):
        """Copy native message fields without exposing ROS types to the core."""
        return {k: getattr(message, k) for k in message.get_fields_and_field_types()}

    def on_snapshot(self, message):
        """Try applying a changed setting immediately, not at the scan interval."""
        with self.lock:
            self.relay.snapshot(self.fields(message))
            self._send()

    def on_status(self, message):
        """Receive status only from the startup-bound VLM."""
        with self.lock:
            self.relay.status(self.fields(message))
            self._send()

    def tick(self):
        """Send liveness and collect Service timeouts without calling a VLM."""
        with self.lock:
            data = self.relay.heartbeat()
            if data is not None:
                self.heartbeats.publish(self.heartbeat_type(**data))
            self._send()

    def _send(self):
        if self.future is not None:
            self.relay.poll()
            if self.relay.pending is not None:
                return
            self.client.remove_pending_request(self.future)
            self.future.cancel()
            self.future = None
        if self.relay.closed or not self.client.service_is_ready():
            return
        dispatch = self.relay.request()
        if dispatch is None:
            return
        call_id, request = dispatch
        try:
            self.future = self.client.call_async(self.service_type.Request(**request))
            self.call_id = call_id
            self.future.add_done_callback(lambda future: self._done(call_id, future))
        except Exception:
            self.future = None
            self.relay.transport_failed(call_id)

    def _done(self, call_id, future):
        with self.lock:
            if self.call_id != call_id or self.future is not future:
                return
            self.future = None
            try:
                report = self.relay.complete(call_id, self.fields(future.result()))
            except Exception:
                self.relay.transport_failed(call_id)
                return
            if report is not None:
                self.reports.publish(self.report_type(**report))

    def close(self):
        """Stop settings traffic before the coordinator shuts down its executor."""
        with self.lock:
            self.relay.close()
            self.timer.cancel()
            if self.future is not None:
                self.client.remove_pending_request(self.future)
                self.future.cancel()
                self.future = None
