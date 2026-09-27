"""Diagnostic figures for each test (PNG, matplotlib Agg backend)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

from . import dsp  # noqa: E402
from . import measure as M  # noqa: E402
from . import waveforms as wf  # noqa: E402
from .capture import Capture  # noqa: E402
from .plan import TestCase  # noqa: E402

INK = "#1f2933"
SIG = "#2563eb"
REF = "#d97706"
BAD = "#dc2626"
OK = "#059669"
GRID = dict(color="#9aa5b1", alpha=0.35, linewidth=0.6)


def _style(ax, title):
    ax.set_title(title, fontsize=9, color=INK, loc="left")
    ax.grid(True, **GRID)
    ax.tick_params(labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def _db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))


def _spectrum_panel(ax, cap: Capture, ref_band=None, limit_dbc=None, split_hz=1e6, window="hann"):
    f, X = M.amplitude_spectrum(cap.ch(), cap.fs, window)
    if ref_band is not None:
        band = (f >= ref_band[0]) & (f <= ref_band[1])
        ref = X[band].max() if band.any() else X.max()
    else:
        ref = X.max()
    ax.plot(f / 1e6, _db(X / ref), color=SIG, linewidth=0.7)
    if limit_dbc is not None:
        ax.hlines(limit_dbc, split_hz / 1e6, f[-1] / 1e6, colors=BAD, linestyles="--", linewidth=0.9,
                  label=f"limit {limit_dbc:g} dBc")
        ax.legend(fontsize=7, loc="upper right", frameon=False)
    ax.set_xlim(0, min(5.0, f[-1] / 1e6))
    ax.set_ylim(-100, 5)
    ax.set_xlabel("MHz", fontsize=8)
    ax.set_ylabel("dBc", fontsize=8)


def pulse_figure(test: TestCase, cap: Capture, result: dict, path: Path) -> Path:
    spec = test.spec
    fs = cap.fs
    x = dsp.clean(cap, spec)
    al = dsp.align(x, fs, spec)
    env = dsp.envelope(x)
    ideal = M._ideal_on_grid(spec, fs, al.start, len(x))
    m = result["metrics"]
    fig, axs = plt.subplots(2, 2, figsize=(11, 6.4), constrained_layout=True)

    ax = axs[0, 0]
    t_ms = cap.time * 1e3
    ax.plot(t_ms, cap.ch(), color="#94a3b8", linewidth=0.3, label="captured")
    ax.plot(t_ms, env, color=SIG, linewidth=1.0, label="envelope")
    if ideal.max() > 0 and m.get("peak_v"):
        ax.plot(t_ms, ideal / ideal.max() * m["peak_v"], color=REF, linestyle="--", linewidth=1.0,
                label="commanded window")
    ax.set_xlabel("ms", fontsize=8)
    ax.set_ylabel("V", fontsize=8)
    ax.legend(fontsize=7, loc="upper right", framealpha=0.9)
    _style(ax, f"Waveform  edge step {m.get('edge_step_pct', float('nan')):.2f} %   "
               f"shape error {100 * (m.get('window_rms_error') or 0):.2f} %")

    ax = axs[0, 1]
    lo, hi = min(spec.f0, spec.f1), max(spec.f0, spec.f1)
    if spec.kind == "barker13":
        lo, hi = spec.f0 - 1 / spec.chip, spec.f0 + 1 / spec.chip
    limit = next((c for c in test.criteria if c.metric == "image_dbc"), None)
    _spectrum_panel(ax, cap, (0.95 * lo, 1.05 * hi), limit.limit if limit else None)
    _style(ax, f"Spectrum  images {m.get('image_dbc', float('nan')):.1f} dBc")

    ax = axs[1, 0]
    nper = max(64, int(round(64e-6 * fs)))
    # zero-padded 8x so a sweeping tone does not scallop between FFT bins
    f, t, S = signal.stft(x, fs, window="hann", nperseg=nper, noverlap=int(0.9 * nper), nfft=8 * nper)
    i0 = max(0, int(al.start) - int(0.1 * spec.duration * fs))
    i1 = min(len(x), int(al.start) + int(1.1 * spec.duration * fs))
    tsel = (t * fs >= i0) & (t * fs <= i1)
    fsel = f <= 1.3 * hi
    Sd = _db(np.abs(S[np.ix_(fsel, tsel)]))
    ax.pcolormesh((cap.t0 + t[tsel]) * 1e3, f[fsel] / 1e3, Sd, shading="auto", cmap="magma",
                  vmin=Sd.max() - 60, vmax=Sd.max())
    if spec.kind in ("lfm", "geometric"):
        tau = np.linspace(0, spec.duration, 200)
        ax.plot((al.start_time(cap) + tau) * 1e3, wf.inst_freq(spec, tau) / 1e3, color="#22d3ee",
                linestyle="--", linewidth=1.0, label="commanded sweep")
        ax.legend(fontsize=7, loc="upper left", frameon=False, labelcolor="white")
    ax.set_xlabel("ms", fontsize=8)
    ax.set_ylabel("kHz", fontsize=8)
    r2 = m.get("freq_r2")
    _style(ax, "Spectrogram" + (f"  R² {r2:.5f}" if r2 is not None else ""))

    ax = axs[1, 1]
    n = int(round(spec.duration * fs))
    s0 = int(round(al.start))
    pad = n // 2
    xg = x[max(0, s0 - pad): s0 + n + pad]
    span = (spec.chip if spec.kind == "barker13" else 1 / max(spec.bandwidth, 1)) * 12
    for weight, color, lab in ((None, SIG, "matched"), (test.rx_weight, REF, f"{test.rx_weight} weighted")):
        if weight is None and lab != "matched":
            continue
        ref = wf.synthesize(spec, fs, rx_weight=weight)
        r = dsp.xcorr_envelope(xg, ref)
        k = int(np.argmax(r))
        lag = (np.arange(len(r)) - k) / fs
        sel = np.abs(lag) <= span
        ax.plot(lag[sel] * 1e6, _db(r[sel] / r[k]), color=color, linewidth=0.9, label=lab)
    ax.set_ylim(-70, 3)
    ax.set_xlabel("lag (µs)", fontsize=8)
    ax.set_ylabel("dB", fontsize=8)
    ax.legend(fontsize=7, loc="upper right", frameon=False)
    psl = m.get("psl_weighted_db") if test.rx_weight else m.get("psl_matched_db")
    _style(ax, "Matched filter" + (f"  PSL {psl:.1f} dB" if psl is not None else ""))

    fig.suptitle(f"{test.name}: {test.description}   [{result['verdict']}]", fontsize=10, color=INK)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def tone_figure(test: TestCase, cap: Capture, result: dict, path: Path) -> Path:
    m = result["metrics"]
    fig, ax = plt.subplots(figsize=(11, 3.6), constrained_layout=True)
    _spectrum_panel(ax, cap, window="flattop")
    f0 = m.get("tone_hz") or test.spec.f0
    for k in range(2, 6):
        ax.axvline(k * f0 / 1e6, color=REF, linewidth=0.6, linestyle=":")
    ax.axvline((2e6 - f0) / 1e6, color=BAD, linewidth=0.6, linestyle=":")
    _style(ax, f"{test.name}: THD {m.get('thd_db', float('nan')):.1f} dB   SFDR {m.get('sfdr_dbc', float('nan')):.1f} dBc"
               f"   (dotted: harmonics, red: first image)   [{result['verdict']}]")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def floor_figure(test: TestCase, cap: Capture, result: dict, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(11, 3.2), constrained_layout=True)
    f, X = M.amplitude_spectrum(cap.ch(), cap.fs, "flattop")
    ax.plot(f / 1e6, _db(X), color=SIG, linewidth=0.7)
    ax.set_xlim(0, min(5.0, f[-1] / 1e6))
    ax.set_xlabel("MHz", fontsize=8)
    ax.set_ylabel("dBV", fontsize=8)
    m = result["metrics"]
    label = (f"floor {m['floor_dbc']:.1f} dBc" if "floor_dbc" in m else
             f"DC {1e3 * m.get('idle_dc_v', 0):.2f} mV, rms {1e3 * m.get('idle_rms_v', 0):.2f} mV")
    _style(ax, f"{test.name}: {label}   [{result['verdict']}]")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def pri_figure(test: TestCase, cap: Capture, result: dict, path: Path) -> Path:
    m = result["metrics"]
    fig, ax = plt.subplots(figsize=(11, 3.2), constrained_layout=True)
    ax.plot(cap.time * 1e3, cap.ch(), color=SIG, linewidth=0.3)
    for t in m.get("ping_times_s", []):
        ax.axvline(t * 1e3, color=REF, linewidth=0.8)
    ax.set_xlabel("ms", fontsize=8)
    jit = m.get("pri_jitter_pp_s")
    _style(ax, f"{test.name}: mean PRI {1e3 * (m.get('pri_mean_s') or 0):.4f} ms   "
               f"jitter p-p {1e9 * (jit or 0):.1f} ns   [{result['verdict']}]")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def transition_figure(test: TestCase, data, result: dict, path: Path) -> Path:
    m = result["metrics"]
    segs = data if isinstance(data, list) else M.segments_from_capture(data, test.spec)
    labels = m.get("labels", [])
    colors = {"old": "#64748b", "new": SIG, "mixed": BAD, "unknown": REF}
    fig, ax = plt.subplots(figsize=(11, 3.2), constrained_layout=True)
    for i, (s, lab) in enumerate(zip(segs, labels)):
        e = dsp.envelope(dsp.bandpass(s.ch(), s.fs, 50e3, 800e3))
        t = (s.timestamp or 0) + s.time
        ax.plot(t * 1e3, e / max(e.max(), 1e-12) + 0.0, color=colors.get(lab, INK), linewidth=0.8)
        ax.text(((s.timestamp or 0) + s.time[len(s.time) // 2]) * 1e3, 1.05, lab, fontsize=7, ha="center",
                color=colors.get(lab, INK))
    t0 = result.get("t0")
    if t0 is not None:
        ax.axvline(t0 * 1e3, color=BAD, linestyle="--", linewidth=0.9)
    ax.set_ylim(0, 1.2)
    ax.set_xlabel("ms", fontsize=8)
    lat = m.get("latency_s")
    _style(ax, f"{test.name}: {m.get('n_mixed', 0)} mixed, latency "
               f"{'n/a' if lat is None else f'{lat * 1e3:.2f} ms'} "
               f"(budget {1e3 * m.get('latency_budget_s', 0):.0f} ms)   [{result['verdict']}]")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def render(test: TestCase, data, result: dict, path: str | Path) -> Path | None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    single = data[0] if isinstance(data, list) and len(data) == 1 else data
    if test.kind == "pulse":
        return pulse_figure(test, single, result, path)
    if test.kind == "tone":
        return tone_figure(test, single, result, path)
    if test.kind in ("floor", "idle"):
        return floor_figure(test, single, result, path)
    if test.kind == "pri":
        return pri_figure(test, single, result, path)
    if test.kind == "transition":
        return transition_figure(test, data, result, path)
    return None
