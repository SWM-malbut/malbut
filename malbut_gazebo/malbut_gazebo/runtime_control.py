"""Exchange bounded runtime-mode requests with the device supervisor."""

from __future__ import annotations

import math
import os
from pathlib import Path
import secrets
import time


RUNTIME_MODES = {"mapping", "navigation"}


def write_runtime_request(
    path: Path | None,
    mode: str,
    *,
    delay_seconds: float = 0.0,
) -> bool:
    """Atomically ask the external supervisor to switch one ROS stack."""
    if path is None:
        return False
    if mode not in RUNTIME_MODES:
        raise ValueError(f"unsupported runtime mode: {mode}")
    target = path.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    not_before = math.ceil(time.time() + max(0.0, delay_seconds))
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        temporary.write_text(
            f"{mode} {not_before}\n", encoding="ascii"
        )
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return True
