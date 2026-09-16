"""Device credential loading with systemd file support."""

import os
from pathlib import Path
import re
from typing import Mapping


_TOKEN_PATTERN = re.compile(
    r"^hc1\."
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-"
    r"[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\."
    r"[0-9a-fA-F]{64}$"
)


def is_valid_device_token(value: str) -> bool:
    """Return whether a value is safe and matches the backend token format."""
    return _TOKEN_PATTERN.fullmatch(value) is not None


def load_device_token(environ: Mapping[str, str] = os.environ) -> str:
    """Load a bounded token file, or use the development-only env fallback."""
    token_file = environ.get("HOMECAM_DEVICE_TOKEN_FILE", "")
    if token_file:
        try:
            with Path(token_file).open("rb") as input_file:
                raw_token = input_file.read(4097)
        except (OSError, UnicodeError):
            return ""
        if len(raw_token) > 4096:
            return ""
        try:
            token = raw_token.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            return ""
        return token if is_valid_device_token(token) else ""
    token = environ.get("HOMECAM_DEVICE_TOKEN", "")
    return token if is_valid_device_token(token) else ""
