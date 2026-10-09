"""Cancel observed foreground Goals using the existing ROS Action protocol."""

from uuid import UUID


class ForegroundCancellation:
    """Agent-side client only; no new manager endpoint or runtime ownership."""

    def __init__(self, node):
        from action_msgs.srv import CancelGoal
        from malbut_interfaces.msg import SystemState
        from rclpy.qos import DurabilityPolicy, QoSProfile

        self._node = node
        self._type = CancelGoal
        self._ids = None
        self._closed = False
        self._client = node.create_client(
            CancelGoal, '/malbut/mission/execute/_action/cancel_goal',
        )
        self._subscription = node.create_subscription(
            SystemState, '/malbut/state', self.observe,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )

    def observe(self, message):
        """Snapshot only foreground IDs, including work which could resume."""
        ids = []
        for group in (message.pending_missions, message.suspended_missions,
                      message.active_foreground_missions):
            for mission in group:
                if mission.mode == 0 and mission.mission_id not in ids:
                    ids.append(UUID(hex=mission.mission_id).hex)
        self._ids = ids

    def __call__(self):
        """Send explicit UUIDs; never use the protocol's cancel-all wildcard."""
        if self._closed or self._ids is None or not self._client.service_is_ready():
            raise RuntimeError('foreground cancellation unavailable')
        for goal_id in tuple(self._ids):
            request = self._type.Request()
            request.goal_info.goal_id.uuid = list(UUID(hex=goal_id).bytes)
            self._client.call_async(request).add_done_callback(self._receipt)
        return len(self._ids)

    def _receipt(self, future):
        if self._closed:
            return
        try:
            response = future.result()
            # UNKNOWN_GOAL_ID / GOAL_TERMINATED are normal for a stale snapshot.
            if response.return_code == response.ERROR_REJECTED:
                raise RuntimeError('cancel rejected')
        except Exception:
            self._node.get_logger().warning('Foreground Goal cancellation was not confirmed')

    def close(self):
        """Release only this Agent's client and subscription."""
        self._closed = True
        self._node.destroy_subscription(self._subscription)
        self._node.destroy_client(self._client)
