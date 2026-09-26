"""Runtime-bound settings and independent Manager/server freshness checks.

The startup Manager ID is a deployment binding, not authentication. Access to
the ROS graph must be restricted separately. This module never imports ROS.
"""

import math
import re
import time
from uuid import uuid4


MANAGER_TIMEOUT_S = 5.0
SERVER_TIMEOUT_S = 15.0
SETTINGS_FIELDS = ('runtime_id', 'settings_revision', 'enabled',
                   'camera_enabled', 'cloud_consent')
HEARTBEAT_FIELDS = ('manager_runtime_id', 'runtime_id', 'sequence',
                    'settings_revision', 'server_checked_at', 'sent_at')


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value)


def _uint(value, minimum=0):
    return type(value) is int and minimum <= value < 2**64


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class FallSettingsControl:
    """Event-loop confined control; replayed messages cannot renew consent."""

    def __init__(self, adapter, *, manager_runtime_id='', runtime_id='', clock=time.monotonic):
        if manager_runtime_id != '' and not _id(manager_runtime_id):
            raise ValueError('invalid startup Manager ID')
        self.adapter, self._clock = adapter, clock
        if runtime_id != '' and not _id(runtime_id):
            raise ValueError('invalid startup VLM ID')
        self.runtime_id = runtime_id or str(uuid4())
        self.manager_runtime_id = manager_runtime_id
        now = clock()
        if not _time(now):
            raise ValueError('invalid control clock')
        self._last_now = now
        self._settings = None
        self._last_control = None
        self._last_sequence = 0
        self._last_sent = -1.0
        self._server_checked = -1.0
        self._server_watermark = -1.0
        self._invalidated_proof = -1.0
        self._proof_after = now
        self._reported_revision = 0
        self._recovery_since = now
        self._applied_since = -1.0
        self._recovered = False
        self._closed = False
        self._active = False
        self._effective = None
        self._pause = 'waiting_settings'
        self._cloud_block = 'waiting_settings'
        self.refresh()

    @property
    def accepting_images(self):
        self.refresh()
        return self._active

    @property
    def cloud_block_reason(self):
        self.refresh()
        return self._cloud_block

    def _response(self, requested_revision, code):
        settings = self._settings or {}
        return dict(
            applied=code in {'applied', 'already_applied'}, runtime_id=self.runtime_id,
            requested_revision=requested_revision if _uint(requested_revision) else 0,
            applied_revision=settings.get('settings_revision', 0),
            enabled=settings.get('enabled', False),
            camera_enabled=settings.get('camera_enabled', False),
            cloud_consent=settings.get('cloud_consent', False), reason_code=code)

    def apply_settings(self, **data):
        """Apply settings without treating an application as a heartbeat."""
        self.refresh()
        revision = data.get('settings_revision', 0)
        if (set(data) != set(SETTINGS_FIELDS) or not _id(data.get('runtime_id'))
                or not _uint(revision, 1)
                or any(type(data.get(key)) is not bool
                       for key in ('enabled', 'camera_enabled', 'cloud_consent'))):
            return self._response(revision, 'invalid_request')
        if data['runtime_id'] != self.runtime_id:
            return self._response(revision, 'runtime_mismatch')
        if self._closed:
            return self._response(revision, 'internal_error')
        if not self.manager_runtime_id:
            return self._response(revision, 'invalid_request')
        code = 'applied'
        if self._settings is not None:
            current = self._settings['settings_revision']
            if revision < current:
                return self._response(revision, 'stale_revision')
            if revision == current:
                if data != self._settings:
                    return self._response(revision, 'revision_conflict')
                code = 'already_applied'
        previous = self._settings
        self._settings = dict(data)
        self._applied_since = self._last_now
        self.refresh()
        if self._closed:
            self._settings = previous
            return self._response(revision, 'internal_error')
        return self._response(revision, code)

    def heartbeat(self, **data):
        """Accept only new messages for the startup-bound Manager and this VLM."""
        self.refresh()  # Detect expiry before a delayed message can hide it.
        now = self._last_now
        if self._closed or not self.manager_runtime_id or set(data) != set(HEARTBEAT_FIELDS):
            return False
        sent = data['sent_at']
        checked = data['server_checked_at']
        if (data['runtime_id'] != self.runtime_id
                or data['manager_runtime_id'] != self.manager_runtime_id
                or not _uint(data['sequence'], 1)
                or data['sequence'] <= self._last_sequence
                or not _uint(data['settings_revision'])
                or not _time(sent) or sent < self._last_sent
                or sent > now or now - sent >= MANAGER_TIMEOUT_S
                or not (_time(checked) or type(checked) in (int, float) and checked == -1)
                or checked > sent):
            return False
        self._last_sequence, self._last_sent = data['sequence'], sent
        self._last_control = now
        self._reported_revision = data['settings_revision']
        if checked == -1:
            self._invalidated_proof = max(self._invalidated_proof, self._server_watermark)
            if self._server_checked >= 0:
                self._proof_after = now
            self._server_checked = -1.0
        elif (checked < max(self._proof_after, self._server_watermark)
              or checked <= self._invalidated_proof):
            self._server_checked = -1.0
        else:
            self._server_checked = checked
            self._server_watermark = checked
        self.refresh()
        return True

    def refresh(self):
        now = self._clock()
        if not _time(now) or now < self._last_now:
            self._closed = True
            now = self._last_now
        self._last_now = now
        if self._last_control is not None and now - self._last_control >= MANAGER_TIMEOUT_S:
            self._last_control = None
            self._recovered = False
            self._recovery_since = now
            self._applied_since = -1.0
            self._proof_after = now
            self._invalidated_proof = max(self._invalidated_proof, self._server_watermark)
        settings = self._settings or {}
        live = self._last_control is not None
        fresh = (self._server_checked >= self._proof_after
                 and now - self._server_checked < SERVER_TIMEOUT_S)
        matching = (self._settings is not None
                    and self._reported_revision == settings['settings_revision'])
        if (live and fresh and matching
                and self._applied_since >= self._recovery_since):
            self._recovered = True
        if self._closed:
            pause = 'runtime_error'
        elif self._settings is None:
            pause = 'waiting_settings'
        elif not settings['enabled']:
            pause = 'disabled'
        elif not settings['camera_enabled']:
            pause = 'camera_off'
        elif not live or not self._recovered:
            pause = 'control_unavailable'
        else:
            pause = 'none'
        active = pause == 'none'
        if not active:
            block = pause
        elif not settings['cloud_consent']:
            block = 'cloud_consent_missing'
        elif not matching:
            block = 'settings_pending'
        elif self._server_checked < 0:
            block = 'server_settings_unavailable'
        elif not fresh:
            block = 'server_settings_stale'
        else:
            block = None
        self._pause, self._cloud_block, self._active = pause, block, active
        effective = active, bool(active and settings['cloud_consent']), block
        if effective == self._effective:
            return
        try:
            self.adapter.set_cloud_block(block)
            self.adapter.configure(enabled=active, camera_enabled=active,
                                   cloud_consent=effective[1], connected=block is None)
            self._effective = effective
        except Exception:
            # Unknown adapter/storage errors must not leave permission open.
            self._closed = True
            self._active = False
            self._pause = self._cloud_block = 'runtime_error'
            self._effective = None
            try:
                self.adapter.set_cloud_block('runtime_error')
                self.adapter.configure(enabled=False, camera_enabled=False,
                                       cloud_consent=False, connected=False)
            except Exception:
                pass

    def status(self):
        self.refresh()
        result = self._response(0, 'internal_error')
        for key in ('applied', 'requested_revision', 'reason_code'):
            result.pop(key)
        result.update(settings_applied=self._settings is not None,
                      accepting_images=self._active, pause_reason=self._pause,
                      runtime_state=('error' if self._closed else 'waiting_settings'
                                     if self._settings is None else 'ready'
                                     if self._active else 'paused'))
        return result

    def close(self):
        self._closed = True
        self.refresh()
