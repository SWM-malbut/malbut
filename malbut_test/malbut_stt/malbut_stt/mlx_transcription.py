"""Optional Apple Silicon Whisper backend using downloaded MLX model assets."""

from pathlib import Path
import platform
from types import SimpleNamespace

from malbut_stt.transcription import LocalWhisperTranscriber


def _require_local_assets(path):
    if not path.is_dir():
        raise ValueError('MLX Whisper model must be an existing local directory')
    if not all((path / name).is_file() for name in ('config.json', 'weights.npz')):
        raise ValueError('local MLX Whisper model requires config.json and weights.npz')


class _MlxModelAdapter:
    """Adapt serialized greedy/fallback decoding to the local segment interface.

    mlx-whisper 0.4.3 uses a process-global ModelHolder cache keyed only by path.
    Use one ASR worker and one MLX model path/dtype per process. Sharing this
    adapter between wake and command recognition keeps that model loaded.
    """

    compute_type = 'float16'
    beam_search = False
    decoding = 'greedy at T=0; best_of=5 for sampling fallback'

    def __init__(self, path):
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder, transcribe

        self.path = path
        self.mx = mx
        self.decode = transcribe
        mx.set_default_device(mx.gpu)
        loaded = ModelHolder.get_model(str(path), mx.float16)
        # Private constant buffers are lazy too; realize them on their creation thread.
        mx.eval(loaded.state)
        mx.synchronize()
        if loaded.encoder.conv1.weight.dtype != mx.float16:
            raise ValueError('MLX notebook backend requires fp16 model weights')

    def transcribe(self, audio, *, language, beam_size, condition_on_previous_text,
                   initial_prompt):
        if beam_size != 1:
            raise ValueError('MLX Whisper supports greedy decoding, not beam search')
        _require_local_assets(self.path)
        self.mx.synchronize()
        try:
            result = self.decode(
                audio, path_or_hf_repo=str(self.path), language=language,
                task='transcribe', condition_on_previous_text=condition_on_previous_text,
                initial_prompt=initial_prompt, fp16=True, best_of=5, verbose=None,
            )
        finally:
            self.mx.synchronize()
        return (SimpleNamespace(text=segment['text']) for segment in result['segments']), None


class MlxWhisperTranscriber(LocalWhisperTranscriber):
    """Reuse local PCM validation with optional fp16 MLX inference; no API fallback."""

    backend = 'mlx'

    def __init__(self, model_path):
        path = Path(model_path).expanduser().resolve()
        _require_local_assets(path)
        if platform.system() != 'Darwin' or platform.machine() != 'arm64':
            raise RuntimeError('MLX Whisper requires Apple Silicon macOS')
        # Tokenizer vocabulary ships with mlx-whisper; no hosted tokenizer is used.
        self.model = _MlxModelAdapter(path)
