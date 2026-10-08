"""Fixed Manager-owned device operations and their movement boundary."""

import json
import re


OPERATIONS = frozenset({
    'status', 'runtime_start', 'runtime_stop', 'map_list', 'map_select', 'map_delete',
    'zones_get', 'zones_update', 'homecam_status', 'homecam_events',
    'homecam_recordings', 'homecam_falls', 'homecam_settings', 'result_publish',
})
PREPARATIONS = frozenset({'runtime_start', 'map_select'})
RESIDENT_CAPABILITIES = frozenset({'device_operation', 'get_weather', 'set_weather_location'})


def validate_device_operation(arguments):
    """Reject unregistered operations and unbounded/non-object JSON before dispatch."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', arguments['request_id']):
        raise ValueError('device_operation requires a bounded request_id')
    if arguments['operation'] not in OPERATIONS:
        raise ValueError('device_operation operation is not registered')
    raw = arguments['arguments_json']
    if len(raw.encode('utf-8')) > 16384:
        raise ValueError('device_operation arguments_json exceeds 16384 bytes')
    try:
        value = json.loads(raw, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError('device_operation arguments_json must be valid JSON') from error
    if not isinstance(value, dict):
        raise ValueError('device_operation arguments_json must be an object')


def is_preparation(mission):
    """Identify preparations that may cause localization motion without owning BASE."""
    return (mission.capability.capability_id == 'device_operation'
            and mission.arguments.get('operation') in PREPARATIONS)


def survives_runtime_stop(mission):
    """Only resident management/query work survives a robot child shutdown."""
    return (mission.capability.capability_id in RESIDENT_CAPABILITIES
            and not is_preparation(mission))


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")
