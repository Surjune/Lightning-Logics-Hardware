"""Transmit-chain simulator: DAC codes -> analog test points -> oscilloscope samples.

Test points (match the bench build):

* TP1  ESP32 DAC pin (GPIO25): stepped output, V = Vref * code / 255
* TP2  after AC coupling and the unity buffer (mid-scale removed)
* TP3  after the 4th-order reconstruction filter
* TP4  driver output across the dummy load (gain, step attenuator, slew limit, rails)

The analog path is simulated at ``fs_dac * U`` (U >= 16) and then sub-sampled to the
scope's sample rate without an anti-alias filter, like a real DSO. Fault models
(DAC nonlinearity, swapped sample pairs, slew limiting, clipping, supply spur,
missing filter) exist so the analyzer can be shown to catch them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from . import config
from . import waveforms as wf
from .afe import ReconstructionFilter
from .capture import Capture
from .waveforms import PulseSpec

NODES = ("TP1", "TP2", "TP3", "TP4")


@dataclass
class ChainConfig:
    fs_dac: float = config.FS_DAC
    dac_bits: int = config.DAC_BITS
    vref: float = config.DAC_VREF
    filt: ReconstructionFilter | None = field(default_factory=ReconstructionFilter)
    driver_gain: float = 5.0
    attenuation_db: float = 0.0        # step attenuator ahead of the driver
    node: str = "TP3"
    # --- fault / non-ideality models ---
    dac_inl_lsb: float = 0.0           # parabolic bow, peak INL in LSB
    swap_pairs: bool = False           # DMA emits samples in swapped pairs
    slew_rate: float | None = None     # V/s at TP4 (None = unlimited)
    rail_v: float | None = None        # TP4 clip level (V)
    supply_spur_hz: float | None = None
    supply_spur_v: float = 0.0         # spur amplitude at the DAC pin (V)
    dc_offset_v: float = 0.0           # residual offset at the observed node (V)
    noise_vrms: float = 0.0            # analog noise at the observed node (V rms)

    def __post_init__(self):
        if self.node not in NODES:
            raise ValueError(f"node must be one of {NODES}")

    @property
    def lsb_v(self) -> float:
        return self.vref / ((1 << self.dac_bits) - 1)

    def node_gain(self) -> float:
        """Volts at the node per unit of normalised DAC amplitude (code offset / 127)."""
        g = self.lsb_v * ((1 << (self.dac_bits - 1)) - 1)
        if self.node == "TP4":
            g *= self.driver_gain * 10 ** (-self.attenuation_db / 20)
        return g


@dataclass
class ScopeModel:
    fs: float = 10e6
    bits: int = 12
    vdiv: float | None = None          # None = auto-range so the signal spans ~80 % of 8 div
    noise_vrms: float = 0.0            # front-end noise (V rms)
    trigger_jitter_s: float = 0.0      # rms jitter of the trigger point
    pre_trigger: float = 100e-6        # record before the trigger (s)


def _choose_oversample(fs_dac: float, fs_scope: float) -> tuple[int, int]:
    for U in range(16, 201):
        ratio = fs_dac * U / fs_scope
        if abs(ratio - round(ratio)) < 1e-9 and round(ratio) >= 1:
            return U, int(round(ratio))
    raise ValueError(f"scope rate {fs_scope} Sa/s is not reachable as an integer sub-multiple of "
                     f"{fs_dac}*U for U in 16..200")


def _apply_inl(codes: np.ndarray, cfg: ChainConfig) -> np.ndarray:
    top = (1 << cfg.dac_bits) - 1
    c = codes.astype(float)
    if cfg.dac_inl_lsb:
        u = c / top * 2 - 1                      # -1..1 across the code range
        c = c + cfg.dac_inl_lsb * (1 - u * u)    # bow peaking at mid-range
    return c


def _slew_limit(x: np.ndarray, rate: float, dt: float) -> np.ndarray:
    step = rate * dt
    y = np.empty_like(x)
    acc = x[0]
    for i, v in enumerate(x):
        d = v - acc
        if d > step:
            d = step
        elif d < -step:
            d = -step
        acc += d
        y[i] = acc
    return y


def render_codes(code_timeline: np.ndarray, cfg: ChainConfig, U: int, rng: np.random.Generator) -> np.ndarray:
    """Analog voltage at ``cfg.node`` sampled at fs_dac*U for a timeline of DAC codes."""
    codes = np.asarray(code_timeline)
    if cfg.swap_pairs:
        codes = codes.copy()
        n2 = len(codes) // 2 * 2
        codes[:n2] = codes[:n2].reshape(-1, 2)[:, ::-1].ravel()
    fs_sim = cfg.fs_dac * U
    v = _apply_inl(codes, cfg) * cfg.lsb_v                  # DAC pin voltage per sample
    v = np.repeat(v, U)                                     # zero-order hold
    if cfg.supply_spur_hz:
        t = np.arange(len(v)) / fs_sim
        v = v + cfg.supply_spur_v * np.sin(2 * np.pi * cfg.supply_spur_hz * t)
    if cfg.node == "TP1":
        out = v
    else:
        mid = (1 << (cfg.dac_bits - 1)) * cfg.lsb_v
        out = v - mid                                       # AC coupling removes mid-scale
        if cfg.node in ("TP3", "TP4") and cfg.filt is not None:
            out = cfg.filt.apply(out, fs_sim)
        if cfg.node == "TP4":
            out = out * cfg.driver_gain * 10 ** (-cfg.attenuation_db / 20)
            if cfg.slew_rate:
                out = _slew_limit(out, cfg.slew_rate, 1 / fs_sim)
            if cfg.rail_v:
                out = np.clip(out, -cfg.rail_v, cfg.rail_v)
    out = out + cfg.dc_offset_v
    if cfg.noise_vrms:
        out = out + rng.normal(0, cfg.noise_vrms, len(out))
    return out


def _digitise(v: np.ndarray, scope: ScopeModel, rng: np.random.Generator) -> tuple[np.ndarray, float]:
    if scope.noise_vrms:
        v = v + rng.normal(0, scope.noise_vrms, len(v))
    center = 0.5 * (np.max(v) + np.min(v)) if scope.vdiv is None else 0.0
    span = np.max(np.abs(v - center))
    vdiv = scope.vdiv if scope.vdiv is not None else max(span, 1e-3) / (0.8 * 4)
    fsr = 8 * vdiv
    lsb = fsr / (1 << scope.bits)
    q = np.clip(np.round((v - center) / lsb), -(1 << (scope.bits - 1)), (1 << (scope.bits - 1)) - 1)
    return q * lsb + center, vdiv


def _timeline(pulses: list[tuple[int, np.ndarray]], n_total: int, mid: int) -> np.ndarray:
    tl = np.full(n_total, mid, dtype=np.int32)
    for start, codes in pulses:
        end = min(n_total, start + len(codes))
        if start < n_total:
            tl[start:end] = codes[: end - start]
    return tl


def simulate(spec: PulseSpec | list[PulseSpec], cfg: ChainConfig | None = None,
             scope: ScopeModel | None = None, *, pri: float | None = None, record: float | None = None,
             t0_channel: float | None = None, seed: int = 0) -> Capture:
    """Simulate one scope acquisition.

    ``spec`` may be a list: pulse k is emitted at ``k * pri`` (PRI required).
    The trigger (t = 0) is the start of the first pulse at the DAC. ``t0_channel``
    adds a ``T0`` channel stepping 0 -> 3.3 V at that time (e.g. a preset button).
    """
    cfg = cfg or ChainConfig()
    scope = scope or ScopeModel()
    rng = np.random.default_rng(seed)
    specs = spec if isinstance(spec, list) else [spec]
    if len(specs) > 1 and not pri:
        raise ValueError("multiple pulses need a PRI")
    U, D = _choose_oversample(cfg.fs_dac, scope.fs)
    fs_sim = cfg.fs_dac * U
    pre = int(round(scope.pre_trigger * cfg.fs_dac))
    last_end = (len(specs) - 1) * (pri or 0) + specs[-1].duration
    rec = record if record is not None else scope.pre_trigger + last_end + 200e-6
    n_total = int(round(rec * cfg.fs_dac))
    mid = 1 << (cfg.dac_bits - 1)
    pulses = []
    for k, s in enumerate(specs):
        start = pre + int(round(k * (pri or 0) * cfg.fs_dac))
        pulses.append((start, wf.quantize(wf.synthesize(s, cfg.fs_dac), cfg.dac_bits)))
    v = render_codes(_timeline(pulses, n_total, mid), cfg, U, rng)
    jitter = int(round(rng.normal(0, scope.trigger_jitter_s) * fs_sim)) if scope.trigger_jitter_s else 0
    offset = (jitter % D)
    v = v[offset::D]
    y, vdiv = _digitise(v, scope, rng)
    t_first = -scope.pre_trigger + offset / fs_sim - jitter / fs_sim
    channels = {"CH1": y}
    if t0_channel is not None:
        t = t_first + np.arange(len(y)) / scope.fs
        channels["T0"] = np.where(t >= t0_channel, config.DAC_VREF, 0.0)
    meta = {"spec": specs[0].to_dict(), "specs": [s.to_dict() for s in specs], "pri": pri,
            "node": cfg.node, "fs_dac": cfg.fs_dac, "vdiv": vdiv, "scope_bits": scope.bits,
            "simulated": True}
    return Capture(1 / scope.fs, channels, t_first, meta)


def simulate_segments(specs: list[PulseSpec], pri: float, cfg: ChainConfig | None = None,
                      scope: ScopeModel | None = None, *, mixed_index: int | None = None,
                      seed: int = 0) -> list[Capture]:
    """Segmented acquisition: one segment per ping, timestamped at ``k * pri``.

    ``mixed_index`` injects the glitch the swap logic must never produce: that ping's
    first half comes from its predecessor's spec and its second half from its own.
    """
    segs = []
    for k, s in enumerate(specs):
        cap_spec = s
        if mixed_index is not None and k == mixed_index and k > 0:
            cap = _simulate_mixed(specs[k - 1], s, cfg, scope, seed + k)
        else:
            cap = simulate(cap_spec, cfg, scope, seed=seed + k)
        cap.timestamp = k * pri
        segs.append(cap)
    return segs


def _simulate_mixed(old: PulseSpec, new: PulseSpec, cfg, scope, seed) -> Capture:
    cfg = cfg or ChainConfig()
    scope = scope or ScopeModel()
    a = wf.quantize(wf.synthesize(old, cfg.fs_dac), cfg.dac_bits)
    b = wf.quantize(wf.synthesize(new, cfg.fs_dac), cfg.dac_bits)
    n = max(len(a), len(b))
    half = n // 2
    mixed = np.concatenate([np.pad(a, (0, max(0, n - len(a))), constant_values=1 << (cfg.dac_bits - 1))[:half],
                            np.pad(b, (0, max(0, n - len(b))), constant_values=1 << (cfg.dac_bits - 1))[half:]])
    rng = np.random.default_rng(seed)
    U, D = _choose_oversample(cfg.fs_dac, scope.fs)
    pre = int(round(scope.pre_trigger * cfg.fs_dac))
    n_total = pre + n + int(200e-6 * cfg.fs_dac)
    mid = 1 << (cfg.dac_bits - 1)
    v = render_codes(_timeline([(pre, mixed)], n_total, mid), cfg, U, rng)[::D]
    y, vdiv = _digitise(v, scope, rng)
    meta = {"spec": new.to_dict(), "node": cfg.node, "fs_dac": cfg.fs_dac, "vdiv": vdiv,
            "simulated": True, "injected_fault": "mixed"}
    return Capture(1 / scope.fs, {"CH1": y}, -scope.pre_trigger, meta)


def transition_schedule(old: PulseSpec, new: PulseSpec, *, pri: float, n_pings: int, t_change: float,
                        adc_period: float = 10e-3, synth_time: float = 3e-3) -> tuple[list[PulseSpec], float]:
    """Which spec each ping carries when the input changes at ``t_change``.

    The firmware polls the ADC every ``adc_period``, re-synthesises into the back
    buffer (``synth_time``) and swaps at the next ping boundary. Returns the
    per-ping specs and the time the first new ping starts.
    """
    seen = np.ceil(t_change / adc_period) * adc_period
    ready = seen + synth_time
    first_new = int(np.ceil(ready / pri - 1e-12))
    specs = [old if k < first_new else new for k in range(n_pings)]
    return specs, first_new * pri


def idle_capture(cfg: ChainConfig | None = None, scope: ScopeModel | None = None,
                 record: float = 1e-3, seed: int = 0) -> Capture:
    """No pulse: the node sits at mid-scale (used for DC-offset and noise-floor tests)."""
    cfg = cfg or ChainConfig()
    scope = scope or ScopeModel()
    rng = np.random.default_rng(seed)
    U, D = _choose_oversample(cfg.fs_dac, scope.fs)
    n = int(round(record * cfg.fs_dac))
    v = render_codes(np.full(n, 1 << (cfg.dac_bits - 1)), cfg, U, rng)[::D]
    fixed = replace(scope, vdiv=scope.vdiv or 5e-3)
    y, vdiv = _digitise(v, fixed, rng)
    return Capture(1 / scope.fs, {"CH1": y}, 0.0, {"node": cfg.node, "vdiv": vdiv, "simulated": True,
                                                  "spec": None})
