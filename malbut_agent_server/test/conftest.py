"""Expose the sibling coordinator source for ROS-independent handoff tests."""

from pathlib import Path
import sys


# CI runs this package without installing the ROS workspace. Append, rather
# than prepend, so an explicitly selected deployment mirror keeps precedence.
sys.path.append(str(Path(__file__).resolve().parents[2] / 'malbut_fall_coordinator'))
