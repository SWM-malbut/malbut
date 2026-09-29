"""Repair responsive Nav2 lifecycle groups without restarting live processes."""

import time

from lifecycle_msgs.msg import State, Transition
from lifecycle_msgs.srv import ChangeState, GetState
from nav2_msgs.srv import ManageLifecycleNodes
from rcl_interfaces.srv import SetParametersAtomically
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.parameter import Parameter
from std_srvs.srv import Trigger


class LifecycleUnavailable(RuntimeError):
    """No ROS response is not evidence of an exited process."""

    def __init__(self, service, reason):
        self.service, self.reason = service, reason
        super().__init__(
            f'{reason}: {service}; no process was killed or duplicated. '
            'An unresponsive live process cannot be repaired by lifecycle commands')


class LifecycleRecovery:
    """Use Nav2's manager for activation/bonds; never reset healthy servers."""

    def __init__(self, node):
        self.node = node
        self.clients = {}
        self.group = ReentrantCallbackGroup()
        # ROS service commands cannot be canceled after dispatch. Keep a timed-out
        # or canceled command pending so another recovery cannot overlap it.
        self.pending_command = None
        self.pending_client = None
        self.response_timeout_s = 3.0  # Set from the existing readiness sensor_timeout_s.

    @property
    def busy(self):
        """Whether a previously dispatched transition still has no response."""
        return self.pending_command is not None and not self.pending_command.done()

    def process_exited(self):
        """Forget outstanding commands only after their owning container has exited."""
        if self.busy:
            self.pending_client.remove_pending_request(self.pending_command)
            self.pending_command.cancel()
        self.pending_command = None
        self.pending_client = None

    def _client(self, kind, name):
        key = kind, name
        if key not in self.clients:
            self.clients[key] = self.node.create_client(kind, name, callback_group=self.group)
        return self.clients[key]

    @staticmethod
    def _check(deadline, canceled, service, reason):
        if canceled():
            raise RuntimeError('Lifecycle recovery canceled; dispatched commands may still finish')
        if time.monotonic() >= deadline:
            raise LifecycleUnavailable(service, reason)

    def _call(self, kind, name, request, deadline, canceled, *, command=False):
        client = self._client(kind, name)
        while not client.service_is_ready():
            self._check(deadline, canceled, name, 'service_not_discovered')
            time.sleep(0.05)
        self._check(deadline, canceled, name, 'deadline_exceeded')
        future = client.call_async(request)
        requested_at = time.monotonic()
        if command:
            self.pending_command = future
            self.pending_client = client
        try:
            while not future.done():
                self._check(deadline, canceled, name, 'response_timeout')
                if not command and time.monotonic() - requested_at >= self.response_timeout_s:
                    # One dropped response is not proof of a hung process.
                    # Only read-only queries are retried; never resend transitions.
                    client.remove_pending_request(future)
                    future.cancel()
                    while not client.service_is_ready():
                        self._check(deadline, canceled, name, 'service_not_discovered')
                        time.sleep(0.05)
                    self._check(deadline, canceled, name, 'response_timeout')
                    future = client.call_async(request)
                    requested_at = time.monotonic()
                time.sleep(0.05)
            result = future.result()
            if result is None:
                raise RuntimeError(f'Lifecycle service returned no result: {name}')
            return result
        finally:
            if not command and not future.done():
                client.remove_pending_request(future)
                future.cancel()

    def _states(self, names, deadline, canceled):
        return {name: self._call(GetState, f'/{name}/get_state', GetState.Request(),
                                 deadline, canceled).current_state for name in names}

    def recover(self, spec, deadline, canceled, report):
        """Restore one group, preserving inactive map state and healthy nodes."""
        if self.busy:
            raise RuntimeError('Previous lifecycle command is still awaiting a response')
        manager, names = spec['manager'], spec['nodes']
        observation = dict(manager=manager, before={}, action='NONE', state='CHECKING')
        report(observation)
        try:
            states = self._states(names, deadline, canceled)
            observation['before'] = {name: state.label for name, state in states.items()}
            active = self._call(Trigger, f'/{manager}/is_active', Trigger.Request(),
                                deadline, canceled).success
            ids = {state.id for state in states.values()}
            if active and ids == {State.PRIMARY_STATE_ACTIVE}:
                observation['state'] = 'PASS'
                report(observation)
                return
            # Do not interrupt an automatic transition or deactivate healthy nodes
            # just to make a group-wide RESUME/STARTUP request legal.
            if active or not ids <= {
                    State.PRIMARY_STATE_UNCONFIGURED, State.PRIMARY_STATE_INACTIVE}:
                raise RuntimeError(
                    f'{manager}: lifecycle states are mixed/transitioning or finalized '
                    f'({observation["before"]}, manager_active={active}); '
                    'healthy nodes were not reset')
            observation.update(state='ACTIVATING', action=(
                'STARTUP' if ids == {State.PRIMARY_STATE_UNCONFIGURED} else 'RESUME'))
            report(observation)
            for name, state in states.items():
                if state.id != State.PRIMARY_STATE_UNCONFIGURED:
                    continue  # A paused map/AMCL still owns its map and particles.
                parameters = spec.get('parameters', {}).get(name, {})
                if parameters:
                    request = SetParametersAtomically.Request(parameters=[
                        Parameter(key, value=value).to_parameter_msg()
                        for key, value in parameters.items()])
                    result = self._call(
                        SetParametersAtomically, f'/{name}/set_parameters_atomically',
                        request, deadline, canceled, command=True).result
                    if not result.successful:
                        raise RuntimeError(f'{name}: restore parameters failed: {result.reason}')
                if len(ids) > 1:
                    # Humble STARTUP configures ALL nodes; it fails on an already
                    # inactive node. Configure only the remaining unconfigured
                    # nodes, then let RESUME activate all and create their bonds.
                    request = ChangeState.Request()
                    request.transition.id = Transition.TRANSITION_CONFIGURE
                    if not self._call(ChangeState, f'/{name}/change_state', request,
                                      deadline, canceled, command=True).success:
                        raise RuntimeError(f'{name}: lifecycle configure failed')
            request = ManageLifecycleNodes.Request(command=getattr(
                ManageLifecycleNodes.Request, observation['action']))
            if not self._call(ManageLifecycleNodes, f'/{manager}/manage_nodes', request,
                              deadline, canceled, command=True).success:
                raise RuntimeError(f'{manager}: {observation["action"]} failed')
            after = self._states(names, deadline, canceled)
            active = self._call(Trigger, f'/{manager}/is_active', Trigger.Request(),
                                deadline, canceled).success
            if not active or any(state.id != State.PRIMARY_STATE_ACTIVE
                                 for state in after.values()):
                raise RuntimeError(f'{manager}: activation not confirmed by fresh state queries')
            observation['state'] = 'ACTIVE'
            report(observation)
        except Exception as error:
            observation.update(state='UNRESPONSIVE' if isinstance(error, LifecycleUnavailable)
                               else 'FAILED', error=str(error))
            report(observation)
            raise
