"""The bench test plan: which pulse, at which node, judged by which criteria.

The default plan follows the bring-up ladder: instrument floor, idle offset, CW
tones, chirps in both bands, window comparison, geometric sweep, Barker-13, ping
repetition, and an adaptation transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import thresholds as th
from . import waveforms as wf
from .waveforms import PulseSpec

KINDS = ("floor", "idle", "tone", "pulse", "pri", "transition")


@dataclass
class TestCase:
    __test__ = False  # not a pytest test class

    name: str
    kind: str
    spec: PulseSpec | None = None
    new_spec: PulseSpec | None = None        # transition target
    criteria: list = field(default_factory=list)
    node: str = "TP4"
    rx_weight: str | None = "hamming"
    informational: bool = False
    expected_fail: tuple[str, ...] = ()
    description: str = ""
    # multi-ping parameters
    pri: float = 20e-3
    n_pings: int = 4
    t_change: float = 31e-3                  # transition: time the input changes
    adc_period: float = 10e-3                # firmware ADC polling period
    synth_time: float = 3e-3                 # firmware re-synthesis time (measure on target)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")

    @property
    def latency_budget(self) -> float:
        """Worst case: change just after a poll, then wait for the next ping boundary."""
        return self.adc_period + self.synth_time + self.pri

    def instruction(self) -> str:
        """What the operator (or the firmware test-mode link) must set up."""
        if self.kind == "floor":
            return "Scope input: probe tip shorted to its ground spring at the test point, transmitter idle."
        if self.kind == "idle":
            return f"Transmitter enabled but idle (DAC at mid-scale). Probe on {self.node}."
        if self.kind == "transition":
            return (f"Transmit '{self.spec.label}' every {self.pri * 1e3:g} ms; switch the environment "
                    f"input to '{self.new_spec.label}' during the capture (preset button on the T0 channel).")
        what = self.spec.label or self.spec.to_json()
        if self.kind == "pri":
            return f"Transmit '{what}' repeatedly every {self.pri * 1e3:g} ms. Probe on {self.node}."
        return f"Transmit '{what}'. Probe on {self.node}."


def _pulse(name, criteria, **kw) -> TestCase:
    spec = wf.preset(name)
    return TestCase(name, "pulse", spec, criteria=criteria, description=spec.label, **kw)


def default_plan() -> list[TestCase]:
    E, S = th.EDGE, th.SWEEP
    return [
        TestCase("floor", "floor", criteria=th.FLOOR, description="Instrument noise and spur floor"),
        TestCase("idle", "idle", criteria=th.IDLE, description="Idle DC offset at the output"),
        TestCase("tone_101k", "tone", wf.preset("tone_101k"), criteria=th.TONE, description="CW 101.3 kHz"),
        TestCase("tone_250k", "tone", wf.preset("tone_250k"), criteria=th.TONE, description="CW 250 kHz"),
        TestCase("tone_487k", "tone", wf.preset("tone_487k"), criteria=th.TONE, description="CW 487.3 kHz"),
        TestCase("tone_500k", "tone", wf.preset("tone_500k"), criteria=th.TONE, informational=True,
                 description="CW 500 kHz: first image lands on H3 (demonstration)"),
        _pulse("lfm_lo", E + S + th.IMAGES_LO + th.PSL_WEIGHTED),
        _pulse("lfm_hi", E + S + th.IMAGES_HI + th.PSL_WEIGHTED),
        _pulse("lfm_down", E + S + th.IMAGES_HI + th.PSL_WEIGHTED),
        _pulse("lfm_hi_hann", E + S + th.IMAGES_HI + th.PSL_HANN, rx_weight=None),
        # no commanded ramp, so the rise/fall ratio criteria do not apply
        _pulse("lfm_hi_rect", E[:3] + S + th.IMAGES_HI, expected_fail=("edge_step_pct",),
               rx_weight="hamming"),
        _pulse("lfm_full", E + S + th.IMAGES_HI + th.PSL_WEIGHTED),
        _pulse("geometric", E + S + th.IMAGES_HI),
        _pulse("barker13", [th.EDGE[0]] + th.IMAGES_HI + th.PSL_BARKER, rx_weight=None),
        TestCase("pri", "pri", wf.preset("lfm_hi"), criteria=th.PRI, pri=20e-3, n_pings=4,
                 description="Ping repetition interval stability"),
        TestCase("transition", "transition", wf.preset("clear_reef"), wf.preset("muddy_estuary"),
                 criteria=th.TRANSITION, pri=20e-3, n_pings=8, t_change=31e-3,
                 description="Clear reef -> muddy estuary adaptation"),
    ]


def quick_plan() -> list[TestCase]:
    """A short subset for fast regression runs."""
    keep = {"floor", "idle", "tone_487k", "lfm_hi", "lfm_hi_hann", "barker13", "transition"}
    return [t for t in default_plan() if t.name in keep]


def get_plan(name: str) -> list[TestCase]:
    plans = {"default": default_plan, "quick": quick_plan}
    if name not in plans:
        raise KeyError(f"unknown plan {name!r}; choose from {sorted(plans)}")
    return plans[name]()
