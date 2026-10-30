"""Frozen pure-NumPy native comparison. No transport, device, ROS, or models."""
from collections import deque
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
from threading import Event, Lock
import time
import tracemalloc

import numpy as np

sys.dont_write_bytecode = True
if sys.flags.optimize:
    raise RuntimeError('Assertions must remain enabled for exactness guards')

HERE = Path(__file__).resolve().parent
RAW = HERE / 'raw8.pcm'
ACTUAL = [0, 0, .53, .27, .525, .265]
STATE = ['weights', 'window', 'history', 'ola', 'denominator']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def provenance():
    manifest = json.loads((HERE/'sha256.json').read_text())
    actual = {name:sha(HERE/name) for name in manifest}
    assert actual == manifest, 'Frozen pack hash mismatch'
    protocol = json.loads((HERE/'protocol.json').read_text())
    assert protocol['id'] == 'cpu35-bridge-four-fft-native-v1'
    for name, expected in protocol['payload_sha256'].items():
        assert actual[name] == expected, ('Payload hash mismatch', name)
    assert protocol['delays'] == ACTUAL
    assert RAW.stat().st_size == 960000*8*2
    return actual


# Verify copied Python and arrays before importing any payload source.
provenance()
ORIGINAL = load('original', HERE / 'original.py').SpatialFilter
PROPOSED = load('proposed', HERE / 'proposed.py').SpatialFilter
PHASE = load('saved_phase', HERE / 'startup_phase.py')
WEIGHTS = np.load(HERE / 'weights.npy')


def phase(factory, delays=None):
    # Bypass the constructor: no socket, thread, listener, or IPC is created.
    value = PHASE.PhaseBridge.__new__(PHASE.PhaseBridge)
    value.weights = WEIGHTS; value.filter_factory = factory
    value.failure = None; value.deadline = float('inf'); value.lock = Lock()
    value.previous = np.zeros(6, '<i2'); value.pending = None
    value.delays = delays; value.sealed = delays is not None
    value.next_frame = 0; value.boundary = 0 if delays is not None else None
    value.applied = Event(); value.history = deque(maxlen=3)
    weights = PHASE._phase_weights(WEIGHTS, delays) if delays is not None else WEIGHTS
    return value, factory(weights), np.zeros(384, 'f4')


def original_step(processor, raw, delay):
    blocks = []
    for offset in range(0, 512, 128):
        voice = (processor.process(raw[offset:offset+128])*32).clip(-.98, .98)
        blocks.append(np.column_stack((voice, delay[:128].copy())).astype('<f4'))
        delay[:-128] = delay[128:]; delay[-128:] = raw[offset:offset+128, 1]
    return np.concatenate(blocks)


def proposed_step(processor, raw, delay):
    voice = (processor.process_block(raw)*32).clip(-.98, .98)
    output = np.empty((512, 2), dtype='<f4')
    output[:, 0] = voice
    output[:384, 1] = delay
    output[384:, 1] = raw[:128, 1]
    delay[:] = raw[128:, 1]
    return output


def pcm(value):
    return (np.clip(value, -1, 1)*32767).astype('<i2').tobytes()


def identical(a, b):
    return a.shape == b.shape and a.dtype == b.dtype and a.tobytes() == b.tobytes()


