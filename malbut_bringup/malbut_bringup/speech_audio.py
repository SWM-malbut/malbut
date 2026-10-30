"""Share the ROSOrin XFM input through the existing per-user PulseAudio server."""

import json
import subprocess

import yaml


def pulse_reference_environment(config_file):
    """Use the selected STT profile's explicit routes for its two audio peers."""
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
    backend = parameters.get('aec_backend', 'none')
    if backend == 'none':
        return {}
    if backend != 'pulse_reference':
        raise RuntimeError('STT audio configuration aec_backend must be none or pulse_reference')
    for key in ('aec_source', 'aec_sink'):
        if not isinstance(parameters.get(key), str) or not parameters[key].strip():
            raise RuntimeError(f'STT audio configuration requires a nonempty {key}')
    return {'MALBUT_SHARED_MICROPHONE': parameters['aec_source'],
            'PULSE_SOURCE': parameters['aec_source'], 'PULSE_SINK': parameters['aec_sink']}


def shared_xfm_source(environment):
    """Find the physical XFM source without changing any system audio defaults."""
    try:
        result = subprocess.run(
            ['pactl', '--format=json', 'list', 'sources'],
            env=environment, capture_output=True, text=True, check=True, timeout=3,
        )
        sources = json.loads(result.stdout)
        if not isinstance(sources, list):
            raise ValueError('Invalid PulseAudio source list')
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError(
            'Shared microphone unavailable: run Bringup as the desktop audio user; '
            "pactl must reach that user's PulseAudio server. "
            'Required packages: pulseaudio pulseaudio-utils libasound2-plugins.'
        ) from error
    matches = []
    for source in sources:
        properties = source.get('properties', {})
        identity = ' '.join(str(properties.get(key, '')) for key in (
            'alsa.card_name', 'alsa.name', 'device.product.name'))
        name = source.get('name', '')
        if ('XFM-DP' in identity and name and not name.endswith('.monitor')
                and properties.get('device.class') != 'monitor'):
            matches.append(name)
    if len(matches) != 1:
        raise RuntimeError(
            'Shared microphone requires one XFM-DP PulseAudio input; '
            f'found {len(matches)}. Check its input profile; no other mic was selected.'
        )
    return matches[0]
