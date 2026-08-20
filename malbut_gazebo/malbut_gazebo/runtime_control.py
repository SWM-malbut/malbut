"""Exchange bounded runtime-mode requests with the device supervisor."""

from __future__ import annotations

import math
import os
from pathlib import Path
import secrets
import time


RUNTIME_MODES = {"mapping", "navigation"}

# 감독자는 요청 파일 옆에 이 접미사로 자기 PID 와 갱신 시각을 남긴다.
SUPERVISOR_SUFFIX = ".supervisor"
# 감독 루프는 이보다 훨씬 자주 갱신한다. 넉넉히 잡아 일시적인 지연을
# 감독자 부재로 오판하지 않는다.
SUPERVISOR_MAX_AGE_S = 30.0


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


def supervisor_path(path: Path | None) -> Path | None:
    """Return the liveness file the supervisor maintains for one request."""
    if path is None:
        return None
    target = path.expanduser().resolve()
    return target.with_name(target.name + SUPERVISOR_SUFFIX)


def supervisor_available(
    path: Path | None,
    *,
    max_age_seconds: float = SUPERVISOR_MAX_AGE_S,
) -> bool:
    """
    Report whether a live supervisor is consuming this request file.

    A request nobody consumes is worse than a refused one: the caller
    reports success and the runtime never switches. Callers check this
    before promising a mode change.
    """
    liveness = supervisor_path(path)
    if liveness is None:
        return False
    try:
        raw = liveness.read_text(encoding="ascii")
    except OSError:
        return False
    fields = raw.split()
    if len(fields) != 2:
        return False
    try:
        pid = int(fields[0])
        refreshed_at = float(fields[1])
    except ValueError:
        return False
    if pid <= 0:
        return False
    if time.time() - refreshed_at > max(0.0, max_age_seconds):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 다른 사용자의 프로세스라도 살아 있다는 뜻이다.
        return True
    except OSError:
        return False
    return True