def parity(raw, delays, switch=None):
    phases, processors, timing = zip(*(phase(c, delays if switch is None else None) for c in (ORIGINAL, PROPOSED)))
    processors = list(processors); digest = hashlib.sha256(); pcm_digest = hashlib.sha256()
    for start in range(0, len(raw), 512):
        block = raw[start:start+512]; before = block.tobytes()
        values = []
        if start == switch:
            for p in phases: p.pending = delays
        for i, step in enumerate((original_step, proposed_step)):
            processors[i], shifted = phases[i].prepare(processors[i], block, start)
            values.append(step(processors[i], shifted, timing[i])); phases[i].written(start+512)
        assert identical(values[0], values[1]), ('bridge_float', start)
        assert pcm(values[0]) == pcm(values[1]), ('bridge_pcm', start)
        assert block.tobytes() == before, ('input_mutated', start)
        assert identical(timing[0], timing[1]), ('timing_state', start)
        for key in STATE:
            assert identical(getattr(processors[0], key), getattr(processors[1], key)), (key, start)
        for key in ('delays', 'pending', 'next_frame', 'boundary'):
            assert getattr(phases[0], key) == getattr(phases[1], key), (key, start)
        assert identical(phases[0].previous, phases[1].previous)
        assert phases[0].applied.is_set() == phases[1].applied.is_set()
        for (af, ar), (bf, br) in zip(phases[0].history, phases[1].history):
            assert af == bf and identical(ar, br)
        assert not any(np.shares_memory(values[1], getattr(processors[1], key)) for key in STATE)
        assert not np.shares_memory(values[1], timing[1])
        digest.update(values[0].tobytes()); pcm_digest.update(pcm(values[0]))
    return dict(frames=len(raw), blocks=len(raw)//512, float_sha256=digest.hexdigest(), pcm_sha256=pcm_digest.hexdigest(), exact=True)


def finite_filter_edges():
    rng = np.random.default_rng(914)
    source = rng.uniform(-1, 1, (24, 512, 6)).astype('f4')
    source[0] = 0.; source[1] = -0.
    source[2] = np.nextafter(np.float32(0), np.float32(1))
    source[3] = -np.finfo('f4').tiny
    source[4] = np.tile([-1, 32767/32768], (512, 3))
    source[5] = 0; source[5, [0, 127, 128, 383, 384, 511], 1] = [1, -1, 1, -1, 1, -1]
    a, b = ORIGINAL(WEIGHTS), PROPOSED(WEIGHTS)
    for i, block in enumerate(source):
        # A noncontiguous six-column input is also a supported existing NumPy path.
        larger = np.zeros((512, 12), 'f4'); larger[:, ::2] = block; block = larger[:, ::2]
        expected = np.concatenate([a.process(block[j:j+128]) for j in range(0, 512, 128)])
        actual = b.process_block(block)
        assert identical(expected, actual), ('finite_edges', i)
        for key in STATE: assert identical(getattr(a, key), getattr(b, key)), (key, i)
        saved = actual.copy(); b.process(np.zeros((128, 6), 'f4')); a.process(np.zeros((128, 6), 'f4'))
        assert identical(saved, actual), ('output_alias', i)
    for shape in ((128, 6), (511, 6), (513, 6), (512, 5), (512, 8), (512,)):
        try: b.process_block(np.zeros(shape))
        except ValueError: pass
        else: raise AssertionError(('bad_shape_accepted', shape))
    return dict(blocks=len(source), invalid_shapes=6, exact=True, includes=['signed_zero', 'subnormal', 'negative', 'fullscale', 'impulses', 'strided_input', '128_warmup_after_batch', 'output_nonalias'])


def raw_input():
    return np.asarray(np.memmap(RAW, dtype='<i2', mode='r').reshape(-1, 8))[:, :6]


def timed(arm):
    factory, step = (ORIGINAL, original_step) if arm == 'original' else (PROPOSED, proposed_step)
    raw = raw_input(); p, processor, delay = phase(factory, ACTUAL)
    start_cpu = time.process_time(); start_wall = time.perf_counter()
    last = None
    for start in range(0, len(raw), 512):
        processor, shifted = p.prepare(processor, raw[start:start+512], start)
        last = step(processor, shifted, delay).tobytes(); p.written(start+512)
    cpu = time.process_time()-start_cpu; wall = time.perf_counter()-start_wall
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # A separate single-block allocation probe is excluded from CPU/wall above.
    tracemalloc.start(); tracemalloc.reset_peak()
    processor, shifted = p.prepare(processor, raw[-512:], len(raw))
    step(processor, shifted, delay).tobytes(); p.written(len(raw)+512)
    current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    return dict(arm=arm, cpu_s=cpu, wall_s=wall, frames=len(raw),
                rss_peak_raw=rss, rss_peak_raw_unit='KiB', rss_peak_bytes=rss*1024,
                traced_single_block_current_bytes=current, traced_single_block_peak_bytes=peak,
                last_output_sha256=hashlib.sha256(last).hexdigest())


def native_environment():
    assert platform.system() == 'Linux' and platform.machine() in ('aarch64', 'arm64'), 'Expected Orin Linux arm64'
    assert np.__version__.split('.')[:2] == ['1', '24'], 'Expected existing NumPy 1.24'
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
        assert os.environ.get(name) == '1', ('Thread count not frozen', name)
    return dict(python=sys.version, numpy=np.__version__, platform=platform.platform(),
                thread_environment={name:os.environ[name] for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','VECLIB_MAXIMUM_THREADS')})


def validate():
    raw = raw_input(); rng = np.random.default_rng(309)
    random = rng.integers(-32768, 32768, (64*512, 8), dtype='<i2')[:, :6]
    zero = np.zeros_like(random)
    cases = {'saved60_actual_fractional':parity(raw, ACTUAL), 'finite_edges':finite_filter_edges()}
    for name, data, delays, switch in [('random_zero', random, [0]*6, None),
        ('random_negative_fractional', random, [0,0,-1,-.53,.27,1], None),
        ('random_integer', random, [0,0,1,1,1,1], None),
        ('phase_boundary_actual', random, ACTUAL, 1536),
        ('phase_boundary_integer', random, [0,0,1,1,1,1], 1536),
        ('phase_boundary_negative', random, [0,0,-1,-.53,.27,1], 1536),
        ('phase_boundary_zero', zero, [0]*6, 1536)]:
        cases[name] = parity(data, delays, switch)
    return cases


def save(out, result):
    temporary = out/'result.json.tmp'
    temporary.write_text(json.dumps(result, indent=2)); temporary.replace(out/'result.json')


def main(out):
    out.mkdir(parents=True, exist_ok=False)
    result = dict(protocol='cpu35-bridge-four-fft-native-v1', complete=False, stage='preflight',
                  scope='Isolated pure NumPy compute; no whole-stack CPU acceptance', cases={}, measurements=[],
                  warning='Mid-run saved raw tail; matched fresh states, not native historical state. Between-arm exactness is required on this host; no cross-host output identity claim.')
    save(out, result)
    try:
        result['runtime'] = native_environment(); before = provenance(); result['provenance'] = before
        result['stage'] = 'parity'; save(out, result)
        result['cases'] = validate()
        assert len(result['cases']) == 9 and all(x['exact'] for x in result['cases'].values())
        result['stage'] = 'timing'; save(out, result)
        # No timed child is started unless every parity condition above passed.
        for arm in ['original','proposed','proposed','original','original','proposed']:
            env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1')
            child = subprocess.run([sys.executable, str(__file__), '--timed', arm], check=True, capture_output=True, text=True, timeout=30, env=env)
            row = json.loads(child.stdout)
            assert row['arm'] == arm and row['frames'] == 960000
            assert all(np.isfinite(row[k]) and row[k] > 0 for k in ('cpu_s', 'wall_s', 'rss_peak_bytes'))
            assert row['rss_peak_raw_unit'] == 'KiB' and row['rss_peak_bytes'] == row['rss_peak_raw']*1024
            assert row['provenance'] == before
            assert isinstance(row['last_output_sha256'], str) and len(row['last_output_sha256']) == 64
            int(row['last_output_sha256'], 16)
            result['measurements'].append(row); save(out, result)
        assert len({x['last_output_sha256'] for x in result['measurements']}) == 1
        assert before == provenance(), 'Frozen pack changed'
        result.update(stage='complete', complete=True); save(out, result)
        print(json.dumps(dict(complete=True, parity_cases=9, measured_processes=6)))
    except BaseException as error:
        result['error'] = type(error).__name__+': '+str(error); save(out, result)
        if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
            detail = error.stderr or b''
            (out/'child-error.txt').write_text(detail.decode(errors='replace') if isinstance(detail, bytes) else detail)
        raise


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--timed':
        assert sys.argv[2] in ('original', 'proposed')
        native_environment(); before = provenance(); row = timed(sys.argv[2])
        assert before == provenance(); row['provenance'] = before
        print(json.dumps(row))
    elif len(sys.argv) == 2: main(Path(sys.argv[1]))
    else: raise SystemExit('Usage: run.py NEW_OUTPUT_DIRECTORY')
