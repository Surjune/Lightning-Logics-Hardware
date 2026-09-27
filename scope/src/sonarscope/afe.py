"""Analog front-end models: DAC zero-order hold and the reconstruction filter.

The reconstruction filter is a 4th-order Butterworth low-pass built from two
unity-gain Sallen-Key stages (Q = 0.5412 and 1.3066, TI SLOA049 coefficient table).

Unity-gain Sallen-Key low-pass, C1 = feedback capacitor, C2 = capacitor to ground:

    H(s) = 1 / (s^2 R1 R2 C1 C2 + s C2 (R1 + R2) + 1)
    w0 = 1 / sqrt(R1 R2 C1 C2),   Q = sqrt(R1 R2 C1 C2) / (C2 (R1 + R2))

A real solution for R1, R2 exists only when C1/C2 >= 4 Q^2.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

BUTTERWORTH4_Q = (0.5412, 1.3066)

_E96 = np.array([
    1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40, 1.43,
    1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10,
    2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
    3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53,
    4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65,
    6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76,
])


def e96(value: float) -> float:
    """Nearest E96 (1 %) resistor value."""
    decade = 10 ** np.floor(np.log10(value))
    return float(f"{_E96[np.argmin(np.abs(_E96 - value / decade))] * decade:.3g}")


@dataclass(frozen=True)
class SallenKeyStage:
    R1: float
    R2: float
    C1: float  # feedback capacitor
    C2: float  # capacitor to ground

    @property
    def den(self) -> np.ndarray:
        """Denominator polynomial in s (numerator is 1)."""
        return np.array([self.R1 * self.R2 * self.C1 * self.C2, self.C2 * (self.R1 + self.R2), 1.0])

    @property
    def f0(self) -> float:
        return 1 / (2 * np.pi * np.sqrt(self.R1 * self.R2 * self.C1 * self.C2))

    @property
    def Q(self) -> float:
        return np.sqrt(self.R1 * self.R2 * self.C1 * self.C2) / (self.C2 * (self.R1 + self.R2))

    @classmethod
    def design(cls, f0: float, Q: float, C1: float, C2: float, round_e96: bool = True) -> "SallenKeyStage":
        """Solve R1, R2 for the target f0 and Q with the given capacitors."""
        w0 = 2 * np.pi * f0
        S = 1 / (Q * w0 * C2)          # R1 + R2
        P = 1 / (w0**2 * C1 * C2)      # R1 * R2
        disc = S * S - 4 * P
        if disc < 0:
            raise ValueError(f"C1/C2 = {C1 / C2:.2f} is below 4Q^2 = {4 * Q * Q:.2f}; no real solution")
        R1 = (S + np.sqrt(disc)) / 2
        R2 = P / R1
        if round_e96:
            R1, R2 = e96(R1), e96(R2)
        return cls(R1, R2, C1, C2)


# Default build: fc = 600 kHz with C0G/NP0 E12 capacitors.
DEFAULT_STAGES = (
    SallenKeyStage.design(600e3, BUTTERWORTH4_Q[0], 390e-12, 270e-12),   # R1 1.30k, R2 511
    SallenKeyStage.design(600e3, BUTTERWORTH4_Q[1], 680e-12, 82e-12),    # R1 1.74k, R2 715
)


class ReconstructionFilter:
    """Cascade of Sallen-Key stages (unity DC gain)."""

    def __init__(self, stages=DEFAULT_STAGES):
        self.stages = tuple(stages)
        den = np.array([1.0])
        for st in self.stages:
            den = np.polymul(den, st.den)
        self.den = den

    @classmethod
    def butterworth(cls, fc: float = 600e3, caps=((390e-12, 270e-12), (680e-12, 82e-12)),
                    round_e96: bool = True) -> "ReconstructionFilter":
        return cls([SallenKeyStage.design(fc, q, c1, c2, round_e96) for q, (c1, c2) in zip(BUTTERWORTH4_Q, caps)])

    def response(self, f) -> np.ndarray:
        """Complex frequency response H(j 2 pi f)."""
        s = 2j * np.pi * np.asarray(f, dtype=float)
        return 1.0 / np.polyval(self.den, s)

    def gain_db(self, f) -> np.ndarray:
        return 20 * np.log10(np.abs(self.response(f)))

    def group_delay(self, f) -> np.ndarray:
        f = np.asarray(f, dtype=float)
        ph = np.unwrap(np.angle(self.response(f)))
        return -np.gradient(ph, 2 * np.pi * f)

    def sos(self, fs: float) -> np.ndarray:
        """Discrete-time model (bilinear transform) for time-domain simulation at ``fs``."""
        z, p, k = signal.tf2zpk([1.0], self.den)
        zd, pd, kd = signal.bilinear_zpk(z, p, k, fs)
        return signal.zpk2sos(zd, pd, kd)

    def apply(self, x: np.ndarray, fs: float) -> np.ndarray:
        return signal.sosfilt(self.sos(fs), x)

    def describe(self) -> list[dict]:
        return [{"stage": i + 1, "R1_ohm": s.R1, "R2_ohm": s.R2, "C1_F": s.C1, "C2_F": s.C2,
                 "f0_hz": s.f0, "Q": s.Q} for i, s in enumerate(self.stages)]


def zoh_response(f, fs_dac: float) -> np.ndarray:
    """Magnitude of the DAC zero-order hold: |sinc(f / fs)|."""
    return np.abs(np.sinc(np.asarray(f, dtype=float) / fs_dac))


def image_frequencies(f: float, fs_dac: float, n: int = 3) -> list[float]:
    """First reconstruction images of a tone f: k*fs +/- f for k = 1..n."""
    out = []
    for k in range(1, n + 1):
        out += [k * fs_dac - f, k * fs_dac + f]
    return sorted(out)
