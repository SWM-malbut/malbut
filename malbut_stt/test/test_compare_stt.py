"""Protect comparison cost and input identity without model or API calls."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest


def write_wav(path, pcm):
    """Keep fixture samples valid mono PCM16 at the comparison sample rate."""
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm)


@pytest.fixture
def comparison(tmp_path, monkeypatch):
    """Load the tool without optional SDKs and freeze two distinct PCM inputs."""
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    for name in ('openai', 'faster_whisper', 'numpy'):
        monkeypatch.setitem(sys.modules, name, None)
    path = Path(__file__).resolve().parents[1] / 'tools' / 'compare_stt.py'
    spec = importlib.util.spec_from_file_location('compare_stt_test_subject', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / 'experiment'
    output.mkdir()
    pcms = [b'\x01\x00' * 320, b'\x02\x00' * 320]
    cases = []
    for index, pcm in enumerate(pcms):
        name = f'case-{index}.wav'
        write_wav(output / name, pcm)
        cases.append({'id': f'case-{index}', 'wav': name,
                      'pcm_sha256': hashlib.sha256(pcm).hexdigest()})
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps([{'id': case['id']} for case in cases]))
    metadata = {'corpus_sha256': hashlib.sha256(corpus.read_bytes()).hexdigest(),
                'cases': cases}
    module.write_json(output / 'experiment.json', metadata)
    return SimpleNamespace(module=module, output=output, metadata=metadata,
                           corpus=corpus, pcms=pcms)


@pytest.mark.parametrize('failed', [False, True])
def test_attempted_engine_is_never_called_again_after_success_or_failure(
    comparison, capsys, failed,
):
    """An attempt is recorded before calling the boundary, including failures."""
    metadata = {'cases': comparison.metadata['cases'][:1]}
    attempts = comparison.output / 'attempts.jsonl'
    results = comparison.output / 'results.jsonl'
    calls = []

    def transcribe(pcm):
        assert json.loads(attempts.read_text())['id'] == 'case-0'
        calls.append(pcm)
        if failed:
            raise RuntimeError('synthetic request failure')
        return '제이크야'

    comparison.module.run(comparison.output, metadata, 'openai', transcribe)
    assert calls == comparison.pcms[:1]
    assert json.loads(results.read_text())['status'] == ('error' if failed else 'ok')
    original = attempts.read_bytes(), results.read_bytes()
    capsys.readouterr()

    def forbidden_retry(pcm):
        pytest.fail('an attempted API case must not be submitted again')

    comparison.module.run(comparison.output, metadata, 'openai', forbidden_retry)
    assert (attempts.read_bytes(), results.read_bytes()) == original
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('error_name', ['AuthenticationError', 'RateLimitError'])
def test_credential_or_quota_error_stops_remaining_cases_without_details(
    comparison, capsys, error_name,
):
    """A global API rejection makes one attempt and records only its class."""
    calls = []
    error_type = type(error_name, (Exception,), {})

    def transcribe(pcm):
        calls.append(pcm)
        raise error_type('PRIVATE-API-ERROR-BODY')

    comparison.module.run(comparison.output, comparison.metadata, 'openai', transcribe)
    assert calls == comparison.pcms[:1]
    attempts = (comparison.output / 'attempts.jsonl').read_text()
    results = (comparison.output / 'results.jsonl').read_text()
    assert len(attempts.splitlines()) == len(results.splitlines()) == 1
    row = json.loads(results)
    assert row['id'] == 'case-0' and row['status'] == 'error'
    assert row['error'] == error_name
    output = capsys.readouterr()
    assert 'PRIVATE-API-ERROR-BODY' not in attempts + results + output.out + output.err


def test_prepare_rejects_changed_pcm_in_an_existing_experiment(comparison):
    """A valid WAV container cannot hide changed samples behind old metadata."""
    assert comparison.module.prepare(comparison.corpus, comparison.output) == comparison.metadata
    path = comparison.output / comparison.metadata['cases'][0]['wav']
    changed = b'\x03\x00' + comparison.pcms[0][2:]
    write_wav(path, changed)
    with pytest.raises(ValueError, match='waveform changed'):
        comparison.module.prepare(comparison.corpus, comparison.output)
