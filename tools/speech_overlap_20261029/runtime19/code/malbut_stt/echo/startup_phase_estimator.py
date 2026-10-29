"""Choose each channel's measured0/1sample state from a verified startup probe.

The caller must prove this render contains the pinned probe. References are
frozen robot-only captures; no current recording updates them or the filter.
"""
import numpy as np

RATE = 16000
WINDOW = np.sqrt(np.hanning(512))
FREQUENCY = np.fft.rfftfreq(512, 1 / RATE)
OMEGA = 2 * np.pi * FREQUENCY / RATE


class PhaseRejected(ValueError):
    def __init__(self, message, **evidence):
        super().__init__(message)
        self.evidence = evidence


def _spectrum(samples):
    frames = np.lib.stride_tricks.sliding_window_view(samples, 512, axis=0)[::128]
    return np.fft.rfft(frames * WINDOW, axis=-1).transpose(0, 2, 1)


def _transfer(raw, render):
    spectrum = _spectrum(np.column_stack((raw, render)))
    power = np.mean(abs(spectrum) ** 2, axis=0)
    cross = np.mean(spectrum[:, :, :6] * spectrum[:, :, 6, None].conj(), axis=0)
    transfer = np.divide(cross, cross[:, 1, None], out=np.zeros_like(cross),
                         where=abs(cross[:, 1, None]) > 1e-15)
    coherence = abs(cross) ** 2 / np.maximum(power[:, :6] * power[:, 6, None], 1e-15)
    return transfer, coherence, power


def estimate(raw6_float32, aligned_render_float32, profiles_path):
    """Return one proven delay vector or raise; no default on ambiguity."""
    raw = np.asarray(raw6_float32, dtype='f4')
    render = np.asarray(aligned_render_float32, dtype='f4')
    if (raw.ndim != 2 or raw.shape[1] != 6 or render.shape != (len(raw),)
            or not RATE * 2 <= len(raw) <= RATE * 8
            or not np.isfinite(raw).all() or not np.isfinite(render).all()
            or np.mean(abs(raw) >= 32760 / 32768) >= .001
            or np.sqrt(np.mean(render.astype('f8') ** 2)) < 1e-4):
        raise ValueError('Invalid or clipped startup phase PCM')
    with np.load(profiles_path, allow_pickle=False) as saved:
        references = saved['transfer']; coherent = saved['coherence']; powers = saved['mic2_power']
        if (saved['source_cases'].tolist() != [1, 2] or references.shape != (2, 257, 6)
                or coherent.shape != references.shape or powers.shape != (2, 257)
                or not all(np.isfinite(x).all() for x in (references, coherent, powers))):
            raise ValueError('Invalid frozen startup phase references')
    rows = []; decisions = {channel: [] for channel in (2, 3, 4, 5)}; windows = []
    for half, (begin, end) in enumerate(((0, len(raw)//2), (len(raw)//2, len(raw)))):
        # Divide out common mic/render timing locally, before long averaging
        # cancels cross phase. Cover every sample; never select a passing subset.
        parts = max(2, (end-begin+RATE-1)//RATE)
        edges = np.linspace(begin, end, parts+1, dtype=int)
        windows.extend((half, window, int(a), int(b))
                       for window, (a,b) in enumerate(zip(edges[:-1], edges[1:])))
    for half, window, start, end in windows:
        current, confidence, power = _transfer(raw[start:end], render[start:end])
        for ref_index in range(2):
            for channel in (0, 2, 3, 4, 5):
                reference = references[ref_index, :, channel]
                mask = ((FREQUENCY >= 300) & (FREQUENCY <= 3000)
                        & (confidence[:, 1] >= .5) & (confidence[:, channel] >= .5)
                        & (coherent[ref_index, :, channel] >= .7)
                        & (powers[ref_index] > powers[ref_index].max() * 1e-5)
                        & (power[:, 6] > power[:, 6].max() * 1e-5)
                        & (abs(reference) > 1e-12) & (abs(current[:, channel]) > 1e-12))
                bins = np.flatnonzero(mask)
                if len(bins) < 16 or np.ptp(FREQUENCY[bins]) < 600:
                    raise PhaseRejected('Insufficient known-render coherence for phase selection',
                        half=half, window=window, sample_interval=[start,end],
                        reference_case=ref_index+1, channel=channel+1,
                        coherent_bins=len(bins), span_hz=float(np.ptp(FREQUENCY[bins])) if len(bins) else 0.,
                        mic2_coherence_median=float(np.median(confidence[:,1])),
                        channel_coherence_median=float(np.median(confidence[:,channel])))
                ratio = current[mask, channel] / reference[mask]
                phase = np.angle(ratio)
                scores = [float(np.median(abs(np.angle(np.exp(1j * (phase - delay * OMEGA[mask]))))))
                          for delay in (0, 1)]
                magnitude = float(np.median(abs(20 * np.log10(abs(ratio)))))
                selected = 0 if channel == 0 else int(np.argmin(scores))
                # .10 rad absolute and .15 rad separation reject a fractional or
                # unsupported channel state instead of forcing the closest integer.
                if (scores[selected] > .10 or magnitude > 1.5
                        or (channel != 0 and scores[1-selected] - scores[selected] < .15)):
                    raise PhaseRejected('Startup phase differs from both frozen integer states',
                        half=half, window=window, sample_interval=[start,end],
                        reference_case=ref_index+1, channel=channel+1,
                        phase_error_rad=scores, magnitude_error_db=magnitude, selected=selected)
                if channel != 0: decisions[channel].append(selected)
                rows.append(dict(half=half, window=window, sample_interval=[start,end],
                                 reference_case=ref_index+1, channel=channel+1,
                                 coherent_bins=len(bins), phase_error_rad=scores,
                                 magnitude_error_db=magnitude, delay=selected))
    if any(len(set(values)) != 1 for values in decisions.values()):
        raise PhaseRejected('Startup phase disagrees across windows or references for one channel', comparisons=rows)
    return dict(delays=[0, 0] + [decisions[channel][0] for channel in (2, 3, 4, 5)], evidence=dict(method='known-render conditioned fixed RTF',
                                                source_cases=[1, 2], window_count=len(windows), comparisons=rows))
