"""Share the ROSOrin XFM input through the existing per-user PulseAudio server."""

import json
import subprocess


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
