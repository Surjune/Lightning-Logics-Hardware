"""Signal-processing helpers shared by the measurements."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

from . import waveforms as wf
from .capture import Capture
from .waveforms import PulseSpec


def analysis_band(spec: PulseSpec) -> tuple[float, float]:
    """Pass band used to strip DC, noise and reconstruction images before analysis.

    The edges sit well outside the pulse band (x1/1.6 below, x1.4 above) so the
    zero-phase order-8 Butterworth response adds < 0.05 dB of in-band droop.
    """
    lo = min(spec.f0, spec.f1)
    hi = max(spec.f0, spec.f1)
    if spec.kind == "barker13":
        lo, hi = spec.f0 - 2.0 / spec.chip, spec.f0 + 2.0 / spec.chip
    return max(5e3, lo / 1.6), hi * 1.4


def bandpass(x: np.ndarray, fs: float, lo: float | None, hi: float | None, order: int = 8) -> np.ndarray:
    """Zero-phase Butterworth band-pass (either edge may be None)."""
    nyq = fs / 2
    y = np.asarray(x, dtype=float)
    if hi is not None and hi < 0.95 * nyq:
        y = signal.sosfiltfilt(signal.butter(order, hi, "low", fs=fs, output="sos"), y)
    if lo is not None and lo > 0:
        y = signal.sosfiltfilt(signal.butter(order, lo, "high", fs=fs, output="sos"), y)
    return y


def clean(cap: Capture, spec: PulseSpec, channel: str | None = None) -> np.ndarray:
    lo, hi = analysis_band(spec)
    return bandpass(cap.ch(channel), cap.fs, lo, hi)


def envelope(x: np.ndarray) -> np.ndarray:
    return np.abs(signal.hilbert(x))


def moving_average(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def parabolic_peak(y: np.ndarray, i: int) -> float:
    """Sub-sample peak location by fitting a parabola through y[i-1..i+1]."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    a, b, c = y[i - 1], y[i], y[i + 1]
    den = a - 2 * b + c
    return float(i) if den == 0 else i + 0.5 * (a - c) / den


def xcorr_envelope(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """|analytic(cross-correlation)|; index k corresponds to lag k - (len(ref) - 1)."""
    return np.abs(signal.hilbert(signal.correlate(x, ref, mode="full", method="fft")))


@dataclass
class Alignment:
    start: float        # sample index (fractional) where the pulse starts in the capture
    peak: float         # correlation envelope peak
    coefficient: float  # normalised correlation (0..1)

    def start_time(self, cap: Capture) -> float:
        return cap.t0 + self.start * cap.dt


def align(x: np.ndarray, fs: float, spec: PulseSpec, search: tuple[int, int] | None = None) -> Alignment:
    """Locate the pulse by correlating against the reference replica."""
    ref = wf.synthesize(spec, fs)
    r = xcorr_envelope(x, ref)
    lag0 = len(ref) - 1
    if search is not None:
        lo = max(0, search[0] + lag0)
        hi = min(len(r), search[1] + lag0)
        k = lo + int(np.argmax(r[lo:hi]))
    else:
        k = int(np.argmax(r))
    kk = parabolic_peak(r, k)
    start = kk - lag0
    i0 = int(round(start))
    seg = x[max(0, i0): max(0, i0) + len(ref)]
    denom = np.sqrt(np.sum(seg**2) * np.sum(ref[: len(seg)] ** 2))
    coef = float(r[k] / denom) if denom > 0 else 0.0
    return Alignment(start, float(r[k]), min(coef, 1.0))


def find_bursts(env: np.ndarray, fs: float, rel: float = 0.1, merge_gap: float = 50e-6,
                min_len: float = 20e-6) -> list[tuple[int, int]]:
    """Contiguous regions where the envelope exceeds ``rel`` of its maximum."""
    if len(env) == 0 or env.max() <= 0:
        return []
    on = env > rel * env.max()
    edges = np.flatnonzero(np.diff(on.astype(int)))
    starts = list(edges[on[edges + 1]] + 1) if len(edges) else []
    ends = list(edges[~on[edges + 1]] + 1) if len(edges) else []
    if on[0]:
        starts = [0] + starts
    if on[-1]:
        ends = ends + [len(on)]
    bursts = list(zip(starts, ends))
    merged: list[list[int]] = []
    gap = int(merge_gap * fs)
    for s, e in bursts:
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= int(min_len * fs)]
