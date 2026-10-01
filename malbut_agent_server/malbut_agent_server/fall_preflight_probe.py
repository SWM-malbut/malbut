"""Internal local import/model-load probe. No ROS init, image input or network."""

import argparse
import contextlib
import sys


def runtime_imports():
    import aiohttp  # noqa: F401
    import cv2  # noqa: F401
    import cv_bridge  # noqa: F401
    import PIL.Image  # noqa: F401
    from rclpy.type_support import check_for_type_support
    from sensor_msgs.msg import Image
    from std_msgs.msg import String
    from malbut_interfaces.msg import (
        FallRuntimeStatus, FallControlHeartbeat, FallSettingsSnapshot, FallSettingsReport,
    )
    from malbut_interfaces.srv import ApplyFallSettings
    from malbut_interfaces.action import ConfirmSituation, ExecuteMission
    for message in (Image, String, FallRuntimeStatus, FallControlHeartbeat,
                    FallSettingsSnapshot, FallSettingsReport, ApplyFallSettings,
                    ConfirmSituation, ExecuteMission):
        check_for_type_support(message)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('runtime', 'pose'))
    parser.add_argument('--model')
    parser.add_argument('--pose-execution-provider', choices=('auto', 'cpu', 'cuda'), default='auto')
    parser.add_argument('--pose-intra-op-num-threads', type=int, default=2)
    parser.add_argument('--pose-allow-spinning', choices=('true', 'false'), default='false')
    parser.add_argument('--pose-opencv-num-threads', type=int, default=1)
    args = parser.parse_args(argv)
    output = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.phase == 'runtime':
                runtime_imports()
            else:
                import cv_bridge  # noqa: F401
                import cv2  # noqa: F401
                import rclpy  # noqa: F401
                from homecam_detector.pose import PersonPoseEstimator
                if not 0 <= args.pose_opencv_num_threads <= 256:
                    raise ValueError('Invalid OpenCV thread count')
                if args.pose_opencv_num_threads:
                    cv2.setNumThreads(args.pose_opencv_num_threads)
                # Match the dedicated node. Explicit CUDA must never pass by
                # checking only a CPU session in this same interpreter.
                PersonPoseEstimator(
                    args.model, keep_aspect=True,
                    execution_provider=args.pose_execution_provider,
                    intra_op_num_threads=args.pose_intra_op_num_threads,
                    allow_spinning=args.pose_allow_spinning == 'true')
        output.write('{"ready":true}\n')
        output.flush()
        return 0
    except Exception:
        # Third-party paths/config/model diagnostics never escape to the report.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
