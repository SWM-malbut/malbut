"""Permit pose processing only while the bound VLM accepts camera images."""

import math
import time


class FallPoseControl:
    """Local ROS-graph liveness check, not authentication or Cloud consent."""

    def __init__(self, runtime_id, clock=time.monotonic):
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise ValueError("fall_runtime_id is required")
        self.runtime_id = runtime_id
        self.clock = clock
        self.sequence = 0
        self.received_at = None
        self.allowed = False

    def receive(self, status):
        """Ignore another runtime and repeated or out-of-order status messages."""
        if (status.runtime_id != self.runtime_id
                or type(status.sequence) is not int
                or not self.sequence < status.sequence < 2**64):
            return False
        self.sequence = status.sequence
        self.received_at = self.clock()
        # cloud_consent and video storage are deliberately not pose gates.
        self.allowed = all(value is True for value in (
            status.settings_applied, status.enabled,
            status.camera_enabled, status.accepting_images,
        ))
        return True

    def active(self):
        """A stopped VLM cannot leave pose processing enabled indefinitely."""
        age = (self.clock() - self.received_at
               if self.received_at is not None else math.inf)
        if not math.isfinite(age) or not 0 <= age < 5.0:
            self.allowed = False
        return self.allowed
