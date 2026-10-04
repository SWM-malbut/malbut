"""Select existing Nav2 parameters at an idle localization boundary."""

from nav2_msgs.srv import ManageLifecycleNodes
from rcl_interfaces.srv import SetParameters
from rclpy.parameter import Parameter
from std_srvs.srv import SetBool


class NavigationProfile:
    """Reconfigure map-dependent Nav2 inputs without replacing its processes."""

    def __init__(self, node, group, profiles, mapped, call):
        self.profiles = profiles
        self.mapped = mapped
        self.paused = False
        self.call = call
        self.manager = node.create_client(
            ManageLifecycleNodes, '/lifecycle_manager_navigation/manage_nodes',
            callback_group=group)
        self.clients = {
            name: node.create_client(SetParameters, name + '/set_parameters',
                                     callback_group=group)
            for name in profiles['mapless']
        }
        self.keepout = (node.create_client(
            SetBool, '/local_costmap/keepout_filter/toggle_filter', callback_group=group)
            if '/local_costmap/local_costmap' in self.clients else None)

    def pause(self, mapped):
        """Stop costmap updates before changing coordinate frames or plugins."""
        if mapped != self.mapped and not self.paused:
            self._manage(ManageLifecycleNodes.Request.PAUSE)
            self.paused = True

    def activate(self, mapped):
        """Apply parameters while costmap executor threads still serve requests."""
        if mapped == self.mapped and not self.paused:
            return
        for name, values in self.profiles['mapped' if mapped else 'mapless'].items():
            request = SetParameters.Request()
            request.parameters = [
                (Parameter(key, type_=Parameter.Type.STRING_ARRAY, value=value)
                 if isinstance(value, list) else Parameter(key, value=value))
                .to_parameter_msg() for key, value in values.items()
            ]
            response = self.call(self.clients[name], request, name + ' parameters')
            if len(response.results) != len(request.parameters) or any(
                    not item.successful for item in response.results):
                raise RuntimeError(f'{name} rejected navigation profile')
        if self.keepout is not None:
            # Humble's filter reads enabled only at initialization; changing
            # its parameter alone does not change the running local costmap.
            request = SetBool.Request()
            request.data = self.profiles['mapped' if mapped else 'mapless'][
                '/local_costmap/local_costmap']['keepout_filter.enabled']
            if not self.call(self.keepout, request, 'local keepout toggle').success:
                raise RuntimeError('local keepout filter toggle failed')
        # cleanup destroys the costmap executor; therefore parameter calls must
        # precede RESET. Configure reads the stored frame/plugin parameters again.
        self._manage(ManageLifecycleNodes.Request.RESET)
        self._manage(ManageLifecycleNodes.Request.STARTUP)
        self.mapped = mapped
        self.paused = False

    def _manage(self, command):
        request = ManageLifecycleNodes.Request()
        request.command = command
        if not self.call(self.manager, request, 'navigation lifecycle').success:
            raise RuntimeError('navigation lifecycle transition failed')
