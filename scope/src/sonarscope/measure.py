"""Measurements on captured transmit pulses.

Each function takes a :class:`~sonarscope.capture.Capture` plus the commanded
:class:`~sonarscope.waveforms.PulseSpec` and returns a flat ``dict`` of named
metrics (SI units unless the name says otherwise). Pulses are located by
correlating against the analytic replica, so no trigger alignment is assumed.

Metric families
---------------
* envelope   — window fidelity, edge step, 10-90 % rise/fall, symmetry, idle DC offset
* frequency  — instantaneous-frequency fit (R^2, sweep-rate error, start/stop
               frequency), -10 dB bandwidth, in-band droop
* spurs      — reconstruction images and near out-of-band spurs (dBc)
* tone       — THD (H2..H5), SFDR, first image, harmonic/image coincidence flags
* compression— matched and weighted matched-filter peak sidelobe level, main-lobe
               width, range resolution, mismatch loss
* pri        — ping repetition interval and its jitter
* transition — old / new / mixed classification of each ping, adaptation latency
* floor      — instrument noise and spur floor from an idle capture
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from . import config, dsp
from . import waveforms as wf
from .capture import Capture
from .waveforms import PulseSpec


def _db20(x: float) -> float:
    return float(20 * np.log10(max(x, 1e-300)))


def _region(al: dsp.Alignment, spec: PulseSpec, fs: float, n: int) -> tuple[int, int]:
    i0 = int(round(al.start))
    i1 = i0 + int(round(spec.duration * fs))
    return max(0, i0), min(n, i1)


def _ideal_on_grid(spec: PulseSpec, fs: float, start: float, n: int) -> np.ndarray:
    """Commanded envelope evaluated on capture sample indices (0 outside the pulse)."""
    tau = (np.arange(n) - start) / fs
    w = wf.amplitude_envelope(spec, 1e7)  # dense reference, sampled below
    grid = np.arange(len(w)) / 1e7
    out = np.interp(tau, grid, w, left=0.0, right=0.0)
    out[(tau < 0) | (tau > spec.duration)] = 0.0
    return out


def _rise_fall(e: np.ndarray, ideal: np.ndarray, fs: float) -> tuple[float, float]:
    """10-90 % rise and 90-10 % fall, each normalised to the level where the commanded
    ramp ends (so in-band droop of a sweep does not distort the edge figures)."""
    if len(e) < 4 or ideal.max() <= 0:
        return float("nan"), float("nan")
    top = np.flatnonzero(ideal >= 0.999 * ideal.max())
    a, b = int(top[0]), int(top[-1])

    def span(x):
        x = x / x[-1] if x[-1] > 0 else x
        i10 = np.flatnonzero(x >= 0.1)
        i90 = np.flatnonzero(x >= 0.9)
        return (i90[0] - i10[0]) / fs if len(i10) and len(i90) else float("nan")

    return span(e[: a + 1]), span(e[b:][::-1])


# ---- envelope ---------------------------------------------------------------------------------
def envelope_metrics(cap: Capture, spec: PulseSpec, edge_window: float = 3e-6) -> dict:
    fs = cap.fs
    x = dsp.clean(cap, spec)
    al = dsp.align(x, fs, spec)
    env = dsp.envelope(x)
    i0, i1 = _region(al, spec, fs, len(x))
    ideal = _ideal_on_grid(spec, fs, al.start, len(x))
    seg, iseg = env[i0:i1], ideal[i0:i1]
    # A sweep sees the chain's frequency response (DAC sinc x filter) as a slow gain
    # change along the pulse. Model it as a quadratic gain in time and compare shapes.
    tt = np.linspace(-1, 1, len(seg))
    core = iseg >= 0.3 * iseg.max()
    if core.sum() > 8:
        gain = np.polyval(np.polyfit(tt[core], seg[core] / iseg[core], 2), tt)
    else:
        gain = np.full(len(seg), float(np.dot(seg, iseg) / max(np.dot(iseg, iseg), 1e-30)))
    model = gain * iseg
    peak_v = float(model.max())
    rms_err = float(np.sqrt(np.mean((seg - model) ** 2)) / peak_v) if peak_v > 0 else float("nan")
    safe_gain = np.where(gain > 0, gain, 1.0)
    flat = seg / safe_gain  # droop-corrected envelope

    W = max(1, int(round(edge_window * fs)))

    def step(e, a, b):
        return float(e[min(len(e) - 1, a)] - e[max(0, b)])

    start_step = step(env, i0 + W, i0 - W) / peak_v
    end_step = step(env, i1 - 1 - W, i1 - 1 + W) / peak_v
    imax = max(iseg.max(), 1e-30)
    ideal_start = step(ideal, i0 + W, i0 - W) / imax
    ideal_end = step(ideal, i1 - 1 - W, i1 - 1 + W) / imax

    rise, fall = _rise_fall(seg, iseg, fs)
    irise, ifall = _rise_fall(iseg, iseg, fs)
    denom = np.mean(np.abs(flat)) if len(flat) else 0.0
    sym = float(1 - np.mean(np.abs(flat - flat[::-1])) / denom) if denom > 0 else float("nan")

    raw = cap.ch()
    idle_end = max(0, i0 - int(20e-6 * fs))
    idle = raw[:idle_end]
    return {
        "pulse_start_s": float(al.start_time(cap)),
        "correlation": al.coefficient,
        "peak_v": peak_v,
        "window_rms_error": rms_err,
        "gain_variation_db": _db20(gain.max() / gain.min()) if gain.min() > 0 else float("nan"),
        "edge_step_pct": float(100 * max(start_step, end_step)),
        "edge_step_commanded_pct": float(100 * max(ideal_start, ideal_end)),
        "rise_s": rise, "fall_s": fall,
        "rise_commanded_s": irise, "fall_commanded_s": ifall,
        "rise_ratio": float(rise / irise) if irise and irise > 0 else float("nan"),
        "fall_ratio": float(fall / ifall) if ifall and ifall > 0 else float("nan"),
        "symmetry": sym,
        "idle_dc_v": float(np.mean(idle)) if len(idle) > 10 else float("nan"),
        "idle_rms_v": float(np.std(idle)) if len(idle) > 10 else float("nan"),
    }


# ---- instantaneous frequency ----------------------------------------------------------------------
def frequency_metrics(cap: Capture, spec: PulseSpec) -> dict:
    if spec.kind not in ("lfm", "geometric"):
        return {}
    fs = cap.fs
    x = dsp.clean(cap, spec)
    al = dsp.align(x, fs, spec)
    ph = np.unwrap(np.angle(signal.hilbert(x)))
    f = np.diff(ph) * fs / (2 * np.pi)
    L = max(1, int(round(2.0 / min(spec.f0, spec.f1) * fs)))
    fsm = dsp.moving_average(f, L)
    tau = (np.arange(len(f)) + 0.5 - al.start) / fs
    T = spec.duration
    w = np.interp(tau, np.linspace(0, T, 4096), wf.window(spec.window, 4096, spec.tukey_alpha),
                  left=0.0, right=0.0)
    m = (w >= 0.5) & (tau > L / fs) & (tau < T - L / fs)
    if m.sum() < 10:
        return {"freq_fit_points": int(m.sum())}
    tt, ff = tau[m], fsm[m]
    if spec.kind == "lfm":
        p = np.polyfit(tt, ff, 1)
        fit = np.polyval(p, tt)
        nominal_rate = (spec.f1 - spec.f0) / T
        rate_err = (p[0] - nominal_rate) / nominal_rate * 100
        f_start, f_end = np.polyval(p, 0.0), np.polyval(p, T)
    else:
        p = np.polyfit(tt, np.log(ff), 1)
        fit = np.exp(np.polyval(p, tt))
        nominal = np.log(spec.f1 / spec.f0)
        rate_err = (p[0] * T - nominal) / abs(nominal) * 100
        f_start, f_end = np.exp(p[1]), np.exp(p[0] * T + p[1])
    ss_res = float(np.sum((ff - fit) ** 2))
    ss_tot = float(np.sum((ff - ff.mean()) ** 2))

    # droop: measured / commanded envelope versus instantaneous frequency
    env = dsp.envelope(x)
    ideal = _ideal_on_grid(spec, fs, al.start, len(x))
    mm = ideal >= 0.5 * ideal.max()
    idx = np.flatnonzero(mm)
    droop = float("nan")
    if len(idx) > 20:
        resp = env[idx] / ideal[idx]
        fi = wf.inst_freq(spec, (idx - al.start) / fs)
        order = np.argsort(fi)
        k = max(3, len(idx) // 20)
        top = np.median(resp[order[-k:]])
        mid = np.median(resp[order[len(order) // 2 - k // 2: len(order) // 2 + k // 2 + 1]])
        droop = _db20(top / mid)

    bw, bw_ref = _bw10(x, fs, al, spec), _bw10(wf.synthesize(spec, fs), fs, None, spec)
    return {
        "freq_r2": 1 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        "sweep_rate_error_pct": float(rate_err),
        "f_start_hz": float(f_start), "f_end_hz": float(f_end),
        "f_start_error_pct": float((f_start - spec.f0) / spec.f0 * 100),
        "f_end_error_pct": float((f_end - spec.f1) / spec.f1 * 100),
        "freq_max_dev_hz": float(np.max(np.abs(ff - fit))),
        "freq_fit_points": int(m.sum()),
        "bw10_hz": bw, "bw10_commanded_hz": bw_ref,
        "bw10_error_pct": float((bw - bw_ref) / bw_ref * 100) if bw_ref else float("nan"),
        "droop_db": droop,
    }


def _bw10(x: np.ndarray, fs: float, al: dsp.Alignment | None, spec: PulseSpec) -> float:
    if al is not None:
        i0 = max(0, int(round(al.start)))
        x = x[i0: i0 + int(round(spec.duration * fs))]
    nfft = 1 << int(np.ceil(np.log2(max(len(x), fs / 500.0))))
    X = np.abs(np.fft.rfft(x, nfft))
    f = np.fft.rfftfreq(nfft, 1 / fs)
    X = dsp.moving_average(X, max(1, int(round(2e3 / (fs / nfft)))))
    above = np.flatnonzero(X >= X.max() * 10 ** (-10 / 20))
    return float(f[above[-1]] - f[above[0]]) if len(above) else float("nan")


# ---- spectrum: images and spurs -----------------------------------------------------------------------
def amplitude_spectrum(x: np.ndarray, fs: float, window: str = "hann", min_res_hz: float = 500.0):
    """One-sided amplitude spectrum scaled so a sine of amplitude A reads A."""
    x = np.asarray(x, dtype=float) - np.mean(x)
    w = signal.windows.get_window(window, len(x), fftbins=False)
    nfft = 1 << int(np.ceil(np.log2(max(len(x), fs / min_res_hz))))
    X = np.abs(np.fft.rfft(x * w, nfft)) * 2 / np.sum(w)
    return np.fft.rfftfreq(nfft, 1 / fs), X


def spur_metrics(cap: Capture, spec: PulseSpec, split_hz: float = 1.0e6,
                 fs_dac: float = config.FS_DAC) -> dict:
    fs = cap.fs
    f, X = amplitude_spectrum(cap.ch(), fs, "hann")
    lo, hi = min(spec.f0, spec.f1), max(spec.f0, spec.f1)
    if spec.kind == "barker13":
        lo, hi = spec.f0 - 1 / spec.chip, spec.f0 + 1 / spec.chip
    band = (f >= 0.95 * lo) & (f <= 1.05 * hi)
    ref = X[band].max()
    top = 0.98 * fs / 2
    out = {"inband_peak_v": float(ref), "analysis_top_hz": float(top)}
    img = (f > split_hz) & (f < top)
    if img.any():
        k = np.argmax(np.where(img, X, 0))
        out["image_dbc"] = _db20(X[k] / ref)
        out["image_peak_hz"] = float(f[k])
    near = (f > 1.25 * hi) & (f < min(split_hz, top))
    if near.any():
        k = np.argmax(np.where(near, X, 0))
        out["oob_near_dbc"] = _db20(X[k] / ref)
        out["oob_near_peak_hz"] = float(f[k])
    first_image = fs_dac - hi
    if first_image < top:
        m = (f > first_image - 0.1 * hi) & (f < fs_dac - 0.95 * lo + 0.1 * hi)
        if m.any():
            out["first_image_dbc"] = _db20(X[m].max() / ref)
    return out


def tone_metrics(cap: Capture, spec: PulseSpec, fs_dac: float = config.FS_DAC,
                 f_max: float = 5e6, n_harm: int = 5) -> dict:
    """THD / SFDR on the flat part of a CW burst (flat-top window for amplitude accuracy)."""
    fs = cap.fs
    raw = cap.ch()
    x = dsp.clean(cap, spec)
    al = dsp.align(x, fs, spec)
    ideal = _ideal_on_grid(spec, fs, al.start, len(raw))
    flat = np.flatnonzero(ideal >= 0.999 * ideal.max())
    if len(flat) < 64:
        return {}
    g = raw[flat[0]: flat[-1] + 1]
    n = len(g)
    f, X = amplitude_spectrum(g, fs, "flattop", min_res_hz=fs / n / 4)
    bin_hz = fs / n                          # resolution of the unpadded record
    half_lobe = 5 * bin_hz                   # flat-top main-lobe half width

    def peak_near(fc, width):
        m = (f > fc - width) & (f < fc + width)
        if not m.any():
            return 0.0, fc
        k = np.flatnonzero(m)[np.argmax(X[m])]
        return float(X[k]), float(f[k])

    f0 = spec.f0
    a1, f1 = peak_near(f0, max(0.02 * f0, half_lobe))
    top = min(f_max, 0.98 * fs / 2)
    harms, coincide = [], []
    for k in range(2, n_harm + 1):
        fk = k * f1
        if fk >= top:
            break
        a, _ = peak_near(fk, half_lobe)
        harms.append(a)
        for mimg in range(1, 4):
            for img in (mimg * fs_dac - f1, mimg * fs_dac + f1):
                if abs(img - fk) < 2 * half_lobe:
                    coincide.append({"harmonic": k, "image_hz": float(img)})
    thd = _db20(np.sqrt(np.sum(np.square(harms))) / a1) if harms and a1 > 0 else float("nan")
    excl = (f < 20e3) | (np.abs(f - f1) < 1.2 * half_lobe) | (f > top)
    spur_k = int(np.argmax(np.where(excl, 0, X)))
    img_a, img_f = peak_near(fs_dac - f1, half_lobe) if fs_dac - f1 < top else (float("nan"), float("nan"))
    return {
        "tone_hz": f1, "tone_amplitude_v": a1,
        "thd_db": thd,
        "harmonics_dbc": [_db20(h / a1) for h in harms],
        "sfdr_dbc": _db20(X[spur_k] / a1), "sfdr_spur_hz": float(f[spur_k]),
        "tone_image_dbc": _db20(img_a / a1) if np.isfinite(img_a) else float("nan"),
        "harmonic_image_coincidence": coincide,
    }


# ---- pulse compression ------------------------------------------------------------------------------
def _mainlobe_bounds(r: np.ndarray, k: int, nominal: int) -> tuple[int, int]:
    rs = dsp.moving_average(r, max(1, nominal // 20))

    def walk(direction):
        a = k + direction * max(1, nominal // 2)
        b = k + direction * 3 * nominal
        rng = range(a, b, direction)
        prev = None
        for i in rng:
            if i <= 0 or i >= len(rs) - 1:
                return i
            if rs[i] <= rs[i - direction] and rs[i] <= rs[i + direction]:
                return i
            prev = i
        return k + direction * 2 * nominal if prev is None else prev

    return walk(-1), walk(+1)


def compression_metrics(cap: Capture, spec: PulseSpec, rx_weight: str | None = "hamming",
                        c: float = config.C_WATER) -> dict:
    if spec.kind == "cw":
        return {}
    fs = cap.fs
    x = dsp.clean(cap, spec)
    al = dsp.align(x, fs, spec)
    n = int(round(spec.duration * fs))
    i0 = int(round(al.start))
    pad = n // 2 + int(10e-6 * fs)
    xg = x[max(0, i0 - pad): min(len(x), i0 + n + pad)]
    nominal = int(round((spec.chip if spec.kind == "barker13" else 1.0 / spec.bandwidth) * fs))

    def one(ref):
        r = dsp.xcorr_envelope(xg, ref)
        k = int(np.argmax(r))
        left, right = _mainlobe_bounds(r, k, max(nominal, 2))
        side = np.concatenate([r[:max(0, left)], r[min(len(r), right + 1):]])
        psl = _db20(side.max() / r[k]) if len(side) else float("nan")
        above = np.flatnonzero(r >= r[k] / np.sqrt(2))
        # contiguous -3 dB region around the peak
        lo = k
        while lo - 1 >= 0 and r[lo - 1] >= r[k] / np.sqrt(2):
            lo -= 1
        hi = k
        while hi + 1 < len(r) and r[hi + 1] >= r[k] / np.sqrt(2):
            hi += 1
        width = (hi - lo + 1) / fs
        eff = r[k] ** 2 / (np.sum(xg**2) * np.sum(ref**2))
        return psl, width, eff, len(above)

    psl_m, w_m, eff_m, _ = one(wf.synthesize(spec, fs))
    out = {"psl_matched_db": psl_m, "mainlobe_3db_matched_s": w_m,
           "range_resolution_matched_m": c * w_m / 2}
    if rx_weight and rx_weight != "none":
        psl_w, w_w, eff_w, _ = one(wf.synthesize(spec, fs, rx_weight=rx_weight))
        out.update({"rx_weight": rx_weight, "psl_weighted_db": psl_w, "mainlobe_3db_weighted_s": w_w,
                    "range_resolution_weighted_m": c * w_w / 2,
                    "mismatch_loss_db": float(10 * np.log10(eff_w / eff_m))})
    return out


# ---- ping repetition ---------------------------------------------------------------------------------
def pri_metrics(cap: Capture, spec: PulseSpec) -> dict:
    fs = cap.fs
    x = dsp.clean(cap, spec)
    ref = wf.synthesize(spec, fs)
    r = dsp.xcorr_envelope(x, ref)
    peaks, _ = signal.find_peaks(r, height=0.5 * r.max(), distance=max(1, int(0.5 * spec.duration * fs)))
    times = np.array([cap.t0 + (dsp.parabolic_peak(r, int(p)) - (len(ref) - 1)) / fs for p in peaks])
    out = {"n_pings": int(len(times)), "ping_times_s": times.tolist()}
    if len(times) >= 2:
        pri = np.diff(times)
        out.update({"pri_mean_s": float(pri.mean()), "pri_jitter_rms_s": float(pri.std()),
                    "pri_jitter_pp_s": float(pri.max() - pri.min())})
    return out


# ---- adaptation transitions -------------------------------------------------------------------------------
def _analytic_rho(m: np.ndarray, r: np.ndarray, max_lag: int) -> float:
    """Normalised complex correlation, best over lags within +/- max_lag."""
    best = 0.0
    if len(m) == 0 or len(r) == 0:
        return 0.0
    ma, ra = signal.hilbert(m), signal.hilbert(r)
    for lag in range(-max_lag, max_lag + 1, max(1, max_lag // 8) if max_lag else 1):
        if lag >= 0:
            a, b = ma[lag:], ra[: len(ma) - lag]
        else:
            a, b = ma[: len(ma) + lag], ra[-lag:]
        k = min(len(a), len(b))
        if k < 8:
            continue
        a, b = a[:k], b[:k]
        den = np.sqrt(np.sum(np.abs(a) ** 2) * np.sum(np.abs(b) ** 2))
        if den > 0:
            best = max(best, float(np.abs(np.sum(a * np.conj(b))) / den))
    return best


def classify_ping(seg: Capture, old: PulseSpec, new: PulseSpec, margin: float = 0.2,
                  n_windows: int = 4) -> dict:
    """Label one ping as old / new / mixed / unknown.

    The ping's full extent is taken from its envelope (not from either replica), cut
    into ``n_windows`` windows, and each window is compared with the same stretch of
    the old and new replicas aligned at the ping start. A ping whose windows disagree
    was built from two parameter sets: the glitch a buffer swap must never produce.
    """
    fs = seg.fs
    lo = min(dsp.analysis_band(old)[0], dsp.analysis_band(new)[0])
    hi = max(dsp.analysis_band(old)[1], dsp.analysis_band(new)[1])
    x = dsp.bandpass(seg.ch(), fs, lo, hi)
    al_o, al_n = dsp.align(x, fs, old), dsp.align(x, fs, new)
    anchor = al_o if al_o.coefficient >= al_n.coefficient else al_n
    s = max(0, int(round(anchor.start)))
    env = dsp.envelope(x)
    bursts = dsp.find_bursts(env, fs, rel=0.05, merge_gap=5e-3)
    end = max((e for b0, e in bursts if e > s), default=s + int(round(min(old.duration, new.duration) * fs)))
    extent = max(end - s, n_windows * 8)
    ro, rn = wf.synthesize(old, fs), wf.synthesize(new, fs)
    n = max(len(ro), len(rn), extent)
    ro, rn = np.pad(ro, (0, n - len(ro))), np.pad(rn, (0, n - len(rn)))
    seg_x = x[s: s + n]
    if len(seg_x) < n:
        seg_x = np.pad(seg_x, (0, n - len(seg_x)))
    lag = int(round(1e-6 * fs))
    edges = np.linspace(0, extent, n_windows + 1).astype(int)
    energies = [float(np.sum(seg_x[edges[i]:edges[i + 1]] ** 2)) for i in range(n_windows)]
    windows = []
    for i in range(n_windows):
        a, b = edges[i], edges[i + 1]
        if energies[i] < 0.05 * max(energies):
            windows.append({"label": "silent"})
            continue
        po = _analytic_rho(seg_x[a:b], ro[a:b], lag)
        pn = _analytic_rho(seg_x[a:b], rn[a:b], lag)
        if po >= 0.6 and po - pn >= margin:
            lab = "old"
        elif pn >= 0.6 and pn - po >= margin:
            lab = "new"
        else:
            lab = "unknown"
        windows.append({"rho_old": po, "rho_new": pn, "label": lab})
    labels = {w["label"] for w in windows} - {"silent"}
    if labels == {"old"}:
        label = "old"
    elif labels == {"new"}:
        label = "new"
    elif labels == {"old", "new"}:
        label = "mixed"
    else:
        label = "unknown"
    ts = seg.timestamp or 0.0
    return {"label": label, "windows": windows, "start_s": float(ts + anchor.start_time(seg)),
            "extent_s": extent / fs, "rho_old": al_o.coefficient, "rho_new": al_n.coefficient}


def transition_metrics(segments: list[Capture], old: PulseSpec, new: PulseSpec,
                       t0: float | None = None) -> dict:
    pings = [classify_ping(s, old, new) for s in segments]
    labels = [p["label"] for p in pings]
    first_new = next((i for i, lab in enumerate(labels) if lab == "new"), None)
    reverted = sum(1 for i, lab in enumerate(labels) if first_new is not None and i > first_new and lab == "old")
    replica_similarity = _analytic_rho(wf.synthesize(old, 10e6)[:20000], wf.synthesize(new, 10e6)[:20000], 0)
    out = {
        "n_pings": len(pings), "labels": labels,
        "n_mixed": labels.count("mixed"), "n_unknown": labels.count("unknown"),
        "n_reverted": int(reverted), "first_new_index": first_new,
        "replica_similarity": replica_similarity,
    }
    if first_new is not None and t0 is not None:
        out["latency_s"] = float(pings[first_new]["start_s"] - t0)
    return out


def segments_from_capture(cap: Capture, spec: PulseSpec, guard: float = 50e-6) -> list[Capture]:
    """Split a continuous multi-ping capture into per-ping segments with timestamps."""
    x = dsp.clean(cap, spec)
    env = dsp.envelope(x)
    bursts = dsp.find_bursts(env, cap.fs, rel=0.1, merge_gap=0.25 * spec.duration)
    segs = []
    for s, e in bursts:
        a = cap.time[max(0, s)] - guard
        b = cap.time[min(cap.n - 1, e)] + max(guard, spec.duration)
        piece = cap.slice_time(a, b)
        piece.timestamp = 0.0
        segs.append(piece)
    return segs


def t0_from_channel(cap: Capture, channel: str = "T0") -> float | None:
    if channel not in cap.channels:
        return None
    v = cap.ch(channel)
    thr = 0.5 * (v.max() + v.min())
    idx = np.flatnonzero((v[:-1] < thr) & (v[1:] >= thr))
    return float(cap.time[idx[0] + 1]) if len(idx) else None


# ---- instrument floor ----------------------------------------------------------------------------------
def floor_metrics(idle: Capture, reference_amplitude_v: float, f_lo: float = 50e3,
                  f_hi: float = 5e6) -> dict:
    """Largest spur / noise level of an idle capture relative to the signal amplitude."""
    f, X = amplitude_spectrum(idle.ch(), idle.fs, "flattop")
    m = (f >= f_lo) & (f <= min(f_hi, 0.98 * idle.fs / 2))
    k = np.flatnonzero(m)[np.argmax(X[m])]
    return {"floor_dbc": _db20(X[k] / reference_amplitude_v), "floor_peak_hz": float(f[k]),
            "floor_rms_v": float(np.std(idle.ch())), "floor_dc_v": float(np.mean(idle.ch()))}
