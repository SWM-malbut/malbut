"""Prepare catalog speech with the same CPU Piper voice as live dialogue."""

import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from malbut_tts.piper_synthesis import PiperSynthesizer


MODEL = 'rhasspy/piper-voices/ko_KR-kss-medium'
MODEL_SHA256 = '624fd774e26895f24bebae1bd9a3379e3394baeade4b584924f83e414096e2c9'
VOICE = 'kss'
NORMALIZATION_FILES = (
    'korean_normalization.py', 'vendor/n2gk.py',
    'config/korean_pronunciation_lexicon.json',
)


def source_hashes():
    """Invalidate recordings when pronunciation rules or live synthesis change."""
    root = Path(__file__).resolve().parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (*NORMALIZATION_FILES, 'piper_synthesis.py', 'piper_notices.py')}


def notice_settings(model_path, *, threads=6):
    """Describe reproducible inputs without loading a model or making requests."""
    if model_path is None:
        raise ValueError('Piper notice preparation requires --model-path')
    model_path = Path(model_path).expanduser()
    config_path = Path(str(model_path) + '.json')
    if not model_path.is_file() or not config_path.is_file():
        raise FileNotFoundError('Piper requires the local ONNX model and configuration')
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('Piper threads must be between 1 and 64')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config['audio']['sample_rate'] != 22050:
        raise ValueError('The selected KSS voice must use 22050 Hz')
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if model_hash != MODEL_SHA256:
        raise ValueError('The local model does not match the selected Piper KSS voice')
    profile = {
        'backend': 'piper', 'provider': 'CPUExecutionProvider', 'threads': threads,
        'model_sha256': model_hash,
        'model_config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'source_sha256': source_hashes(),
        'versions': {package: version(package) for package in
                     ('piper-tts', 'onnxruntime', 'numpy', 'scipy')},
        'source_rate': 22050, 'output_rate': 24000,
        'resampling': 'scipy.signal.resample_poly:160/147',
    }
    return dict(model=MODEL, voice=VOICE, instructions=None, generation_profile=profile)


class PiperNotices:
    """Resample offline once; prerecorded playback keeps its existing 24 kHz format."""

    def __init__(self, model_path, *, threads=6):
        self.synthesizer = PiperSynthesizer(model_path, threads=threads)
        self.synthesizer.load()

    def generate(self, text, cancel_event):
        from scipy.signal import resample_poly

        chunks = self.synthesizer.generate(text, cancel_event)
        try:
            for pcm, rate in chunks:
                if rate != 22050:
                    raise ValueError('The selected KSS voice must use 22050 Hz')
                if cancel_event.is_set():
                    return
                yield resample_poly(pcm, 160, 147), 24000
        finally:
            chunks.close()
