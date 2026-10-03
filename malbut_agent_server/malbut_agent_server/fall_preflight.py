"""Read-only fall deployment preflight; never starts camera, ROS graph or Cloud.

Run as the actual runtime account after sourcing its ROS overlay. Default:
metadata only. --probe additionally imports native dependencies and loads local
Pose/optional SAM models in isolated, time-limited children; no inference.
"""

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys

from malbut_agent_server.fall_runtime import FallNodeSettings


PACKAGE_EXECUTABLES = {
    'malbut_agent_server': 'malbut-fall-monitor',
    'malbut_fall_coordinator': 'fall_coordinator',
    'malbut_system_manager': 'system_manager',
    'homecam_detector': 'homecam_detector_node',
    'homecam_media_agent': 'homecam_media_agent_node',
    'malbut_interfaces': None,
}
UNVERIFIED = [
    'credential_contents_and_cloud_authentication',
    'server_registration_settings_and_consent',
    'database_open_identity_and_schema',
    'live_camera_timestamps_and_pose_accuracy',
    'live_tracking_accuracy_and_robot_performance',
    'speech_movement_and_guardian_notification',
]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    reason: str
    action: str = ''

    def metadata(self):
        return dict(name=self.name, status=self.status, reason=self.reason, action=self.action)


def item(name, reason, action=''):
    return Check(name, 'ok' if reason == 'ok' else 'failed', reason, action)


def file_reason(path, *, executable=False, follow=True):
    try:
        info = path.stat() if follow else path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return 'not_regular_file'
        if not executable and info.st_size == 0:
            return 'empty_file'
        if not os.access(path, os.X_OK if executable else os.R_OK):
            return 'not_accessible'
        return 'ok'
    except FileNotFoundError:
        return 'missing'
    except (OSError, ValueError):
        return 'not_accessible'


def directory_reason(path):
    try:
        if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
            return 'directory_unavailable'
        return 'ok'
    except (OSError, ValueError):
        return 'directory_unavailable'


def read_settings(path):
    # Bound reads and refuse FIFOs/symlinks before reading any content.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16384:
            raise ValueError('invalid configuration file')
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ValueError('configuration too large')
        return FallNodeSettings.parse(raw.decode('utf-8'))
    finally:
        os.close(fd)


def credential_reason(path):
    # Deliberately lstat/access only: not even a one-byte read or key-file open.
    reason = file_reason(path, follow=False)
    if reason != 'ok':
        return reason
    try:
        info = path.lstat()
        if info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & 0o027:
            return 'unsafe_owner_or_permissions'
        if info.st_size > 4096:
            return 'too_large'
        return 'ok'
    except (OSError, ValueError):
        return 'not_accessible'


def key_directory_reason(path):
    """Server-synced keys are replaced in place: only this account may write there."""
    try:
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return 'not_owned'
        if stat.S_IMODE(info.st_mode) & 0o022:
            return 'writable_by_others'
        return 'ok' if os.access(path.parent, os.W_OK | os.X_OK) else 'not_writable'
    except (OSError, ValueError):
        return 'not_accessible'


def journal_checks(path):
    hint = '실행 계정 소유 0700 디렉터리와 0600 DB 파일을 준비하세요. 기존 DB는 삭제하지 마세요.'
    try:
        parent = path.parent.lstat()
        parent_ok = (stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
                     and not stat.S_IMODE(parent.st_mode) & 0o077
                     and os.access(path.parent, os.W_OK | os.X_OK))
    except (OSError, ValueError):
        parent_ok = False
    result = [item('journal_directory', 'ok' if parent_ok else 'private_directory_required', hint)]
    try:
        info = path.lstat()
    except FileNotFoundError:
        result.append(Check('journal_file', 'ok', 'will_be_created_by_runtime',
                            '검사에서는 DB를 만들지 않았습니다.'))
        return result
    except (OSError, ValueError):
        result.append(item('journal_file', 'not_accessible', hint))
        return result
    valid = (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and not stat.S_IMODE(info.st_mode) & 0o077
             and os.access(path, os.R_OK | os.W_OK))
    result.append(item('journal_file', 'ok' if valid else 'unsafe_or_inaccessible', hint))
    return result


def package_prefix(name):
    from ament_index_python.packages import get_package_prefix
    return Path(get_package_prefix(name))


def runtime_environment():
    # Keep paths needed by the sourced ROS overlay, not provider credentials.
    keys = ('PATH', 'LANG', 'LC_ALL', 'ROS_DISTRO', 'AMENT_PREFIX_PATH',
            'CMAKE_PREFIX_PATH', 'LD_LIBRARY_PATH', 'PYTHONPATH')
    env = {key: os.environ[key] for key in keys if key in os.environ}
    package_root = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (package_root, env.get('PYTHONPATH'))))
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


