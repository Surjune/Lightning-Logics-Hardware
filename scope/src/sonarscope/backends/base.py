"""Backend interfaces.

A *bench* produces the data one test case needs (a capture, segments, or a long
multi-ping record) plus any context the analysis needs (for example the time the
environment input changed). Three benches exist:

* ``SimulatedBench``  — the reference model of the transmit chain (no hardware)
* ``FileBench``       — previously saved captures, one file per test
* ``InstrumentBench`` — a real oscilloscope driver plus control of the transmitter

Oscilloscope drivers implement :class:`ScopeDriver`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..capture import Capture
from ..plan import TestCase


class NotSupported(Exception):
    """The bench cannot run this test (the suite records it as SKIPPED)."""


@dataclass
class Acquisition:
    data: Capture | list[Capture]
    context: dict = field(default_factory=dict)


class Bench(ABC):
    name = "bench"

    #: full-scale amplitude at the observed node (V), used as the floor reference
    full_scale_v: float | None = None

    @abstractmethod
    def acquire(self, test: TestCase) -> Acquisition:
        ...

    def describe(self) -> dict:
        return {"bench": self.name}

    def close(self) -> None:
        pass


@dataclass
class ScopeSetup:
    channels: tuple[str, ...] = ("CH1",)
    record_s: float = 3e-3
    pre_trigger_s: float = 100e-6
    sample_rate: float = 10e6          # requested; the driver reports what it achieved
    trigger_channel: str = "CH1"
    trigger_level_v: float = 0.1
    trigger_slope: str = "rise"
    vdiv: dict[str, float] = field(default_factory=dict)   # optional per-channel V/div


class ScopeDriver(ABC):
    """Minimal oscilloscope interface used by :class:`InstrumentBench`."""

    name = "scope"

    @abstractmethod
    def configure(self, setup: ScopeSetup) -> None:
        ...

    @abstractmethod
    def single(self, timeout_s: float = 10.0) -> bool:
        """Arm a single acquisition and wait for the trigger. Returns False on timeout."""

    def force(self, timeout_s: float = 5.0) -> bool:
        """Acquire one frame without waiting for a trigger (idle / floor captures)."""
        return self.single(timeout_s)

    @abstractmethod
    def read(self, channel: str) -> Capture:
        """Read one channel of the last acquisition (volts, with its own time base)."""

    def identify(self) -> str:
        return self.name

    def close(self) -> None:
        pass

    def read_many(self, channels) -> Capture:
        caps = [self.read(ch) for ch in channels]
        first = caps[0]
        n = min(c.n for c in caps)
        return Capture(first.dt, {ch: c.ch()[:n] for ch, c in zip(channels, caps)}, first.t0,
                       {"scope": self.identify()})
