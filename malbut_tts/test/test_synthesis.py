"""Verify lazy local synthesis and discarded results after cancellation."""

from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from malbut_tts.audio import PlaybackCancelled
from malbut_tts.synthesis import MlxSynthesizer


def chunk(values=(0.1, 0.2), *, tokens=6, rate=24000):
    """Represent one streaming result from the installed MLX model API."""
    return SimpleNamespace(
        audio=np.array(values), token_count=tokens, sample_rate=rate,
    )


class MlxSynthesizerTests(unittest.TestCase):
    """Test synthesis contracts with a model that never needs a GPU."""

    def setUp(self):
        """Replace optional MLX imports with a locally loaded fake model."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.cancel = Event()
        self.model = SimpleNamespace(generate_custom_voice=Mock())
        self.load = Mock(return_value=self.model)
        modules = {
            'mlx.core': SimpleNamespace(eval=Mock()),
            'mlx_audio.tts.utils': SimpleNamespace(load_model=self.load),
        }
        mocked = patch(
            'malbut_tts.synthesis.importlib.import_module',
            side_effect=modules.__getitem__,
        )
        self.import_module = mocked.start()
        self.addCleanup(mocked.stop)
        self.engine = MlxSynthesizer(str(self.path))

    def supply(self, chunks, after_yield=None):
        """Track closure of a model iterator, including exceptional exits."""
        state = {'closed': False}

        def generate(**kwargs):
            try:
                for result in chunks:
                    yield result
                    if after_yield is not None:
                        after_yield()
            finally:
                state['closed'] = True

        self.model.generate_custom_voice.side_effect = generate
        return state

    def test_lazy_model_reuse_and_whole_text_streaming(self):
        """One local model handles requests without altering original text."""
        state = self.supply([chunk(), chunk((0.3,))])
        self.import_module.assert_not_called()
        text = '  안녕하세요. 다음 문장도 그대로 읽어 줘요.  '
        results = list(self.engine.generate(text, self.cancel))
        self.assertTrue(state['closed'])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0].dtype, np.float32)
        self.assertEqual(results[0][1], 24000)
        self.model.generate_custom_voice.assert_called_with(
            text=text, speaker='Sohee', language='Korean', instruct=None,
            stream=True, streaming_interval=0.5, max_tokens=1024,
        )
        list(self.engine.generate('새 요청', self.cancel))
        self.load.assert_called_once_with(str(self.path.resolve()))

    def test_missing_local_path_never_imports_or_downloads(self):
        """A nonexistent model path fails before invoking an MLX loader."""
        engine = MlxSynthesizer(str(self.path / 'missing'))
        with self.assertRaises(FileNotFoundError):
            next(engine.generate('안녕', self.cancel))
        self.import_module.assert_not_called()

    def test_cancel_before_start_does_not_load_model(self):
        """An already cancelled request performs no model work."""
        self.cancel.set()
        with self.assertRaises(PlaybackCancelled):
            next(self.engine.generate('안녕', self.cancel))
        self.import_module.assert_not_called()

    def test_cancellation_during_inference_discards_late_chunk(self):
        """The result produced by in-flight inference never reaches output."""
        state = {'closed': False}

        def generate(**kwargs):
            try:
                self.cancel.set()
                yield chunk()
            finally:
                state['closed'] = True

        self.model.generate_custom_voice.side_effect = generate
        with self.assertRaises(PlaybackCancelled):
            next(self.engine.generate('안녕', self.cancel))
        self.assertTrue(state['closed'])

    def test_cancel_before_next_inference_closes_generator(self):
        """Cancellation at a chunk boundary does not request another chunk."""
        after_yield = Mock()
        state = self.supply([chunk(), chunk()], after_yield)
        iterator = self.engine.generate('안녕', self.cancel)
        next(iterator)
        self.cancel.set()
        with self.assertRaises(PlaybackCancelled):
            next(iterator)
        after_yield.assert_not_called()
        self.assertTrue(state['closed'])

    def test_token_limit_fails_instead_of_finishing_truncated_audio(self):
        """Per-chunk token deltas are summed to detect the model limit."""
        state = self.supply([chunk(tokens=600), chunk(tokens=424)])
        iterator = self.engine.generate('긴 답변', self.cancel)
        next(iterator)
        with self.assertRaisesRegex(RuntimeError, 'generation limit'):
            next(iterator)
        self.assertTrue(state['closed'])

    def test_empty_invalid_or_rate_changing_output_fails(self):
        """Invalid generation cannot be mistaken for complete speech."""
        cases = [
            ([], 'no audio'),
            ([chunk((float('nan'),))], 'invalid PCM'),
            ([chunk(), chunk(rate=16000)], 'changed its sample rate'),
        ]
        for chunks, message in cases:
            with self.subTest(message=message):
                state = self.supply(chunks)
                with self.assertRaisesRegex(RuntimeError, message):
                    list(self.engine.generate('안녕', self.cancel))
                self.assertTrue(state['closed'])
