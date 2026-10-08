"""Identify the installed Python source without disclosing machine paths."""

import hashlib
import os
from pathlib import Path
import re


def reported_source_revision(environ=None):
    """Return an unverified build label; never infer deployment identity from Git."""
    value = (os.environ if environ is None else environ).get('MALBUT_SOURCE_SHA', '')
    return value.lower() if re.fullmatch(r'[0-9a-fA-F]{40}', value) else 'unknown'


def runtime_source_fingerprint(directory=None):
    """Hash relative Python filenames and bytes of this installed STT package."""
    root = Path(__file__).parent if directory is None else Path(directory)
    digest = hashlib.sha256()
    paths = sorted(root.rglob('*.py'), key=lambda path: path.relative_to(root).as_posix())
    if not paths:
        return 'unknown'
    for path in paths:
        name = path.relative_to(root).as_posix().encode('utf-8')
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big') + name)
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()
