"""Own fall-specific settings and handoffs, not robot mission scheduling."""

from rclpy.node import Node

from .fall_confirmation_link import FallConfirmationLink
from .fall_flow_link import FallFlowLink
from .fall_settings_link import FallSettingsLink


class FallRulesNode(Node):
    """Relay permissions and request urgent confirmation through ExecuteMission."""

    def __init__(self, *, runtime_id, bridge_runtime_id, vlm_runtime_id, flow=False):
        super().__init__('malbut_fall_rules')
        # manager_runtime_id remains the settings wire field for compatibility;
        # it identifies these fall rules, not the generic system manager.
        self.settings = FallSettingsLink(
            self, manager_id=runtime_id, bridge_id=bridge_runtime_id, vlm_id=vlm_runtime_id)
        # flow (Bringup fall_flow): the one fall check decides; off, the earlier link.
        self.confirmation = (FallFlowLink if flow else FallConfirmationLink)(
            self, runtime_id=vlm_runtime_id)

    def close(self):
        """Stop permission heartbeats and cancel our managed confirmation."""
        self.settings.close()
        return self.confirmation.close()

    def destroy_node(self):
        """Release the Action client before destroying ROS entities."""
        self.close()
        self.confirmation.destroy()
        return super().destroy_node()
