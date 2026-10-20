"""When the no-entry Zones are turned off, and that they always come back."""

from malbut_bringup.zone_override import (
    ESCAPE_LIMIT_S, GLOBAL_TOGGLE, LOCAL_TOGGLE, RESEND_S, ZoneOverride,
)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _send(override):
    due = override.due()
    for service, enabled in due:
        override.mark_sent(service, enabled)
    return dict(due)


def test_filters_start_on_and_only_changes_are_sent():
    override = ZoneOverride(clock=Clock())
    assert override.wanted() == {LOCAL_TOGGLE: True, GLOBAL_TOGGLE: True}
    assert _send(override) == {}


def test_manual_driving_turns_only_the_local_filter_off():
    override = ZoneOverride(clock=Clock())
    override.set_manual(True)
    assert _send(override) == {LOCAL_TOGGLE: False}
    override.set_manual(False)
    assert _send(override) == {LOCAL_TOGGLE: True}
    assert _send(override) == {}


def test_an_autonomous_escape_never_disables_either_filter():
    clock = Clock()
    override = ZoneOverride(clock=clock)
    override.start_escape()
    assert override.wanted() == {LOCAL_TOGGLE: True, GLOBAL_TOGGLE: True}
    assert _send(override) == {}
    clock.now += RESEND_S
    assert _send(override) == {}
    clock.now += ESCAPE_LIMIT_S
    assert not override.escaping()
    assert _send(override) == {}


def test_manual_driving_keeps_the_local_filter_off_after_an_escape():
    override = ZoneOverride(clock=Clock())
    override.start_escape()
    override.set_manual(True)
    assert _send(override) == {LOCAL_TOGGLE: False}
    override.end_escape()
    assert override.wanted() == {LOCAL_TOGGLE: False, GLOBAL_TOGGLE: True}
    assert _send(override) == {}
