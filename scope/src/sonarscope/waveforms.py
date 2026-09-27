"""Reference models of the transmit pulses.

Every pulse the firmware can emit is described by a :class:`PulseSpec`. The same
spec drives three things:

* the simulated transmit chain (what the DAC should output),
* the matched-filter replica used by the analyzer (evaluated at any sample rate,
  because every waveform here is defined analytically),
* the ideal instantaneous-frequency law that measured chirps are compared against.

Waveform definitions (t in seconds, 0 <= t < T):

* LFM:        phi(t) = 2*pi*(f0*t + (f1 - f0)/(2*T) * t**2)
* Geometric:  f(t) = f0 * (f1/f0)**(t/T),  phi(t) = 2*pi*f0*T/L * (exp(L*t/T) - 1),
              L = ln(f1/f0)
* Barker-13:  BPSK on a carrier, chips of fixed duration, optional raised-cosine
              shaping of the phase transitions.
* CW:         single tone (used for THD / SFDR tests).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from . import config

KINDS = ("lfm", "geometric", "barker13", "cw")
WINDOWS = ("rect", "tukey", "hann", "hamming", "blackman")

BARKER13 = np.array([1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1], dtype=float)


@dataclass(frozen=True)
class PulseSpec:
    """One transmit pulse as commanded to the firmware.

    Frequencies in Hz, times in seconds. ``amplitude`` is a fraction of DAC full
    scale (1.0 = codes 1..255 around mid-scale 128).
    """

    kind: str = "lfm"
    f0: float = 400e3
    f1: float = 500e3
    duration: float = 2e-3
    window: str = "tukey"
    tukey_alpha: float = 0.2
    amplitude: float = 1.0
    # Barker-13 only
    chip: float = 40e-6
    rc_shaping: float = 0.0  # fraction of a chip used for raised-cosine phase transitions
    label: str = field(default="", compare=False)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}, got {self.window!r}")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if not 0 < self.amplitude <= 1.0:
            raise ValueError("amplitude must be in (0, 1]")
        if self.kind in ("lfm", "geometric") and (self.f0 <= 0 or self.f1 <= 0):
            raise ValueError("sweep frequencies must be positive")
        if self.kind == "geometric" and self.f0 == self.f1:
            raise ValueError("geometric sweep needs f0 != f1")
        if not 0 <= self.tukey_alpha <= 1:
            raise ValueError("tukey_alpha must be in [0, 1]")
        if not 0 <= self.rc_shaping <= 1:
            raise ValueError("rc_shaping must be in [0, 1]")
        if self.kind == "barker13":
            object.__setattr__(self, "duration", 13 * self.chip)

    # ---- derived quantities -------------------------------------------------
    @property
    def bandwidth(self) -> float:
        """Swept (or occupied, for Barker) bandwidth in Hz."""
        if self.kind in ("lfm", "geometric"):
            return abs(self.f1 - self.f0)
        if self.kind == "barker13":
            return 1.0 / self.chip
        return 1.0 / self.duration

    @property
    def carrier(self) -> float:
        """Centre / carrier frequency in Hz (f0 is the carrier for Barker and CW)."""
        if self.kind in ("lfm", "geometric"):
            return 0.5 * (self.f0 + self.f1)
        return self.f0

    @property
    def time_bandwidth(self) -> float:
        return self.duration * self.bandwidth

    def replace(self, **changes) -> "PulseSpec":
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PulseSpec":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "PulseSpec":
        return cls.from_dict(json.loads(s))


# ---- windows -----------------------------------------------------------------
def window(name: str, n: int, tukey_alpha: float = 0.2) -> np.ndarray:
    """Symmetric amplitude window of length ``n``."""
    if n <= 0:
        return np.zeros(0)
    if name == "rect":
        return np.ones(n)
    if name == "tukey":
        return signal.windows.tukey(n, tukey_alpha)
    if name == "hann":
        return signal.windows.hann(n)
    if name == "hamming":
        return signal.windows.hamming(n)
    if name == "blackman":
        return signal.windows.blackman(n)
    raise ValueError(f"unknown window {name!r}")


def window_energy_db(name: str, tukey_alpha: float = 0.2, n: int = 4096) -> float:
    """Pulse energy relative to a rectangular pulse of the same peak amplitude."""
    w = window(name, n, tukey_alpha)
    return 10 * np.log10(np.mean(w**2))


# ---- phase / frequency laws -----------------------------------------------------
def _time_axis(spec: PulseSpec, fs: float) -> np.ndarray:
    n = int(round(spec.duration * fs))
    return np.arange(n) / fs


def phase(spec: PulseSpec, t: np.ndarray) -> np.ndarray:
    """Carrier phase in radians (Barker: carrier only, code applied separately)."""
    T = spec.duration
    if spec.kind == "lfm":
        return 2 * np.pi * (spec.f0 * t + (spec.f1 - spec.f0) / (2 * T) * t * t)
    if spec.kind == "geometric":
        L = np.log(spec.f1 / spec.f0)
        return 2 * np.pi * spec.f0 * T / L * np.expm1(L * t / T)
    return 2 * np.pi * spec.f0 * t


def inst_freq(spec: PulseSpec, t: np.ndarray) -> np.ndarray:
    """Ideal instantaneous frequency in Hz at pulse-relative time ``t``."""
    T = spec.duration
    t = np.asarray(t, dtype=float)
    if spec.kind == "lfm":
        return spec.f0 + (spec.f1 - spec.f0) * t / T
    if spec.kind == "geometric":
        return spec.f0 * (spec.f1 / spec.f0) ** (t / T)
    return np.full_like(t, spec.f0)


def barker_code(spec: PulseSpec, t: np.ndarray, fs: float) -> np.ndarray:
    """Chip sequence (+1/-1) sampled at ``t``, optionally raised-cosine shaped."""
    idx = np.clip((t / spec.chip).astype(int), 0, 12)
    seq = BARKER13[idx]
    if spec.rc_shaping > 0:
        k = int(round(spec.rc_shaping * spec.chip * fs))
        if k >= 3:
            ker = signal.windows.hann(k)
            ker /= ker.sum()
            padded = np.concatenate([np.full(k, seq[0]), seq, np.full(k, seq[-1])])
            seq = np.convolve(padded, ker, mode="same")[k:-k]
    return seq


# ---- synthesis --------------------------------------------------------------------
def envelope(spec: PulseSpec, fs: float) -> np.ndarray:
    """Commanded amplitude envelope (window x amplitude) at sample rate ``fs``."""
    n = int(round(spec.duration * fs))
    return spec.amplitude * window(spec.window, n, spec.tukey_alpha)


def synthesize(spec: PulseSpec, fs: float, rx_weight: str | None = None) -> np.ndarray:
    """Ideal (unquantised) pulse in [-1, 1] sampled at ``fs``.

    ``rx_weight`` multiplies an additional window onto the pulse; the analyzer uses
    this to build mismatched (sidelobe-weighted) replicas.
    """
    t = _time_axis(spec, fs)
    x = np.sin(phase(spec, t))
    if spec.kind == "barker13":
        x = x * barker_code(spec, t, fs)
    x = x * envelope(spec, fs)
    if rx_weight is not None and rx_weight != "none":
        x = x * window(rx_weight, len(x), spec.tukey_alpha)
    return x


def quantize(x: np.ndarray, bits: int = config.DAC_BITS) -> np.ndarray:
    """Map [-1, 1] to unsigned DAC codes around mid-scale, as the firmware does.

    code = mid + round((mid - 1) * x), clipped to the code range.
    """
    mid = 1 << (bits - 1)
    top = (1 << bits) - 1
    return np.clip(np.round(mid + (mid - 1) * np.asarray(x)), 0, top).astype(np.int32)


def codes_to_unit(codes: np.ndarray, bits: int = config.DAC_BITS) -> np.ndarray:
    """Inverse of :func:`quantize` (without the rounding)."""
    mid = 1 << (bits - 1)
    return (np.asarray(codes, dtype=float) - mid) / (mid - 1)


def dac_codes(spec: PulseSpec, fs: float = config.FS_DAC, bits: int = config.DAC_BITS) -> np.ndarray:
    """The exact code sequence the firmware writes into the DMA buffer."""
    return quantize(synthesize(spec, fs), bits)


# ---- standard test pulses -----------------------------------------------------------
def preset(name: str) -> PulseSpec:
    """Named pulses used by the test plan and the demo."""
    presets = {
        "lfm_hi": PulseSpec("lfm", 400e3, 500e3, 2e-3, "tukey", label="LFM 400-500 kHz, 2 ms, Tukey"),
        "lfm_hi_hann": PulseSpec("lfm", 400e3, 500e3, 2e-3, "hann", label="LFM 400-500 kHz, 2 ms, Hann"),
        "lfm_hi_rect": PulseSpec("lfm", 400e3, 500e3, 2e-3, "rect", label="LFM 400-500 kHz, 2 ms, no window"),
        "lfm_lo": PulseSpec("lfm", 100e3, 140e3, 5e-3, "tukey", label="LFM 100-140 kHz, 5 ms, Tukey"),
        "lfm_down": PulseSpec("lfm", 500e3, 400e3, 2e-3, "tukey", label="LFM 500-400 kHz down-chirp"),
        "lfm_full": PulseSpec("lfm", 100e3, 500e3, 2e-3, "tukey", label="LFM 100-500 kHz full band"),
        "geometric": PulseSpec("geometric", 100e3, 500e3, 2e-3, "tukey", label="Geometric 100-500 kHz"),
        "barker13": PulseSpec("barker13", 300e3, 300e3, 13 * 40e-6, "tukey", tukey_alpha=0.05,
                              chip=40e-6, rc_shaping=0.2, label="Barker-13 on 300 kHz"),
        "tone_101k": PulseSpec("cw", 101.3e3, 101.3e3, 5e-3, "tukey", tukey_alpha=0.1, label="CW 101.3 kHz"),
        "tone_250k": PulseSpec("cw", 250e3, 250e3, 5e-3, "tukey", tukey_alpha=0.1, label="CW 250 kHz"),
        "tone_487k": PulseSpec("cw", 487.3e3, 487.3e3, 5e-3, "tukey", tukey_alpha=0.1, label="CW 487.3 kHz"),
        "tone_500k": PulseSpec("cw", 500e3, 500e3, 5e-3, "tukey", tukey_alpha=0.1, label="CW 500 kHz"),
        # Environment presets used in the adaptation demo
        "clear_reef": PulseSpec("lfm", 400e3, 500e3, 1e-3, "hann", amplitude=0.5, label="Clear shallow reef"),
        "muddy_estuary": PulseSpec("lfm", 100e3, 140e3, 5e-3, "tukey", amplitude=1.0, label="Muddy estuary"),
    }
    if name not in presets:
        raise KeyError(f"unknown preset {name!r}; choose from {sorted(presets)}")
    return presets[name]


PRESETS = (
    "lfm_hi", "lfm_hi_hann", "lfm_hi_rect", "lfm_lo", "lfm_down", "lfm_full", "geometric",
    "barker13", "tone_101k", "tone_250k", "tone_487k", "tone_500k", "clear_reef", "muddy_estuary",
)
