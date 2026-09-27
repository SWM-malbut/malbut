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
                # Same model shape/CPU provider validation as the real Pose node.
                PersonPoseEstimator(args.model, keep_aspect=True)
        output.write('{"ready":true}\n')
        output.flush()
        return 0
    except Exception:
        # Third-party paths/config/model diagnostics never escape to the report.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
