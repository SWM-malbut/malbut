"""Validate explicitly configured perception files without running a runtime."""

import os
from pathlib import Path


def validate_perception_files(python_executable, reid_python_executable,
                              model_path, reid_model_path):
    """Return expanded paths, or report all missing files and preparation steps."""
    requested = {
        'python_executable': python_executable,
        'reid_python_executable': reid_python_executable,
        'model_path': model_path,
    }
    # The robot test runs the existing box tracker without loading OSNet.
    paths, problems = {}, []
    for name, value in requested.items():
        try:
            if not value:
                raise ValueError('No path supplied')
            # Preserve the venv's Python symlink instead of resolving to the
            # system interpreter, which would bypass that runtime environment.
            path = Path(value).expanduser().absolute()
        except (TypeError, ValueError, RuntimeError) as exc:
            problems.append(f'{name}: {value!r} ({exc})')
            continue
        paths[name] = str(path)
        if not path.is_file():
            problems.append(f'{name}: {path} (file missing or not a regular file)')
        elif name.endswith('python_executable') and not os.access(path, os.X_OK):
            problems.append(f'{name}: {path} (Python file is not executable)')
    if problems:
        raise RuntimeError(
            'Perception files are not ready:\n- ' + '\n- '.join(problems)
            + '\nRun the existing preparation scripts explicitly:\n'
            'bash "$(ros2 pkg prefix malbut_yolo)/share/malbut_yolo/scripts/'
            'prepare_runtime.sh"\n'
            'bash "$(ros2 pkg prefix malbut_reid)/share/malbut_reid/scripts/'
            'prepare_inference_runtime.sh"\n'
            'These scripts prepare the configured cache/runtime directories. '
            'For custom file paths, pass the resulting prepared paths to the '
            'launch arguments above. Bringup does not install or download files.')
    return paths