async def run_probe(command, env, *, timeout_s=30):
    """EOF input, bounded stdout, no inherited stdin, stderr or secret values."""
    process = None
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, limit=4096))
    try:
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await launch
            raise
        async def receive():
            try:
                # read() may return a partial write before EOF. Accumulate up
                # to the bound, never an unbounded communicate()/read().
                await process.stdout.readexactly(4097)
                return 'invalid_probe_output'
            except asyncio.IncompleteReadError as exc:
                output = exc.partial
            code = await process.wait()
            return ('ok' if code == 0 and output == b'{"ready":true}\n'
                    else 'dependencies_or_model_failed')
        return await asyncio.wait_for(receive(), timeout_s)
    except asyncio.TimeoutError:
        return 'probe_timeout'
    except (OSError, ValueError):
        return 'probe_unavailable'
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Drain after killing an over-producing child so paused pipe
            # transports cannot prevent process.wait() from completing.
            while await process.stdout.read(4096):
                pass
            await process.wait()


async def inspect(config, pose_model, pose_python, *, probe=False, prefix_lookup=package_prefix,
                  probe_runner=run_probe, pose_execution_provider='auto',
                  pose_intra_op_num_threads=2, pose_allow_spinning=False,
                  pose_opencv_num_threads=1):
    checks, settings = [], None
    try:
        settings = read_settings(config)
        checks.append(item('configuration', 'ok'))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        checks.append(item('configuration', 'invalid_or_missing',
                           '실제 로봇용 fall_runtime.json 경로와 필수 항목을 확인하세요.'))
    if settings is not None:
        checks.append(item('device_id', 'placeholder' if settings.device_id ==
                           'REPLACE_WITH_REGISTERED_DEVICE_ID' else 'ok',
                           '예시 ID가 아닌 서버에 등록된 로봇 ID를 지정하세요.'))
        key_reason = credential_reason(settings.cloud_key_file)
        if settings.key_sync is not None and key_reason == 'missing':
            key_reason = 'ok'  # The owner's key arrives from the server.
        checks.append(item('cloud_key_metadata', key_reason,
                           'Ollama 키 파일을 실행 계정 소유 0600으로 준비하세요. 키 내용은 검사하지 않습니다.'))
        if settings.key_sync is not None:
            token = credential_reason(settings.key_sync.token_file)
            checks.append(item('key_sync_token_metadata', token,
                               '기기 토큰 파일을 실행 계정 소유 0600으로 준비하세요.'))
            directory = key_directory_reason(settings.cloud_key_file)
            checks.append(item('key_sync_key_directory', directory,
                               '키 파일 디렉터리를 실행 계정 소유로, 다른 계정은 쓸 수 없게(예: 0700) 준비하세요.'))
        checks.extend(journal_checks(settings.journal_path))
    for package, executable in PACKAGE_EXECUTABLES.items():
        try:
            prefix = prefix_lookup(package)
            reason = directory_reason(prefix / 'share' / package)
            if reason == 'ok' and executable:
                reason = file_reason(prefix / 'lib' / package / executable, executable=True)
        except (ImportError, LookupError, OSError, ValueError):
            reason = 'package_not_installed'
        checks.append(item('package.' + package, reason,
                           '필요한 ROS 패키지를 빌드하고 해당 overlay의 setup.bash를 불러오세요.'))
    pose_ready = True
    for name, path, executable in (('pose_model', pose_model, False),
                                    ('pose_python', pose_python, True)):
        reason = file_reason(path, executable=executable)
        pose_ready &= reason == 'ok'
        checks.append(item(name, reason, 'Bringup과 같은 Pose 모델·Python 경로를 지정하세요.'))
    tracking = settings.tracking if settings else None
    tracking_ready = tracking is not None
    if tracking is None:
        checks.append(Check('tracking', 'skipped',
                            'disabled' if settings is not None else 'configuration_unavailable'))
    else:
        for name, path, executable in (
                ('tracking_python', Path(tracking.python_executable), True),
                ('tracking_checkpoint', Path(tracking.checkpoint_path), False)):
            reason = file_reason(path, executable=executable)
            tracking_ready &= reason == 'ok'
            checks.append(item(name, reason, 'tracking 설정의 로컬 파일·실행 권한을 확인하세요.'))
        for i, path in enumerate((tracking.source_path, *tracking.python_paths)):
            reason = directory_reason(Path(path))
            tracking_ready &= reason == 'ok'
            checks.append(item('tracking_source' if i == 0 else f'tracking_dependency_{i}',
                               reason, '검증한 SAM 소스와 의존성 디렉터리를 준비하세요.'))
    for name, enabled, command, env in (
        ('runtime_probe', True,
         [sys.executable, '-m', 'malbut_agent_server.fall_preflight_probe', 'runtime'],
         runtime_environment()),
        ('pose_probe', pose_ready,
         [str(pose_python), '-m', 'malbut_agent_server.fall_preflight_probe',
          'pose', '--model', str(pose_model),
          '--pose-execution-provider', pose_execution_provider,
          '--pose-intra-op-num-threads', str(pose_intra_op_num_threads),
          '--pose-allow-spinning', 'true' if pose_allow_spinning else 'false',
          '--pose-opencv-num-threads', str(pose_opencv_num_threads)], runtime_environment()),
    ):
        if probe and enabled:
            checks.append(item(name, await probe_runner(command, env),
                               '실행 Python의 ROS/native 의존성, 요청한 CUDA/CPU 제공자와 '
                               'Pose ONNX 형식을 확인하세요.'))
        else:
            checks.append(Check(name, 'skipped', 'not_requested' if not probe else 'files_unavailable'))
    if tracking is not None:
        if probe and tracking_ready:
            from malbut_agent_server.adapters.outbound.sam_tracking import worker_environment
            command = [tracking.python_executable, '-m',
                       'malbut_agent_server.adapters.outbound.sam_tracking_worker',
                       '--source', tracking.source_path, '--checkpoint', tracking.checkpoint_path]
            checks.append(item('tracking_probe', await probe_runner(
                command, worker_environment(tracking)),
                '고정된 SAM 소스·체크포인트, CUDA/BF16 및 Python 의존성을 확인하세요.'))
        else:
            checks.append(Check('tracking_probe', 'skipped',
                                'not_requested' if not probe else 'files_unavailable'))
    return dict(schema_version=1, event='fall_preflight',
                scope='local_model_loading' if probe else 'file_metadata',
                checks_passed=all(c.status != 'failed' for c in checks),
                deployment_verified=False,
                key_contents_read=False, cloud_requests=0, camera_started=False,
                journal_opened=False, checks=[c.metadata() for c in checks],
                unverified=UNVERIFIED)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))).expanduser()
    parser.add_argument('--config', default=os.environ.get(
        'MALBUT_FALL_CONFIG', '/etc/malbut/fall_runtime.json'), type=Path)
    parser.add_argument('--pose-model', type=Path, default=os.environ.get(
        'MALBUT_FALL_POSE_MODEL', str(cache / 'malbut_perception/yolo26s-pose.onnx')))
    parser.add_argument('--pose-python', type=Path, default=os.environ.get(
        'MALBUT_FALL_POSE_PYTHON', str(cache / 'malbut_fall_pose/runtime/bin/python')))
    parser.add_argument('--probe', action='store_true',
                        help='Load local dependencies/models without images, ROS init or Cloud.')
    parser.add_argument('--pose-execution-provider', choices=('auto', 'cpu', 'cuda'), default='auto',
                        help='Match Bringup; use cuda to require a working CUDA session.')
    parser.add_argument('--pose-intra-op-num-threads', type=int, default=2)
    parser.add_argument('--pose-allow-spinning', choices=('true', 'false'), default='false')
    parser.add_argument('--pose-opencv-num-threads', type=int, default=1)
    parser.add_argument('--json', action='store_true', help='Print only the bounded report schema.')
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(inspect(args.config.expanduser(), args.pose_model.expanduser(),
                                     args.pose_python.expanduser(), probe=args.probe,
                                     pose_execution_provider=args.pose_execution_provider,
                                     pose_intra_op_num_threads=args.pose_intra_op_num_threads,
                                     pose_allow_spinning=args.pose_allow_spinning == 'true',
                                     pose_opencv_num_threads=args.pose_opencv_num_threads))
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print('낙상 실행 준비 검사 — 실제 실행 계정에서 사용하세요.')
        for check in report['checks']:
            print(f"[{check['status'].upper()}] {check['name']}: {check['reason']}")
            if check['status'] == 'failed' and check['action']:
                print('  ' + check['action'])
        print('키 내용·API 인증·실제 카메라·서버 설정·동시 성능은 확인하지 않았습니다.')
        print('요청한 검사 통과' if report['checks_passed'] else '준비가 필요한 항목이 있습니다.')
    return 0 if report['checks_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
