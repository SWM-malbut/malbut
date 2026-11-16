"""Apply the registered owner's speech settings without blocking cloud or ROS callbacks."""

import math
import time


def _valid(profile):
    if not isinstance(profile, dict):
        return False
    limits = {'endpointMinSilenceS': (0.4, 2.0), 'silenceTimeoutS': (1.2, 5.0),
              'piperSpeed': (0.5, 2.0), 'speechVolumePercent': (0, 100)}
    return (profile.keys() == limits.keys()
            and all(type(profile[key]) in (float, int) and lo <= profile[key] <= hi
                    for key, (lo, hi) in limits.items())
            and type(profile['speechVolumePercent']) is int
            and profile['endpointMinSilenceS'] <= profile['silenceTimeoutS'])


def _parameter_request(values):
    from rcl_interfaces.srv import SetParametersAtomically
    from rclpy.parameter import Parameter

    return SetParametersAtomically.Request(parameters=[
        Parameter(name, value=value).to_parameter_msg() for name, value in values.items()])


class _NodeParameters:
    def __init__(self, client):
        self.client = client
        self.confirmed = None
        self.pending = None
        self.sent = None
        self.sent_at = -math.inf

    def sync(self, values, now):
        # The executor resolves futures; the cloud worker only polls them. Never hold
        # up a driving command or a homecam heartbeat waiting for a speech node.
        if self.pending is not None:
            if self.pending.done():
                try:
                    accepted = self.pending.result().result.successful
                except Exception:
                    accepted = False
                self.confirmed = self.sent if accepted else None
                self.pending = None
            elif now - self.sent_at >= 5 or not self.client.service_is_ready():
                self.client.remove_pending_request(self.pending)
                self.pending = self.confirmed = None

        if not self.client.service_is_ready():
            self.confirmed = self.sent = None
            return False

        # Reconcile even when a node restarted too quickly for discovery to notice.
        retry_after = 15 if self.confirmed == values else 2
        if (self.pending is None
                and (self.sent != values or now - self.sent_at >= retry_after)):
            self.sent_at = now
            self.sent = values
            try:
                self.pending = self.client.call_async(_parameter_request(values))
            except Exception:
                self.confirmed = None
        return self.confirmed == values and (self.pending is None or self.sent == values)


class VoiceProfileRelay:
    """Track independent acknowledgments from the running STT and TTS services."""

    def __init__(self, node):
        """Connect to the local speech parameter services."""
        from rcl_interfaces.srv import SetParametersAtomically

        self.stt, self.tts = (
            _NodeParameters(node.create_client(
                SetParametersAtomically, f'/malbut_{name}/set_parameters_atomically'))
            for name in ('stt', 'tts'))
        self.desired = None

    def receive(self, reply):
        """Accept the owner profile supplied in a cloud heartbeat reply."""
        if isinstance(reply, dict) and 'voiceProfile' in reply:
            value = reply['voiceProfile']
            self.desired = value if _valid(value) else None

    def advance(self):
        """Return the profile only after both nodes accept these exact values."""
        profile = self.desired
        if profile is None:
            return None
        now = time.monotonic()
        stt_ready = self.stt.sync({
            # Older STT nodes declared both timings but only read them at startup.
            'voice_profile_version': 1,
            'endpoint_min_silence_s': float(profile['endpointMinSilenceS']),
            'silence_timeout_s': float(profile['silenceTimeoutS']),
        }, now)
        tts_ready = self.tts.sync({
            'piper_speed': float(profile['piperSpeed']),
            'speech_volume_percent': profile['speechVolumePercent'],
        }, now)
        return profile if stt_ready and tts_ready else None
