"""Share or pin PulseAudio microphone sources without changing desktop defaults."""

import json
import subprocess

import yaml


class PulseAudioUnavailableError(RuntimeError):
    """PulseAudio could not be queried to verify the microphone source."""


class SharedMicrophoneNotFoundError(RuntimeError):
    """Expected exactly one matching physical PulseAudio input source."""


def _load_stt_parameters(config_file):
    try:
        with open(config_file, encoding='utf-8') as stream:
            document = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f'Cannot read STT audio configuration: {config_file}') from error
    if not isinstance(document, dict):
        raise RuntimeError('STT audio configuration must be a ROS parameter mapping')
    parameters = {}
    for scope in ('/**', 'malbut_stt'):
        if scope not in document:
            continue
        node = document[scope]
        if not isinstance(node, dict) or not isinstance(node.get('ros__parameters'), dict):
            raise RuntimeError(f'STT audio configuration requires {scope}.ros__parameters')
        parameters.update(node['ros__parameters'])
    return parameters


def pulse_reference_environment(config_file):
    """Use the selected STT profile's explicit routes for its two audio peers."""
    parameters = _load_stt_parameters(config_file)
    backend = parameters.get('aec_backend', 'none')
    if backend == 'none':
        return {}
    if backend != 'pulse_reference':
        raise RuntimeError('STT audio configuration aec_backend must be none or pulse_reference')
    custom = parameters.get('input_pulse_source', '')
    if isinstance(custom, str) and custom.strip():
        raise RuntimeError(
            'STT audio configuration cannot combine input_pulse_source with pulse_reference')
    for key in ('aec_source', 'aec_sink'):
        if not isinstance(parameters.get(key), str) or not parameters[key].strip():
            raise RuntimeError(f'STT audio configuration requires a nonempty {key}')
    return {'MALBUT_SHARED_MICROPHONE': parameters['aec_source'],
            'PULSE_SOURCE': parameters['aec_source'], 'PULSE_SINK': parameters['aec_sink']}


def stt_input_pulse_source(config_file):
    """Return an optional PulseAudio source selector for STT only (never TTS/homecam)."""
    parameters = _load_stt_parameters(config_file)
    raw = parameters.get('input_pulse_source', '')
    if raw == '':
        return ''
    if not isinstance(raw, str):
        raise RuntimeError('STT audio configuration input_pulse_source must be a string')
    selector = raw.strip()
    if not selector:
        return ''
    if selector.startswith('match:') and not selector[len('match:'):].strip():
        raise RuntimeError('STT audio configuration match: selector requires a nonempty keyword')
    if parameters.get('aec_backend', 'none') == 'pulse_reference':
        raise RuntimeError(
            'STT audio configuration cannot combine input_pulse_source with pulse_reference')
    return selector


def _list_pulse_sources(environment):
    try:
        result = subprocess.run(
            ['pactl', '--format=json', 'list', 'sources'],
            env=environment, capture_output=True, text=True, check=True, timeout=3,
        )
        sources = json.loads(result.stdout)
        if not isinstance(sources, list):
            raise ValueError('Invalid PulseAudio source list')
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise PulseAudioUnavailableError(
            'Shared microphone unavailable: run Bringup as the desktop audio user; '
            "pactl must reach that user's PulseAudio server. "
            'Required packages: pulseaudio pulseaudio-utils libasound2-plugins.'
        ) from error
    return sources


def _is_physical_source(source):
    properties = source.get('properties', {})
    name = source.get('name', '')
    return bool(name) and not name.endswith('.monitor') and properties.get('device.class') != 'monitor'


def resolve_pulse_source(selector, environment):
    """Resolve an exact PulseAudio source name or ``match:<keyword>`` fail-closed."""
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError('PulseAudio source selector must be a nonempty string')
    selector = selector.strip()
    sources = [src for src in _list_pulse_sources(environment) if _is_physical_source(src)]
    if selector.startswith('match:'):
        needle = selector[len('match:'):].strip()
        if not needle:
            raise ValueError('match: selector requires a nonempty keyword')
        needle_cf = needle.casefold()
        matches = []
        for source in sources:
            properties = source.get('properties', {})
            haystack = ' '.join(str(part) for part in (
                source.get('name', ''),
                source.get('description', ''),
                properties.get('alsa.card_name', ''),
                properties.get('alsa.name', ''),
                properties.get('device.product.name', ''),
                properties.get('device.vendor.name', ''),
            )).casefold()
            if needle_cf in haystack:
                matches.append(source['name'])
        if len(matches) != 1:
            raise SharedMicrophoneNotFoundError(
                f'Configured STT microphone selector {selector!r} requires one PulseAudio input; '
                f'found {len(matches)}. Check USB connection/power; no other mic was selected.'
            )
        return matches[0]
    matches = [src['name'] for src in sources if src.get('name') == selector]
    if len(matches) != 1:
        raise SharedMicrophoneNotFoundError(
            f'Configured STT microphone source {selector!r} not found among PulseAudio inputs; '
            'check USB connection/power; no other mic was selected.'
        )
    return matches[0]


def ensure_pulse_source_unmuted(source_name, environment):
    """Unmute an explicitly configured PulseAudio input (e.g. DJI Mic Mini defaults muted)."""
    if not isinstance(source_name, str) or not source_name.strip():
        return False
    try:
        result = subprocess.run(
            ['pactl', 'set-source-mute', source_name, '0'],
            env=environment, capture_output=True, text=True, check=False, timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def shared_xfm_source(environment):
    """Find the physical XFM source without changing any system audio defaults."""
    sources = _list_pulse_sources(environment)
    matches = []
    for source in sources:
        properties = source.get('properties', {})
        identity = ' '.join(str(properties.get(key, '')) for key in (
            'alsa.card_name', 'alsa.name', 'device.product.name'))
        if 'XFM-DP' in identity and _is_physical_source(source):
            matches.append(source['name'])
    if len(matches) != 1:
        raise SharedMicrophoneNotFoundError(
            'Shared microphone requires one XFM-DP PulseAudio input; '
            f'found {len(matches)}. Check its input profile; no other mic was selected.'
        )
    return matches[0]
