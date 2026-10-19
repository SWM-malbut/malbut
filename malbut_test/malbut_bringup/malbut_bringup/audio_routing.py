"""Keep the dedicated robot user's PulseAudio on XFM and the USB speaker."""

import argparse
import json
import os
import subprocess
import sys


# USB identities survive ALSA/PulseAudio index and port changes. No serial is pinned.
MICROPHONE = ('2207', '0001')
SPEAKER = ('1b3f', '2008')
EVENT_TYPES = {'card', 'source', 'sink', 'source-output', 'sink-input', 'server'}


def pactl(*arguments):
    return subprocess.run(
        ['pactl', *map(str, arguments)], check=True, capture_output=True,
        text=True, timeout=5,
    ).stdout


def unique_device(devices, identity, profile=None):
    """Never pick an arbitrary device when the target is absent or ambiguous."""
    matches = []
    for device in devices:
        properties = device.get('properties', {})
        if (properties.get('device.bus') == 'usb'
                and (properties.get('device.vendor.id'),
                     properties.get('device.product.id')) == identity
                and properties.get('device.class') != 'monitor'
                and (profile is None or properties.get('device.profile.name') == profile)):
            matches.append(device)
    return matches[0] if len(matches) == 1 else None


def change(*arguments):
    pactl(*arguments)
    print('audio routing:', *arguments, flush=True)


def reconcile():
    snapshot = json.loads(pactl('--format=json', 'list'))
    profiles_changed = False
    for identity, profile in (
            (MICROPHONE, 'input:mono-fallback'),
            (SPEAKER, 'output:analog-stereo')):
        card = unique_device(snapshot['cards'], identity)
        if card is not None and profile in card['profiles']:
            if card['active_profile'] != profile:
                change('set-card-profile', card['name'], profile)
                profiles_changed = True
    # Profile changes replace devices; never route using their previous indices.
    if profiles_changed:
        snapshot = json.loads(pactl('--format=json', 'list'))
    defaults = json.loads(pactl('--format=json', 'info'))
    monitors = {source['index'] for source in snapshot['sources']
                if source.get('properties', {}).get('device.class') == 'monitor'}
    for kind, identity, profile, devices, streams, command in (
            ('source', MICROPHONE, 'mono-fallback', 'sources',
             'source_outputs', 'move-source-output'),
            ('sink', SPEAKER, 'analog-stereo', 'sinks',
             'sink_inputs', 'move-sink-input')):
        if unique_device(snapshot['cards'], identity) is None:
            continue
        target = unique_device(snapshot[devices], identity, profile)
        if target is None:
            continue  # A later device event will retry; do not select a substitute.
        if defaults['default_' + kind + '_name'] != target['name']:
            change('set-default-' + kind, target['name'])
        for stream in snapshot[streams]:
            if kind == 'source' and stream[kind] in monitors:
                continue  # Explicit loopback recordings must remain loopbacks.
            if stream[kind] != target['index']:
                change(command, stream['index'], target['name'])


def watch():
    # Subscribe before the first snapshot so a connection during startup is seen.
    events = subprocess.Popen(
        ['pactl', 'subscribe'], stdout=subprocess.PIPE, text=True,
        env={**os.environ, 'LC_ALL': 'C'},
    )
    try:
        reconcile()
        for line in events.stdout:
            event_type = line.rsplit(' on ', 1)[-1].split(' ', 1)[0]
            if event_type in EVENT_TYPES:
                reconcile()
        raise RuntimeError('PulseAudio event connection closed')
    finally:
        events.terminate()
        events.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Apply routing once and exit')
    args = parser.parse_args(argv)
    try:
        reconcile() if args.once else watch()
    except KeyboardInterrupt:
        return 0
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        print(f'audio routing unavailable: {error}', file=sys.stderr, flush=True)
        return 1  # The user service retries after hotplug races or server restarts.
    return 0


if __name__ == '__main__':
    sys.exit(main())
