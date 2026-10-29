from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
from malbut_stt.echo.startup_phase_estimator import estimate, _spectrum, OMEGA


@pytest.fixture
def signals(tmp_path):
    rng = np.random.default_rng(71)
    render = rng.normal(0, .08, 64000).astype('f4')
    raw = render[:, None] * np.array([.8, 1., .7, .9, 1.1, .75], 'f4')
    s = _spectrum(raw)
    cov = np.einsum('tfc,tfd->fcd', s, s.conj()) / len(s)
    power = cov[:, 1, 1].real
    transfer = cov[:, :, 1] / power[:, None]
    data = dict(transfer=np.stack([transfer, transfer]), coherence=np.ones((2,257,6)),
                mic2_power=np.stack([power,power]), source_cases=[1,2])
    path = tmp_path / 'references.npz'; np.savez(path, **data)
    return raw, render, path, data


def advanced(raw):
    result = raw.copy(); result[:-1,2:] = raw[1:,2:]
    return result


def test_same_state_selects_zero_without_mutating_pcm(signals):
    raw, render, path, _ = signals; before = raw.copy()
    assert estimate(raw, render, path)['delays'] == [0, 0, 0, 0, 0, 0]
    assert np.array_equal(raw, before)


def test_one_sample_advanced_group_selects_one(signals):
    raw, render, path, _ = signals
    assert estimate(advanced(raw), render, path)['delays'] == [0, 0, 1, 1, 1, 1]


def test_render_conditioning_tolerates_independent_interference(signals):
    raw, render, path, _ = signals
    interference = np.random.default_rng(3).normal(0,.025,raw.shape).astype('f4')
    assert estimate(advanced(raw) + interference, render, path)['delays'] == [0, 0, 1, 1, 1, 1]


def test_unrelated_render_fails_coherence(signals):
    raw, render, path, _ = signals
    with pytest.raises(ValueError, match='coherence'):
        estimate(raw, np.random.default_rng(3).normal(0,.08,len(render)), path)


def test_changed_phase_between_halves_is_rejected(signals):
    raw, render, path, _ = signals
    mixed = raw.copy(); mixed[len(raw)//2:] = advanced(raw)[len(raw)//2:]
    with pytest.raises(ValueError, match='disagrees'):
        estimate(mixed, render, path)


def test_static_channel_states_are_proven_independently(signals):
    raw, render, path, _ = signals; mixed = advanced(raw); mixed[:,3] = raw[:,3]
    assert estimate(mixed, render, path)['delays'] == [0, 0, 1, 0, 1, 1]


def test_unchanged_channel_transfer_must_match(signals):
    raw, render, path, _ = signals; changed=raw.copy(); changed[:-1,0]=raw[1:,0]
    with pytest.raises(ValueError, match='integer states'):
        estimate(changed, render, path)


def test_wrong_channel_gain_is_not_explained_by_phase(signals):
    raw, render, path, _ = signals; changed=raw.copy(); changed[:,4]*=2
    with pytest.raises(ValueError, match='integer states'):
        estimate(changed, render, path)


def test_conflicting_fixed_references_are_rejected(signals):
    raw, render, path, data=signals
    data['transfer'][1,:,2:] *= np.exp(-1j*OMEGA[:,None]); np.savez(path,**data)
    with pytest.raises(ValueError, match='disagrees'):
        estimate(raw, render, path)


@pytest.mark.parametrize('kind', ['nan','clip','short','silent'])
def test_invalid_pcm_fails_closed(signals, kind):
    raw, render, path, _ = signals
    if kind=='nan': raw[0,0]=np.nan
    if kind=='clip': raw[:1000,0]=1
    if kind=='short': raw=raw[:16000];render=render[:16000]
    if kind=='silent': render=render*0
    with pytest.raises(ValueError, match='PCM'): estimate(raw,render,path)


def test_narrow_tone_cannot_establish_broadband_phase(signals):
    _,_,path,_=signals
    render=(.08*np.sin(2*np.pi*1000*np.arange(64000)/16000)).astype('f4')
    raw=render[:,None]*np.ones((1,6),'f4')
    with pytest.raises(ValueError, match='coherence'): estimate(raw,render,path)


def test_unknown_reference_provenance_rejected(signals):
    raw,render,path,data=signals;data['source_cases']=[0,3];np.savez(path,**data)
    with pytest.raises(ValueError, match='references'): estimate(raw,render,path)


def test_fractional_group_phase_is_not_forced_to_nearest_integer(signals):
    raw, render, path, _ = signals
    fractional=(raw+advanced(raw))*.5
    with pytest.raises(ValueError, match='integer states'):
        estimate(fractional,render,path)


@pytest.mark.parametrize('shift', [-1, 2])
def test_unsupported_integer_channel_shift_is_rejected(signals, shift):
    raw, render, path, _ = signals; changed=raw.copy()
    if shift == -1: changed[1:,3] = raw[:-1,3]
    else: changed[:-2,3] = raw[2:,3]
    with pytest.raises(ValueError, match='integer states'):
        estimate(changed, render, path)


@pytest.mark.parametrize('bits', range(16))
def test_all_independently_proven_vectors_keep_anchor_channels_zero(signals, bits):
    raw, render, path, _ = signals; changed=raw.copy()
    expected=[0, 0]+[(bits >> bit) & 1 for bit in range(4)]
    for channel in range(2,6):
        if expected[channel]: changed[:-1,channel] = raw[1:,channel]
    assert estimate(changed, render, path)['delays'] == expected
