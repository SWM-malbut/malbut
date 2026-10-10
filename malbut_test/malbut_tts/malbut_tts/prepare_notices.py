"""Generate finite notice WAVs once, or validate them without an API call."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
import wave

import numpy as np

from malbut_tts.prerecorded import PrerecordedAudio, valid_audio_id


def read_catalog(path):
    """Validate the complete catalog before creating files or making requests."""
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or not value or len(value) > 1000
            or not all(valid_audio_id(key) and isinstance(text, str)
                       and text.strip() and len(text) <= 500
                       for key, text in value.items())):
        raise ValueError('catalog must map audio IDs to nonblank, bounded transcripts')
    return value


def prepare(catalog, directory, synthesizer=None, *, model='gpt-4o-mini-tts',
            voice='marin', check_only=False):
    """Resume matching WAVs; never silently reuse stale or changed recordings."""
    directory = Path(directory)
    manifest_path = directory / 'manifest.json'
    manifest = (json.loads(manifest_path.read_text(encoding='utf-8'))
                if manifest_path.exists() else {})
    reader = PrerecordedAudio(directory)
    generated = 0
    for audio_id, text in sorted(catalog.items()):
        if not valid_audio_id(audio_id) or not isinstance(text, str) or not text.strip():
            raise ValueError('invalid notice catalog')
        path = directory / (audio_id + '.wav')
        expected = {'text': text, 'model': model, 'voice': voice}
        entry = manifest.get(audio_id, {})
        if path.exists():
            if (any(entry.get(key) != value for key, value in expected.items())
                    or entry.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
                raise ValueError(f'{audio_id}: existing recording does not match the catalog')
            list(reader.generate(audio_id, Event()))
            continue
        if check_only:
            raise ValueError(f'{audio_id}: recording is missing')
        if synthesizer is None:
            raise ValueError('a synthesizer is required for generation')
        directory.mkdir(parents=True, exist_ok=True)
        chunks = synthesizer.generate(text, Event())
        parts = []
        frames = 0
        try:
            while True:
                try:
                    audio, sample_rate = next(chunks)
                except StopIteration as complete:
                    if complete.value == 'notice_substituted':
                        raise ValueError('API error audio cannot become a notice recording')
                    break
                audio = np.asarray(audio)
                frames += audio.size
                if (sample_rate != 24000 or audio.ndim != 1
                        or not np.isfinite(audio).all()
                        or frames > 24000 * reader.max_seconds):
                    raise ValueError('generated notice has invalid or oversized PCM')
                parts.append((np.clip(audio, -1, 32767 / 32768) * 32768).astype('<i2').tobytes())
        finally:
            chunks.close()
        if frames == 0:
            raise ValueError('generated notice has no audio')
        with tempfile.NamedTemporaryFile(dir=directory, suffix='.wav', delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with wave.open(str(temporary_path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b''.join(parts))
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        manifest[audio_id] = {**expected, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        temporary_manifest = directory / 'manifest.json.tmp'
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        temporary_manifest.replace(manifest_path)
        generated += 1
        print(f'{audio_id}: prepared ({frames / 24000:.1f}s)', flush=True)
    return generated


def main(argv=None):
    """Use only an existing process key; do not accept secrets as CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--model', default='gpt-4o-mini-tts')
    parser.add_argument('--voice', default='marin')
    parser.add_argument('--check', action='store_true',
                        help='Validate all files without using TTS')
    args = parser.parse_args(argv)
    try:
        catalog = read_catalog(args.catalog)
        synthesizer = None
        if not args.check:
            from malbut_tts.api_synthesis import OpenAISynthesizer
            synthesizer = OpenAISynthesizer(model=args.model, voice=args.voice, timeout_seconds=30)
            synthesizer.load()
        count = prepare(catalog, args.output_dir, synthesizer, model=args.model,
                        voice=args.voice, check_only=args.check)
    except Exception as error:
        # API response details and credentials never reach console output.
        print(f'Notice preparation failed: {type(error).__name__}')
        return 1
    print(f'{len(catalog)} notices verified; {count} generated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
