"""Own the spatial microphone path and ROS for one normal service lifetime.

No detached restore timer: systemd must use KillMode=mixed. Every command is
bounded; the normal listening lifetime is not. This module never starts or
stops another service, kills a saved numeric PID, or removes a lock pathname.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

RELEASE = Path(__file__).resolve().parent
AEC = Path('/home/ubuntu/.local/share/malbut/speech-releases/aec-20261028')
NORMAL = Path('/home/ubuntu/.local/share/malbut/speech-releases/interaction-20261028')
PHYSICAL_SOURCE = 'alsa_input.usb-iflytek_XFM-DP-V0.0.18_1c001425758447f2650-01.mono-fallback'
CFG = '/data/build/user_cfg.ini'
VTN = '/data/build/vtn/vtn.ini'
ORIGIN = '/data/build/origin.pcm'
HASHES = {'weights.npy': '9edceb9216cd04f4828e54ea092cd202b6d730fe6cd005f912a4719d8150eed0',
          'leakage.npy': 'c892f05d81b09b0c1f46bb09016482044dd3fd27fdfe8929441287de338757ec',
          'phase_reference.npz': '41c74c61ac0710637309c625312922b75d3b2297ff2d8d6b3ec3d79c02a390f1'}


def save(path, value):
    temporary = path.with_name(path.name + '.new')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def identity(pid):
    try:
        # Field22 is the process start tick; the comm field may contain spaces.
        return {'pid': pid, 'start_ticks': Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]}
    except FileNotFoundError:
        return None


def gains(text):
    result = []
    for channel in range(1, 7):
        section = text.split(f"name='Channel {channel} PGA Gain'", 1)[1].split('numid=', 1)[0]
        result.append(int(re.search(r': values=(\d+)', section).group(1)))
    return result


def interrupted(number, frame):
    raise SystemExit(128 + number)


class Supervisor:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        if not self.state_dir.is_absolute(): raise ValueError('State directory must be absolute')
        self.state_dir = self.state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.active = self.state_dir / 'active.json'
        self.state = None
        self.children = {}
        self.lock_fd = None
        self.deadline = time.monotonic() + 90
        self.adb_root = RELEASE / 'adb-diag/root'
        self.adb_env = dict(os.environ, LD_LIBRARY_PATH=str(self.adb_root / 'usr/lib/aarch64-linux-gnu/android'))
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def command(self, *args, adb=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('Microphone lifecycle deadline exceeded')
        if adb: args = (str(self.adb_root/'usr/bin/adb'), '-P', '5042', *args)
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
            check=True, timeout=min(25, remaining), env=self.adb_env if adb else os.environ).stdout.decode().replace('\r', '')

    def shell(self, command): return self.command('shell', command, adb=True)

    def persist(self): save(self.active, self.state)

    def setting(self, name, target):
        stage = target + '.malbut-' + self.state['run']
        self.command('push', str(self.out/name), stage, adb=True)
        self.shell(f'chmod 644 {stage} && mv {stage} {target}')

    def spawn(self, role, command, env=None):
        log = (self.out/(role+'.log')).open('ab')
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL if role == 'warm' else log,
                stderr=log, env=env, start_new_session=True,
                pass_fds=(() if self.lock_fd is None else (self.lock_fd,)))
        finally: log.close()
        self.children[role] = child
        self.state['processes'][role] = identity(child.pid)
        self.persist()
        return child

    def stop(self, role):
        child = self.children.get(role)
        if child is None: return
        # Only current Popen children are signalled. Old-state PIDs are never killed.
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=3)

    def modules(self):
        modules = json.loads(self.command('pactl', '--format=json', 'list', 'modules'))
        short = []
        for line in self.command('pactl', 'list', 'short', 'modules').splitlines():
            fields = line.split('\t')
            if len(fields) < 2 or not fields[0].isdigit():
                raise RuntimeError('Unrecognized Pulse short-module listing')
            short.append((int(fields[0]), fields[1],
                          shlex.split(fields[2] if len(fields) > 2 else '')))
        for module in modules:
            # Jetson Pulse JSON omits index. Never substitute a saved numeric ID.
            matches = [index for index, name, args in short
                       if name == module['name'] and args == shlex.split(module.get('argument', ''))]
            previous = module.get('index')
            module['index'] = (matches[0] if len(matches) == 1 and
                (previous is None or str(previous) == str(matches[0])) else None)
        return modules

    def owned_module(self):
        matches = []
        for module in self.modules():
            words = shlex.split(module.get('argument', ''))
            for word in words:
                if re.match(r'source_name=malbut_(raw_spatial|spatial_)', word) and word != 'source_name='+self.state['source']:
                    raise RuntimeError('Another spatial Pulse source has foreign ownership')
            if 'source_name='+self.state['source'] in words:
                files = [word[5:] for word in words if word.startswith('file=')]
                if (module['name'] != 'module-pipe-source' or len(files) != 1 or
                        not Path(files[0]).is_absolute() or
                        Path(files[0]).resolve() != (self.out/'source.fifo').resolve()):
                    raise RuntimeError('Source name is owned by a foreign Pulse module')
                if module['index'] is None:
                    raise RuntimeError('Owned Pulse module ID is missing, ambiguous or changed')
                matches.append(module['index'])
        if len(matches) > 1: raise RuntimeError('Duplicate source ownership')
        return matches[0] if matches else None

    def origin_kind(self):
        return self.shell(f'if test -p {ORIGIN}; then echo fifo; elif test -f {ORIGIN}; then echo regular; elif test -e {ORIGIN}; then echo other; else echo missing; fi').strip()

    def verify(self, original=False):
        cfg = 'original.ini' if original else 'candidate.ini'
        vtn = 'original-vtn.ini' if original else 'candidate-vtn.ini'
        expected = self.state['original_gains'] if original else [0]*6
        if self.shell('cat '+CFG) != (self.out/cfg).read_text(): raise RuntimeError('Firmware configuration mismatch')
        if self.shell('cat '+VTN) != (self.out/vtn).read_text(): raise RuntimeError('VTN configuration mismatch')
        engine = self.shell('tail -18 /data/build/demo.log')
        mixer = self.shell('amixer -c 1 contents')
        if 'libvtn_mic6_circle.so' not in engine or gains(mixer) != expected:
            raise RuntimeError('mic6_circle engine or actual PGA verification failed')
        if self.origin_kind() != ('regular' if original else 'fifo'):
            raise RuntimeError('Microphone origin type mismatch')
        save(self.out/('restored-hardware.json' if original else 'active-hardware.json'),
             {'time': time.time(), 'engine': engine, 'gains': gains(mixer), 'origin': 'regular' if original else 'fifo'})

    def recover(self):
        if not self.active.exists(): return
        self.state = json.loads(self.active.read_text())
        run = self.state.get('run', '')
        if self.state.get('kind') != 'malbut-spatial-runtime03' or not re.fullmatch(r'run-[0-9a-f]{32}', run):
            raise RuntimeError('Unknown active state; refusing device changes')
        self.out = self.state_dir/run
        if self.state['boot'] == self.boot:
            for process in self.state['processes'].values():
                if process and identity(process['pid']) == process:
                    raise RuntimeError('Prior owner is still alive; refusing duplicate startup')
        for name, digest in self.state['backup_hashes'].items():
            if hashlib.sha256((self.out/name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('Saved original configuration failed integrity verification')
        self.restore(stale=True)
        self.children.clear()
        self.state = None

    def setup(self):
        for filename, digest in HASHES.items():
            if hashlib.sha256((RELEASE/'diverse_spatial'/filename).read_bytes()).hexdigest() != digest:
                raise RuntimeError('Frozen spatial coefficient mismatch')
        for file in (AEC/'environment.sh', AEC/'speech.launch.py', NORMAL/'code', self.adb_root/'usr/bin/adb'):
            if not file.exists(): raise RuntimeError('Missing runtime dependency: '+str(file))
        if self.origin_kind() != 'regular': raise RuntimeError('Foreign/missing microphone origin; refusing takeover')
        for module in self.modules():
            if re.search(r'\bsource_name=malbut_(raw_spatial|spatial_\w+)', module.get('argument', '')):
                raise RuntimeError('An unowned spatial source already exists')
        original = self.shell('cat '+CFG); vtn = self.shell('cat '+VTN)
        if 'beam=-3' not in original or 'debug=0' not in original or 'version=mic6_circle' not in original:
            raise RuntimeError('Original microphone configuration is not the verified normal profile')
        if 'beam = 1' not in vtn or 'ivw_enable = 1' not in vtn:
            raise RuntimeError('Original VTN configuration is not the verified normal profile')
        original_gains = gains(self.shell('amixer -c 1 contents'))
        run = 'run-'+uuid.uuid4().hex
        self.out = self.state_dir/run; self.out.mkdir(mode=0o700)
        (self.out/'original.ini').write_text(original); (self.out/'original-vtn.ini').write_text(vtn)
        self.command('pull', ORIGIN, str(self.out/'original-origin.pcm'), adb=True)
        (self.out/'candidate.ini').write_text(original.replace('debug=0','debug=1').replace('beam=-3','beam=5'))
        (self.out/'candidate-vtn.ini').write_text(vtn.replace('beam = 1','beam = 5').replace('ivw_enable = 1','ivw_enable = 0'))
        self.state = dict(kind='malbut-spatial-runtime03', run=run, boot=self.boot,
            source='malbut_spatial_'+run[4:], original_gains=original_gains,
            processes={'supervisor': identity(os.getpid())},
            backup_hashes={n:hashlib.sha256((self.out/n).read_bytes()).hexdigest()
                for n in ('original.ini','original-vtn.ini','original-origin.pcm')})
        phase_dir = Path('/tmp') / ('malbut-phase-' + run[4:])
        phase_dir.mkdir(mode=0o700)
        self.state['phase_dir'] = str(phase_dir)
        # Flush originals themselves before publishing the durable recovery marker.
        for name in (*self.state['backup_hashes'], 'candidate.ini', 'candidate-vtn.ini'):
            with (self.out/name).open('rb') as stream: os.fsync(stream.fileno())
        descriptor = os.open(self.out, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        self.persist()  # Durable recovery identity exists before the first device mutation.
        config = (RELEASE/'stt.yaml').read_text()
        if config.count(PHYSICAL_SOURCE) != 1: raise RuntimeError('Unexpected STT source configuration')
        (self.out/'stt.yaml').write_text(config.replace(PHYSICAL_SOURCE, self.state['source']))
        fifo = self.out/'source.fifo'
        module = self.command('pactl','load-module','module-pipe-source','source_name='+self.state['source'],
            'file='+str(fifo),'rate=16000','channels=2','format=float32le').strip()
        if not module.isdigit(): raise RuntimeError('Pulse module did not return an ID')
        self.state['module_id'] = int(module); self.persist()
        self.spawn('warm', ['parec','--device='+self.state['source'],'--raw','--format=float32le',
                           '--rate=16000','--channels=2','--latency-msec=32'])
        backup = '/data/build/.malbut-spatial-origin-'+run
        self.shell(f'test ! -e {backup} && mv {ORIGIN} {backup} && mkfifo {ORIGIN}')
        self.spawn('bridge', [sys.executable,str(RELEASE/'raw_spatial_ring_bridge.py'),str(self.out),
            str(RELEASE/'diverse_spatial/weights.npy'),str(fifo)],
            dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                 MALBUT_SPATIAL_PHASE_DIR=self.state['phase_dir']))
        self.setting('candidate-vtn.ini', VTN); self.setting('candidate.ini', CFG)
        time.sleep(2)
        for channel in range(1, 7): self.shell(f'amixer -c 1 cset name="Channel {channel} PGA Gain" 0')
        self.verify()
        while not (self.out/'bridge-ready').exists():
            self.check_children()
            if time.monotonic() >= self.deadline: raise TimeoutError('Bridge did not become ready')
            time.sleep(.1)
        self.check_children()

    def start_ros(self, extra):
        env = dict(os.environ, PULSE_SOURCE=self.state['source'], MALBUT_SHARED_MICROPHONE=self.state['source'],
                   MALBUT_SPATIAL_PHASE_DIR=self.state['phase_dir'])
        env.pop('MALBUT_TTS_OUTPUT_TRACE_DIR', None)
        if os.environ.get('MALBUT_TTS_OUTPUT_TRACE') == '1':
            env['MALBUT_TTS_OUTPUT_TRACE_DIR'] = str(self.out/'output-traces')
        self.spawn('ros', ['ros2','launch',str(AEC/'speech.launch.py'),'python_executable:=/usr/bin/python3',
            'stt_model_path:=/home/ubuntu/.cache/malbut_speech/models/ggml-small.bin',
            'stt_library_path:=/home/ubuntu/.cache/malbut_speech/whisper-cpp-build/bin/libmalbut_whisper.so',
            'stt_config:='+str(self.out/'stt.yaml'),'tts_config:='+str(RELEASE/'tts.yaml'),
            'manager_commands:=false','start_manager:=false',
            'agent_conversation_db:=/home/ubuntu/.local/state/malbut/speech-dialogue.sqlite3',*extra], env)
        save(self.out/'started.json', {'time':time.time(), 'status':'bridge ready and ROS launch started; dialogue readiness is separate'})

    def check_children(self):
        for role, child in self.children.items():
            if child.poll() is not None: raise RuntimeError(f'{role} exited with status {child.returncode}')

    def restore(self, stale=False):
        self.deadline = time.monotonic() + 90
        errors = []
        try:
            self.stop('ros')
            # Check both configurations and module ownership before mutating anything.
            for target, original, candidate in ((CFG,'original.ini','candidate.ini'),(VTN,'original-vtn.ini','candidate-vtn.ini')):
                if self.shell('cat '+target) not in ((self.out/original).read_text(), (self.out/candidate).read_text()):
                    raise RuntimeError('Firmware changed by another owner; refusing restoration')
            module = self.owned_module()
            backup = '/data/build/.malbut-spatial-origin-'+self.state['run']
            have_backup = self.shell(f'test -f {backup} && echo yes || true').strip() == 'yes'
            kind = self.origin_kind()
            if kind != 'regular' and not (have_backup and kind in ('fifo','missing')):
                raise RuntimeError('Unknown microphone FIFO/origin; refusing restoration')
            if kind == 'fifo':
                # An internal drain also covers a bridge blocked on a dead Pulse reader.
                # ROS is already stopped; this read is never used for recognition.
                self.spawn('recovery-reader', [str(self.adb_root/'usr/bin/adb'),'-P','5042','shell',
                    f'exec cat {ORIGIN} >/dev/null'], self.adb_env)
            self.setting('original-vtn.ini', VTN)
            if have_backup: self.shell(f'mv {backup} {ORIGIN}')
            self.setting('original.ini', CFG)
            time.sleep(2)
            for channel, gain in enumerate(self.state['original_gains'], 1):
                self.shell(f'amixer -c 1 cset name="Channel {channel} PGA Gain" {gain}')
            self.verify(original=True)
        except BaseException as error:
            errors.append(repr(error))
        finally:
            for role in ('bridge','warm','recovery-reader'):
                try: self.stop(role)
                except Exception as error: errors.append(repr(error))
            try:
                phase = self.state.get('phase_dir')
                expected = Path('/tmp') / ('malbut-phase-' + self.state['run'][4:])
                if phase is not None:
                    if Path(phase) != expected or expected.is_symlink():
                        raise RuntimeError('Invalid owned startup phase directory')
                    if expected.exists():
                        socket_path = expected/'phase.sock'
                        if socket_path.exists():
                            if not socket_path.is_socket(): raise RuntimeError('Unexpected startup phase file')
                            socket_path.unlink()
                        expected.rmdir()
            except Exception as error: errors.append(repr(error))
            try:
                module = self.owned_module()
                if module is not None: self.command('pactl','unload-module',str(module))
            except Exception as error: errors.append(repr(error))
            save(self.out/'cleanup.json', {'time':time.time(), 'restored':not errors, 'errors':errors, 'stale_recovery':stale})
        if errors: raise RuntimeError('Restoration incomplete; saved state retained: '+'; '.join(errors))
        self.active.unlink()


def run(state_dir, extra):
    # Same advisory inode used by the existing normal release. Never unlink it.
    with (AEC/'speech.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = Supervisor(state_dir)
        owner.lock_fd = lock.fileno()
        owner.recover()
        if extra == ['--restore-only']: return
        failure = None
        try:
            owner.setup(); owner.start_ros(extra)
            while True:
                owner.check_children(); time.sleep(.5)
        except BaseException as error:
            failure = error
            raise
        finally:
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(number, signal.SIG_IGN)
            if owner.state is not None:
                try: owner.restore()
                except BaseException as error:
                    print('Microphone cleanup failed: '+repr(error), file=sys.stderr, flush=True)
                    if failure is None or (isinstance(failure, SystemExit) and failure.code in (129,130,143)):
                        raise


if __name__ == '__main__':
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(number, interrupted)
    if len(sys.argv) < 2: raise SystemExit('Usage: start.sh ABSOLUTE_STATE_DIR [standard ROS launch overrides | --restore-only]')
    run(sys.argv[1], sys.argv[2:])
