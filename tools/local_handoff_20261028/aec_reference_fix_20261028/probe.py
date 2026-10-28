"""Bounded speaker/microphone AEC measurement; restore all owned resources."""
import json
import os
from pathlib import Path
import subprocess
import time
import wave
import argparse

os.umask(0o077)
root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--variant', default='first')
parser.add_argument('--aec-method', default='webrtc', choices=['webrtc', 'speex'])
parser.add_argument('--aec-args', default='analog_gain_control=0 digital_gain_control=0 noise_suppression=0 high_pass_filter=1')
parser.add_argument('--mic-offset-us',type=int,default=0)
args = parser.parse_args()
reference = root / 'reference.wav'
root = root / args.variant
root.mkdir(exist_ok=True)

def pactl(*args):
    return subprocess.check_output(['pactl', *args], text=True, timeout=8).strip()

def listing(kind):
    return json.loads(pactl('--format=json', 'list', kind))

before = {'sources': listing('sources'), 'sinks': listing('sinks'),
          'modules': listing('modules'),
          'default_source': pactl('get-default-source'),
          'default_sink': pactl('get-default-sink')}
source = [x['name'] for x in before['sources'] if 'iflytek_XFM' in x['name']]
sink = [x['name'] for x in before['sinks'] if 'GeneralPlus' in x['name']]
assert len(source) == len(sink) == 1, 'Ambiguous physical audio devices'
assert not listing('sink-inputs') and not listing('source-outputs'), 'Audio is in use'
debug_files = [Path('/tmp/aec_'+name) for name in ['rec.sw','play.sw','out.sw','drift.txt']]
assert not any(p.exists() for p in debug_files), 'Existing AEC debug files must not be overwritten'
module = None
recorders, files = [], []
result = {'before': before, 'source': source[0], 'sink': sink[0], 'events': []}
card=next(c for c in listing('cards') if 'iflytek_XFM' in c['name'])
old_offset=int(card['ports']['analog-input']['latency_offset'].split()[0])
try:
    pactl('set-port-latency-offset',card['name'],'analog-input',str(args.mic_offset_us))
    module = pactl('load-module', 'module-echo-cancel',
        'source_master=' + source[0], 'sink_master=' + sink[0],
        'source_name=malbut_aec_probe_input', 'sink_name=malbut_aec_probe_output',
        'rate=16000', 'channels=1', 'save_aec=1', 'aec_method=' + args.aec_method,
        'aec_args="' + args.aec_args + '"')
    for p in debug_files:
        if p.exists():p.chmod(0o600)
    result['module'] = module
    result['during_modules'] = listing('modules')
    print('AEC module loaded:', module, flush=True)
    for label, device in [('raw', source[0]), ('aec', 'malbut_aec_probe_input'), ('monitor',sink[0]+'.monitor')]:
        f = (root / (label + '.pcm')).open('wb'); files.append(f)
        p = subprocess.Popen(['parec', '--device=' + device, '--raw', '--format=s16le',
            '--rate=16000', '--channels=1', '--latency-msec=20',
            '--property=application.name=malbut_aec_measurement_' + label], stdout=f, stderr=subprocess.PIPE)
        recorders.append(p)
    time.sleep(1)
    assert all(p.poll() is None for p in recorders), 'Recorder failed'
    result['events'].append(['playback_start', time.monotonic()])
    print('Playing prerecorded Korean notices; recording raw and AEC together', flush=True)
    playback = subprocess.Popen(['paplay', '--device=malbut_aec_probe_output',
        '--latency-msec=20', '--process-time-msec=10',
        '--property=application.name=malbut_aec_measurement_playback', str(reference)])
    try:
        time.sleep(1)
        result['during'] = {kind: listing(kind) for kind in
            ['sources', 'sinks', 'sink-inputs', 'source-outputs']}
        playback.wait(timeout=50)
        assert playback.returncode == 0
    finally:
        if playback.poll() is None: playback.terminate(); playback.wait(timeout=3)
    result['events'].append(['playback_end', time.monotonic()])
    time.sleep(1)
    result['ok'] = True
finally:
    for p in recorders:
        p.terminate()
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    for f in files: f.close()
    if module is not None: pactl('unload-module', module)
    pactl('set-port-latency-offset',card['name'],'analog-input',str(old_offset))
    for p in debug_files:
        if p.exists():p.rename(root/p.name)
    for kind in ('source', 'sink'):
        if pactl('get-default-' + kind) != before['default_' + kind]:
            pactl('set-default-' + kind, before['default_' + kind])
    result['restored'] = not any(x['name'] == 'module-echo-cancel' and
        'malbut_aec_probe' in x.get('argument', '') for x in listing('modules'))
    (root / 'probe.json').write_text(json.dumps(result, indent=2) + '\n')
    for label in ('raw', 'aec','monitor'):
        p = root / (label + '.pcm')
        if p.exists():
            with wave.open(str(root / (label + '.wav')), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                w.writeframes(p.read_bytes())
    print('Temporary audio module removed; original defaults restored', flush=True)
