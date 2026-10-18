"""Describe an explicitly verified private Piper voice for offline notices."""

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re

from malbut_tts.piper_notices import source_hashes


def custom_notice_settings(model_path, expected_sha256, *, threads=6):
    """Keep the default KSS profile intact; identify custom voices by content."""
    if not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise ValueError('Expected Piper model SHA-256 must contain 64 hex digits')
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('Piper threads must be between 1 and 64')
    model_path = Path(model_path).expanduser()
    config_path = Path(str(model_path) + '.json')
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if model_hash != expected_sha256.lower():
        raise ValueError('The local Piper model does not match the expected SHA-256')
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config['audio']['sample_rate'] != 22050:
        raise ValueError('Custom notice voices must use 22050 Hz')
    sources = source_hashes()
    sources['custom_piper_notices.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    profile = {
        'backend': 'piper', 'provider': 'CPUExecutionProvider', 'threads': threads,
        'model_sha256': model_hash,
        'model_config_sha256': hashlib.sha256(config_bytes).hexdigest(),
        'source_sha256': sources,
        'versions': {package: version(package) for package in
                     ('piper-tts', 'onnxruntime', 'numpy', 'scipy')},
        'source_rate': 22050, 'output_rate': 24000,
        'resampling': 'scipy.signal.resample_poly:160/147',
    }
    return dict(model=f'local-piper/sha256/{model_hash}', voice='custom',
                instructions=None, generation_profile=profile)
