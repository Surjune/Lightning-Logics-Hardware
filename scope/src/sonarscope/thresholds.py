"""Pass/fail criteria and verdict evaluation.

Thresholds are engineering targets derived from the reference model of the
transmit chain (8-bit DAC at 2 MSPS, 4th-order 600 kHz reconstruction filter) with
margin for real hardware. They are not taken from a published NIOT standard; the
THD / SFDR / sine-fit methodology follows IEEE Std 1241 and IEEE Std 1057.

Each criterion has a *pass* limit (must meet) and a *target* (should meet).
Spur-type criteria are *floor sensitive*: if a result fails but the measured
instrument floor is within 10 dB of the limit, the verdict is INCONCLUSIVE, because
the oscilloscope, not the transmitter, may be what is being measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PASS, FAIL, INCONCLUSIVE, MISSING, EXPECTED_FAIL, INFO = (
    "PASS", "FAIL", "INCONCLUSIVE", "MISSING", "EXPECTED-FAIL", "INFO")

FLOOR_MARGIN_DB = 10.0


@dataclass(frozen=True)
class Criterion:
    metric: str
    op: str            # "<=", ">=", "abs<=", "==", "in"
    limit: float | tuple
    target: float | tuple | None = None
    unit: str = ""
    what: str = ""
    floor_sensitive: bool = False

    @property
    def better(self) -> str:
        """Direction used by regression comparison."""
        return {"<=": "lower", ">=": "higher", "abs<=": "closer", "==": "equal", "in": "closer"}[self.op]

    def check(self, value, limit=None) -> bool:
        lim = self.limit if limit is None else limit
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return False
        if self.op == "<=":
            return value <= lim
        if self.op == ">=":
            return value >= lim
        if self.op == "abs<=":
            return abs(value) <= lim
        if self.op == "==":
            return value == lim
        if self.op == "in":
            return lim[0] <= value <= lim[1]
        raise ValueError(self.op)

    def describe_limit(self, limit=None) -> str:
        lim = self.limit if limit is None else limit
        if self.op == "in":
            return f"{lim[0]:g} .. {lim[1]:g} {self.unit}".strip()
        if self.op == "abs<=":
            return f"|x| <= {lim:g} {self.unit}".strip()
        return f"{self.op} {lim:g} {self.unit}".strip()


# ---- criterion library ------------------------------------------------------------------------------
C = Criterion
EDGE = [
    C("edge_step_pct", "<=", 5.0, 2.0, "%", "envelope jump within 3 us of the pulse edges"),
    C("window_rms_error", "<=", 0.05, 0.03, "", "envelope shape vs commanded window (droop removed)"),
    C("symmetry", ">=", 0.98, 0.99, "", "rising vs falling edge symmetry (droop removed)"),
    C("rise_ratio", "in", (0.8, 1.25), (0.9, 1.1), "", "measured / commanded 10-90 % rise"),
    C("fall_ratio", "in", (0.8, 1.25), (0.9, 1.1), "", "measured / commanded 90-10 % fall"),
]
SWEEP = [
    C("freq_r2", ">=", 0.998, 0.999, "", "instantaneous-frequency fit R^2"),
    C("sweep_rate_error_pct", "abs<=", 1.0, 0.5, "%", "sweep rate vs commanded"),
    C("f_start_error_pct", "abs<=", 1.0, 0.5, "%", "start frequency vs commanded"),
    C("f_end_error_pct", "abs<=", 1.0, 0.5, "%", "stop frequency vs commanded"),
    C("bw10_error_pct", "abs<=", 5.0, 2.0, "%", "-10 dB bandwidth vs commanded waveform"),
    C("droop_db", ">=", -1.5, -0.5, "dB", "in-band droop at the top of the sweep"),
]
IMAGES_HI = [C("image_dbc", "<=", -38.0, -42.0, "dBc", "worst spur above 1 MHz (reconstruction images)",
               floor_sensitive=True)]
IMAGES_LO = [C("image_dbc", "<=", -50.0, -60.0, "dBc", "worst spur above 1 MHz (reconstruction images)",
               floor_sensitive=True)]
IDLE = [C("idle_dc_v", "abs<=", 0.020, 0.005, "V", "DC offset while idle")]
PSL_WEIGHTED = [C("psl_weighted_db", "<=", -28.0, -30.0, "dB", "peak sidelobe, Hamming-weighted replica")]
PSL_HANN = [C("psl_matched_db", "<=", -35.0, -42.0, "dB", "peak sidelobe, matched replica")]
# Barker-13 is -22.3 dB for ideal rectangular chips; a tapered, band-limited burst
# gives about -16.5 dB in the reference model. Limits reflect the tapered build.
PSL_BARKER = [C("psl_matched_db", "<=", -15.0, -18.0, "dB", "peak sidelobe, matched replica")]
TONE = [
    C("thd_db", "<=", -34.0, -40.0, "dB", "THD from H2..H5", floor_sensitive=True),
    C("sfdr_dbc", "<=", -38.0, -42.0, "dBc", "largest spur up to 5 MHz", floor_sensitive=True),
]
PRI = [C("pri_jitter_pp_s", "<=", 0.5e-6, 0.1e-6, "s", "ping-to-ping interval jitter (1 DAC sample = 0.5 us)")]
TRANSITION = [
    C("n_mixed", "==", 0, 0, "", "pings built from two parameter sets"),
    C("n_unknown", "==", 0, 0, "", "pings matching neither parameter set"),
    C("n_reverted", "==", 0, 0, "", "pings reverting to the old parameters"),
    C("latency_s", "<=", "latency_budget_s", None, "s", "input change to first new ping"),
]
FLOOR = [C("floor_dbc", "<=", -48.0, -60.0, "dBc", "instrument spur/noise floor vs full scale")]


# ---- evaluation -------------------------------------------------------------------------------------------
def evaluate(metrics: dict, criteria: list[Criterion], *, floor_dbc: float | None = None,
             expected_fail: tuple[str, ...] = (), informational: bool = False) -> dict:
    """Apply criteria to a metrics dict. Returns per-criterion results and a verdict."""
    rows = []
    for c in criteria:
        value = metrics.get(c.metric)
        limit = metrics.get(c.limit) if isinstance(c.limit, str) else c.limit
        if value is None or limit is None:
            rows.append({"metric": c.metric, "value": value, "limit": c.describe_limit(limit) if limit is not None else c.limit,
                         "verdict": MISSING, "what": c.what})
            continue
        ok = c.check(value, limit)
        meets_target = c.check(value, c.target) if c.target is not None else ok
        if ok:
            verdict = PASS
        elif c.metric in expected_fail:
            verdict = EXPECTED_FAIL
        elif c.floor_sensitive and floor_dbc is not None and floor_dbc > limit - FLOOR_MARGIN_DB:
            verdict = INCONCLUSIVE
        else:
            verdict = FAIL
        if informational and verdict in (FAIL, INCONCLUSIVE):
            verdict = INFO
        rows.append({"metric": c.metric, "value": value, "limit": c.describe_limit(limit),
                     "target": c.describe_limit(c.target) if c.target is not None else None,
                     "meets_target": bool(meets_target), "verdict": verdict, "what": c.what,
                     "unit": c.unit})
    verdicts = {r["verdict"] for r in rows}
    if informational:
        overall = INFO
    elif FAIL in verdicts or MISSING in verdicts:
        overall = FAIL
    elif INCONCLUSIVE in verdicts:
        overall = INCONCLUSIVE
    else:
        overall = PASS
    return {"verdict": overall, "criteria": rows}


def all_criteria() -> dict[str, Criterion]:
    """Every criterion by metric name (used for regression direction)."""
    out = {}
    for group in (EDGE, SWEEP, IMAGES_HI, IDLE, PSL_WEIGHTED, PSL_HANN, TONE, PRI, TRANSITION, FLOOR):
        for c in group:
            out.setdefault(c.metric, c)
    return out
