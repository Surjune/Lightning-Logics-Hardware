"""Underwater acoustics and the transmitter's adaptation rule.

This is the reference model of what the firmware decides (firmware/sonar_tx/env_model.cpp
implements the same equations and constants). Given the environment, it picks the band,
pulse length, amplitude, window, modulation and ping interval.

Physics:

* Sound speed: Mackenzie (1981), nine-term equation, valid 2-30 degC, 25-40 PSU, 0-8000 m.
* Seawater absorption: Francois and Garrison (1982): boric acid and magnesium sulphate
  relaxations plus pure-water viscosity, in dB/km with f in kHz.
* Suspended sediment: a linear-in-frequency attenuation term, K_SED * NTU * f_kHz. Viscous
  absorption by fine sediment grows with concentration and frequency (Richards, Heathershaw
  and Thorne, 1996); the coefficient here is an order-of-magnitude estimate (100 NTU adds
  about 400 dB/km at 500 kHz) to be calibrated against a real turbidity sensor.

Rule:

1. Band: the highest centre frequency in 120-450 kHz whose band edge still keeps the two-way
   absorption over the required range within ABSORPTION_BUDGET_DB. Higher frequency means
   finer range resolution; the budget caps what the water takes away.
2. Energy: the two-way transmission loss (spherical spreading + absorption), normalised over
   the operating envelope (5 m clear water to 200 m at 100 NTU), sets how much of the pulse
   energy range to use: first a longer pulse (up to 5 ms, and short enough that the blind
   zone stays within 10 % of the range), then a larger amplitude (0.5 to 1.0).
3. Modulation (auto): Barker-13 when the blind-zone limit forces a pulse under 1 ms, else LFM.
4. Window (auto): Hann for light-demand pings (low range sidelobes), Tukey when energy matters.
5. Ping interval: long enough for the echo to come back from the required range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .waveforms import PulseSpec

# ---- operating envelope and rule constants (keep in step with firmware env_model.h) -------------
FC_MAX_HZ = 450e3
FC_MIN_HZ = 120e3
FC_STEP_HZ = 2.5e3
BW_AT_FC_MIN_HZ = 40e3      # 100-140 kHz at the bottom of the band
BW_AT_FC_MAX_HZ = 100e3     # 400-500 kHz at the top
ABSORPTION_BUDGET_DB = 20.0  # two-way absorption + sediment loss allowed over the range
K_SED_DB_PER_KM_NTU_KHZ = 0.008
PH = 8.0

RANGE_MIN_M, RANGE_MAX_M = 5.0, 200.0
NTU_MAX = 100.0
REF_TEMP_C, REF_SALINITY_PSU, REF_DEPTH_M = 20.0, 35.0, 10.0

T_MIN_S, T_MAX_S = 1e-3, 5e-3
AMP_MIN, AMP_MAX = 0.5, 1.0
ENERGY_SPAN_DB = 10 * math.log10(T_MAX_S / T_MIN_S) + 20 * math.log10(AMP_MAX / AMP_MIN)
BLIND_FRACTION = 0.1         # blind zone c*T/2 kept within 10 % of the range
PRI_MIN_S = 20e-3
PRI_MARGIN = 1.25

MODULATIONS = ("auto", "lfm", "geometric", "barker13", "cw")
WINDOW_MODES = ("auto", "rect", "tukey", "hann", "hamming", "blackman")

# mean-square of each window (energy relative to a rectangular pulse of the same peak)
_WINDOW_POWER = {"rect": 1.0, "hann": 0.375, "hamming": 0.3974, "blackman": 0.3046}


def window_power(window: str, tukey_alpha: float = 0.2) -> float:
    return 1.0 - 5.0 * tukey_alpha / 8.0 if window == "tukey" else _WINDOW_POWER[window]


# ---- physics ------------------------------------------------------------------------------------
def sound_speed(temp_c: float, salinity_psu: float, depth_m: float) -> float:
    """Mackenzie (1981), m/s."""
    T, S, D = temp_c, salinity_psu, depth_m
    return (1448.96 + 4.591 * T - 5.304e-2 * T * T + 2.374e-4 * T ** 3 + 1.340 * (S - 35.0)
            + 1.630e-2 * D + 1.675e-7 * D * D - 1.025e-2 * T * (S - 35.0) - 7.139e-13 * T * D ** 3)


@dataclass(frozen=True)
class _FGCoefficients:
    a1: float
    f1: float
    a2p2: float
    f2: float
    a3p3: float


def _fg_coefficients(temp_c: float, salinity_psu: float, depth_m: float, ph: float = PH) -> _FGCoefficients:
    T, S, D = temp_c, max(salinity_psu, 0.0), depth_m
    c = 1412.0 + 3.21 * T + 1.19 * S + 0.0167 * D
    theta = 273.0 + T
    a1 = 8.86 / c * 10 ** (0.78 * ph - 5.0)
    f1 = 2.8 * math.sqrt(S / 35.0) * 10 ** (4.0 - 1245.0 / theta)
    a2 = 21.44 * S / c * (1.0 + 0.025 * T)
    p2 = 1.0 - 1.37e-4 * D + 6.2e-9 * D * D
    f2 = 8.17 * 10 ** (8.0 - 1990.0 / theta) / (1.0 + 0.0018 * (S - 35.0))
    if T <= 20.0:
        a3 = 4.937e-4 - 2.59e-5 * T + 9.11e-7 * T * T - 1.50e-8 * T ** 3
    else:
        a3 = 3.964e-4 - 1.146e-5 * T + 1.45e-7 * T * T - 6.5e-10 * T ** 3
    p3 = 1.0 - 3.83e-5 * D + 4.9e-10 * D * D
    return _FGCoefficients(a1, f1, a2 * p2, f2, a3 * p3)


def _fg_eval(k: _FGCoefficients, f_khz: float) -> float:
    ff = f_khz * f_khz
    boric = k.a1 * k.f1 * ff / (k.f1 * k.f1 + ff) if k.f1 > 0 else 0.0
    return boric + k.a2p2 * k.f2 * ff / (k.f2 * k.f2 + ff) + k.a3p3 * ff


def seawater_absorption(f_hz: float, temp_c: float, salinity_psu: float, depth_m: float,
                        ph: float = PH) -> float:
    """Francois and Garrison (1982), dB/km."""
    return _fg_eval(_fg_coefficients(temp_c, salinity_psu, depth_m, ph), f_hz / 1e3)


def sediment_attenuation(f_hz: float, turbidity_ntu: float) -> float:
    """Suspended-sediment attenuation estimate, dB/km."""
    return K_SED_DB_PER_KM_NTU_KHZ * max(turbidity_ntu, 0.0) * f_hz / 1e3


def bandwidth_for(fc_hz: float) -> float:
    return BW_AT_FC_MIN_HZ + (fc_hz - FC_MIN_HZ) * (BW_AT_FC_MAX_HZ - BW_AT_FC_MIN_HZ) / (FC_MAX_HZ - FC_MIN_HZ)


# ---- decision --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Environment:
    turbidity_ntu: float = 0.0
    range_m: float = 60.0
    temp_c: float = 25.0
    salinity_psu: float = 35.0
    depth_m: float = 10.0


@dataclass(frozen=True)
class Decision:
    spec: PulseSpec
    pri_s: float
    sound_speed: float
    fc_hz: float
    bandwidth_hz: float
    alpha_db_km: float          # total attenuation at the band's top edge
    absorption_2way_db: float   # over the required range, at the top edge
    tl_2way_db: float           # spreading + absorption at the centre frequency
    demand: float               # 0..1 share of the pulse-energy range used
    range_limited: bool         # even the lowest band exceeds the absorption budget
    achievable_range_m: float   # range at which the chosen band uses the whole budget
    range_resolution_m: float
    blind_zone_m: float
    time_bandwidth: float
    energy_db: float            # pulse energy relative to the lightest ping (1 ms, 0.5, Hann)
    avg_power_db: float         # energy / PRI relative to the lightest ping at the minimum PRI


def _q(x: float, step: float) -> float:
    """Round to a multiple of step (half up), the same way the firmware does."""
    return math.floor(x / step + 0.5) * step


def _select_band(env: Environment, k: _FGCoefficients) -> tuple[float, float, bool]:
    fc = FC_MAX_HZ
    while True:
        top = fc + bandwidth_for(fc) / 2
        alpha = _fg_eval(k, top / 1e3) + sediment_attenuation(top, env.turbidity_ntu)
        if 2.0 * alpha * env.range_m / 1e3 <= ABSORPTION_BUDGET_DB:
            return fc, alpha, False
        if fc - FC_STEP_HZ < FC_MIN_HZ - 1.0:
            return fc, alpha, True
        fc -= FC_STEP_HZ


def _tl_2way(env: Environment, k: _FGCoefficients, fc: float) -> float:
    alpha_c = _fg_eval(k, fc / 1e3) + sediment_attenuation(fc, env.turbidity_ntu)
    return 2.0 * (20.0 * math.log10(env.range_m) + alpha_c * env.range_m / 1e3)


def _corner_tl(turbidity_ntu: float, range_m: float) -> float:
    env = Environment(turbidity_ntu, range_m, REF_TEMP_C, REF_SALINITY_PSU, REF_DEPTH_M)
    k = _fg_coefficients(env.temp_c, env.salinity_psu, env.depth_m)
    return _tl_2way(env, k, _select_band(env, k)[0])


TL_MIN_DB = _corner_tl(0.0, RANGE_MIN_M)
TL_MAX_DB = _corner_tl(NTU_MAX, RANGE_MAX_M)


def decide(env: Environment, modulation: str = "auto", window: str = "auto") -> Decision:
    if modulation not in MODULATIONS or window not in WINDOW_MODES:
        raise ValueError("unknown modulation or window mode")
    k = _fg_coefficients(env.temp_c, env.salinity_psu, env.depth_m)
    c = sound_speed(env.temp_c, env.salinity_psu, env.depth_m)
    fc, alpha_top, limited = _select_band(env, k)
    bw = bandwidth_for(fc)
    tl = _tl_2way(env, k, fc)
    demand = min(max((tl - TL_MIN_DB) / (TL_MAX_DB - TL_MIN_DB), 0.0), 1.0)
    energy_db = demand * ENERGY_SPAN_DB

    t_blind = BLIND_FRACTION * 2.0 * env.range_m / c
    kind = modulation
    if kind == "auto":
        kind = "barker13" if t_blind < T_MIN_S else "lfm"

    # energy: pulse length first (within the blind-zone limit), then amplitude
    t_energy = T_MIN_S * 10 ** (energy_db / 10.0)
    t_cap = min(T_MAX_S, max(t_blind, T_MIN_S))
    if kind == "barker13":
        chip = max(1.0 / bw, min(t_blind, t_energy) / 13.0)
        chip = _q(chip, 0.5e-6)
        duration = 13 * chip
    else:
        duration = _q(min(t_energy, t_cap), 0.5e-6)
    leftover_db = energy_db - 10 * math.log10(max(duration, 1e-9) / T_MIN_S)
    amplitude = _q(min(AMP_MAX, max(AMP_MIN, AMP_MIN * 10 ** (max(leftover_db, 0.0) / 20.0))), 1e-3)

    if kind == "barker13":
        win, alpha_w = "tukey", 0.12
    elif window == "auto":
        win, alpha_w = ("hann", 0.2) if demand < 0.5 else ("tukey", 0.2)
    else:
        win, alpha_w = window, 0.2

    if kind in ("lfm", "geometric"):
        spec = PulseSpec(kind, fc - bw / 2, fc + bw / 2, duration, win, alpha_w, amplitude)
        b_eff = bw
    elif kind == "barker13":
        spec = PulseSpec("barker13", fc, fc, duration, win, alpha_w, amplitude, chip=chip, rc_shaping=0.2)
        b_eff = 1.0 / chip
    else:
        spec = PulseSpec("cw", fc, fc, duration, win, alpha_w, amplitude)
        b_eff = 1.0 / duration

    pri = _q(max(PRI_MIN_S, PRI_MARGIN * 2.0 * env.range_m / c), 1e-4)
    energy = amplitude ** 2 * duration * window_power(win, alpha_w)
    lightest = AMP_MIN ** 2 * T_MIN_S * window_power("hann")
    return Decision(
        spec=spec, pri_s=pri, sound_speed=c, fc_hz=fc, bandwidth_hz=bw, alpha_db_km=alpha_top,
        absorption_2way_db=2.0 * alpha_top * env.range_m / 1e3, tl_2way_db=tl, demand=demand,
        range_limited=limited, achievable_range_m=ABSORPTION_BUDGET_DB / (2.0 * alpha_top) * 1e3,
        range_resolution_m=c / (2.0 * b_eff), blind_zone_m=c * duration / 2.0,
        time_bandwidth=b_eff * duration, energy_db=10 * math.log10(energy / lightest),
        avg_power_db=10 * math.log10((energy / pri) / (lightest / PRI_MIN_S)),
    )
