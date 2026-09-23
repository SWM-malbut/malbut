"""Forward verified server settings, never values proposed by the Agent."""

import math
import re
import time


FLAGS = ('enabled', 'camera_enabled', 'cloud_consent')
RESULTS = {'applied', 'already_applied', 'runtime_mismatch', 'stale_revision',
           'revision_conflict', 'invalid_request', 'internal_error'}
CHECKS = {
    'waiting': {'waiting_server'}, 'confirmed': {'none'},
    'unavailable': {'server_timeout', 'server_transport_error'},
    'rejected': {'server_auth_failed', 'server_invalid_settings', 'server_settings_missing'},
}


def valid_id(value):
    """Bind startup peers by ID, not authenticate callers."""
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value)


def uint(value, minimum=0):
    """Reject bool and integers outside the native message range."""
    return type(value) is int and minimum <= value < 2**64


def stamp(value):
    """Check same-robot monotonic seconds without accepting NaN or bool."""
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class FallSettingsRelay:
    """Single-owner state machine; no ROS, HTTP, or Cloud calls."""

    def __init__(self, *, manager_id, bridge_id, vlm_id, clock=time.monotonic):
        if not all(valid_id(v) for v in (manager_id, bridge_id, vlm_id)):
            raise ValueError('all startup peer IDs are required')
        self.manager_id, self.bridge_id, self.vlm_id = manager_id, bridge_id, vlm_id
        self.clock = clock
        self.started = clock()
        if not stamp(self.started):
            raise ValueError('invalid monotonic clock')
        self.proof_after = self.started
        self.last_now = self.started
        self.closed = False
        self.snapshot_sequence = self.status_sequence = self.heartbeat_sequence = 0
        self.report_sequence = self.call_sequence = self.generation = 0
        self.last_status = None
        self.status_at = None
        self.settings = None
        self.checked = self.watermark = self.invalidated = -1.0
        self.last_observed = -1.0
        self.pending = None
        self.ack_key = None
        self.retry_at = 0.0
        self.was_ready = False

    def _now(self):
        now = self.clock()
        if not stamp(now) or now < self.last_now:
            self.closed = True
            return self.last_now
        now = float(now)
        self.last_now = now
        if self.status_at is not None and now - self.status_at >= 5:
            self.status_at = None
            self.proof_after = now
            self.invalidated = max(self.invalidated, self.watermark)
            self.generation += 1
            self.pending = None
        if self.pending and now - self.pending['sent_at'] >= 3:
            self.pending = None
            self.retry_at = now + 1
        return now

    def poll(self):
        """Expire pending work even while the Service is unavailable."""
        self._now()

    def snapshot(self, data):
        """Retain server confirmation time, including on failed HTTP polls."""
        now = self._now()
        if (self.closed or data.get('bridge_runtime_id') != self.bridge_id
                or not uint(data.get('sequence'), 1)
                or data['sequence'] <= self.snapshot_sequence):
            return False
        state = data.get('check_state')
        observed, checked = data.get('observed_at'), data.get('server_checked_at')
        revision = data.get('settings_revision')
        if (not isinstance(state, str) or state not in CHECKS
                or data.get('reason_code') not in CHECKS[state]
                or not stamp(observed) or observed > now or observed < self.last_observed
                or not (stamp(checked) or type(checked) in (int, float) and checked == -1)
                or checked > observed or not uint(revision)
                or any(type(data.get(k)) is not bool for k in FLAGS)):
            self._reject_proof()
            return False
        self.snapshot_sequence, self.last_observed = data['sequence'], observed
        if state == 'confirmed':
            if (revision == 0 or checked < self.started or checked < self.watermark
                    or checked <= self.invalidated):
                self._reject_proof()
                return False
            if self.settings is not None:
                previous = self.settings['settings_revision']
                if (revision < previous or revision == previous
                        and any(data[k] != self.settings[k] for k in FLAGS)):
                    self._reject_proof()
                    return False
            self.settings = dict(data)
            self.checked = self.watermark = checked
        elif state == 'rejected':
            self._reject_proof()
        elif state == 'unavailable':
            # Failed polls cannot refresh proof or substitute a new ON/OFF.
            old = self.settings or dict(settings_revision=0, **dict.fromkeys(FLAGS, False))
            if (checked != self.checked or revision != old['settings_revision']
                    or any(data[k] != old[k] for k in FLAGS)):
                self._reject_proof()
                return False
        elif revision != 0 or checked != -1 or any(data[k] for k in FLAGS):
            self._reject_proof()
            return False
        return True

    def _reject_proof(self):
        self.invalidated = max(self.invalidated, self.watermark)
        self.checked = -1.0

    def status(self, data):
        """Only the startup-bound VLM can refresh the five-second status lease."""
        now = self._now()
        if (self.closed or data.get('runtime_id') != self.vlm_id
                or not uint(data.get('sequence'), 1)
                or data['sequence'] <= self.status_sequence
                or not uint(data.get('applied_revision'))
                or type(data.get('settings_applied')) is not bool
                or any(type(data.get(k)) is not bool for k in FLAGS)):
            return False
        self.status_sequence, self.status_at = data['sequence'], now
        self.last_status = dict(data)
        if (self.was_ready and data.get('pause_reason') == 'control_unavailable'):
            self.generation += 1
            self.proof_after = now
            self.invalidated = max(self.invalidated, self.watermark)
            self.was_ready = False
        elif data.get('runtime_state') == 'ready':
            self.was_ready = True
        return True

    def heartbeat(self):
        """Report liveness without applying settings or inventing server proof."""
        now = self._now()
        if self.closed or self.status_at is None:
            return None
        self.heartbeat_sequence += 1
        return dict(manager_runtime_id=self.manager_id, runtime_id=self.vlm_id,
                    sequence=self.heartbeat_sequence,
                    settings_revision=self.settings['settings_revision'] if self.settings else 0,
                    server_checked_at=float(self.checked) if self.checked > self.invalidated
                    else -1.0,
                    sent_at=now)

    def request(self):
        """Return one numbered Service request, or None; never block the timer."""
        now = self._now()
        if (self.closed or self.pending or self.status_at is None or not self.settings
                or now < self.retry_at or self.checked < self.proof_after
                or self.checked <= self.invalidated
                or now - self.checked >= 15):
            return None
        key = (self.settings['settings_revision'], self.generation)
        if self.ack_key == key:
            return None
        self.call_sequence += 1
        request = dict(runtime_id=self.vlm_id,
                       settings_revision=self.settings['settings_revision'],
                       **{k: self.settings[k] for k in FLAGS})
        self.pending = dict(call_id=self.call_sequence, sent_at=now,
                            snapshot_sequence=self.settings['sequence'],
                            request=request, key=key)
        return self.call_sequence, dict(request)

    def complete(self, call_id, response):
        """Validate the real Service reply before producing an application report."""
        now = self._now()
        pending = self.pending
        if self.closed or not pending or pending['call_id'] != call_id:
            return None
        self.pending = None
        self.retry_at = now + 1
        request = pending['request']
        fields = {'runtime_id', 'requested_revision', 'applied_revision',
                  'applied', 'reason_code', *FLAGS}
        if (set(response) != fields
                or response.get('runtime_id') != self.vlm_id
                or response.get('requested_revision') != request['settings_revision']
                or not uint(response.get('requested_revision'), 1)
                or not uint(response.get('applied_revision'))
                or type(response.get('applied')) is not bool
                or any(type(response.get(k)) is not bool for k in FLAGS)
                or response.get('reason_code') not in RESULTS):
            return None
        success = response['reason_code'] in {'applied', 'already_applied'}
        if response['applied'] != success:
            return None
        if success and (response['applied_revision'] != request['settings_revision']
                        or any(response[k] != request[k] for k in FLAGS)):
            return None
        # A deterministic refusal is also reported once, not retried every second.
        self.ack_key = pending['key']
        self.report_sequence += 1
        return dict(bridge_runtime_id=self.bridge_id, manager_runtime_id=self.manager_id,
                    sequence=self.report_sequence, snapshot_sequence=pending['snapshot_sequence'],
                    reported_at=now, **response)

    def transport_failed(self, call_id):
        """Retry the same idempotent setting later; do not fabricate a failure reply."""
        if self.pending and self.pending['call_id'] == call_id:
            self.pending = None
            self.retry_at = self._now() + 1

    def close(self):
        """Stop heartbeats on Manager shutdown so the VLM expires its lease."""
        self.closed = True
        self.pending = None
