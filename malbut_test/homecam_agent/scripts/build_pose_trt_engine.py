#!/usr/bin/env python3
"""Build the FP16 TensorRT engine the fall pose node loads instead of ONNX Runtime.

Run once per robot, with Bringup stopped: the build takes about ten minutes and
over 2 GB of memory on an Orin NX. An engine only works with the TensorRT
version and GPU it was built on, so do not copy it between robots.
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'homecam_detector'))
from homecam_detector.trt_session import TensorRTSession, engine_path_for  # noqa: E402


def build(model: Path, engine: Path, workspace_mb: int) -> None:
    import tensorrt as trt

    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)
    network = builder.create_network(0)
    parser = trt.OnnxParser(network, logger)
    if not parser.parse_from_file(str(model)):
        errors = '; '.join(str(parser.get_error(i)) for i in range(parser.num_errors))
        raise RuntimeError(f'TensorRT cannot parse {model}: {errors}')
    if not builder.platform_has_fast_fp16:
        raise RuntimeError('this GPU has no fast FP16 support')
    config = builder.create_builder_config()
    config.set_flag(trt.BuilderFlag.FP16)
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, workspace_mb << 20)
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError('TensorRT engine build failed')
    # Never leave a half-written engine where the node would pick it up.
    handle, temporary = tempfile.mkstemp(dir=engine.parent, prefix='.' + engine.name + '.')
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(bytes(serialized))
        TensorRTSession(temporary).close()
        os.replace(temporary, engine)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, default=Path(os.environ.get(
        'MALBUT_FALL_POSE_MODEL',
        Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
        / 'malbut_perception' / 'yolo26s-pose.onnx')))
    parser.add_argument('--workspace-mb', type=int, default=1024)
    parser.add_argument('--force', action='store_true', help='rebuild an existing engine')
    args = parser.parse_args()
    model = args.model.expanduser()
    if not model.is_file():
        print(f'Pose ONNX model not found: {model}', file=sys.stderr)
        return 1
    engine = engine_path_for(model)
    if engine.is_file() and engine.stat().st_mtime >= model.stat().st_mtime and not args.force:
        print(f'Engine is already up to date: {engine}')
        return 0
    print(f'Building {engine} (about ten minutes; keep Bringup stopped)...', flush=True)
    build(model, engine, args.workspace_mb)
    print(f'Built {engine} ({engine.stat().st_size >> 20} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
