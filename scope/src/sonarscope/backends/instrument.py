"""Bench that drives a real oscilloscope and the transmitter's test mode."""

from __future__ import annotations

from ..dut import NullDut
from ..plan import TestCase
from .base import Acquisition, Bench, NotSupported, ScopeDriver, ScopeSetup


class InstrumentBench(Bench):
    name = "instrument"

    def __init__(self, scope: ScopeDriver, dut=None, *, signal_channel: str = "CH1",
                 marker_channel: str = "CH2", sample_rate: float = 10e6, trigger_level_v: float = 0.1,
                 full_scale_v: float | None = None, vdiv: dict | None = None, timeout_s: float = 10.0):
        self.scope = scope
        self.dut = dut or NullDut()
        self.sig = signal_channel
        self.marker = marker_channel
        self.fs = sample_rate
        self.level = trigger_level_v
        self.full_scale_v = full_scale_v
        self.vdiv = vdiv or {}
        self.timeout_s = timeout_s

    def _setup_for(self, test: TestCase) -> ScopeSetup:
        pre = 100e-6
        if test.kind in ("floor", "idle"):
            return ScopeSetup((self.sig,), 1e-3, 0.5e-3, self.fs, self.sig, self.level, vdiv=self.vdiv)
        if test.kind in ("tone", "pulse"):
            return ScopeSetup((self.sig,), pre + test.spec.duration + 300e-6, pre, self.fs, self.sig, self.level,
                              vdiv=self.vdiv)
        if test.kind == "pri":
            rec = pre + (test.n_pings - 1) * test.pri + test.spec.duration + 300e-6
            return ScopeSetup((self.sig,), rec, pre, self.fs, self.sig, self.level, vdiv=self.vdiv)
        if test.kind == "transition":
            rec = test.n_pings * test.pri + 1e-3
            # trigger on the T0 marker; keep enough history to see pings before the change
            return ScopeSetup((self.sig, self.marker), rec, test.t_change, self.fs, self.marker, 1.65,
                              vdiv=self.vdiv)
        raise ValueError(test.kind)

    def acquire(self, test: TestCase) -> Acquisition:
        setup = self._setup_for(test)
        if test.kind in ("pri", "transition") and getattr(self.scope, "name", "") == "analog-discovery":
            raise NotSupported("multi-ping records need a deep-memory oscilloscope")
        self.dut.prepare(test)
        self.scope.configure(setup)
        if test.kind in ("floor", "idle"):
            if not self.scope.force(self.timeout_s):
                raise NotSupported(f"forced acquisition did not complete for '{test.name}'")
        else:
            armed = self._arm_and_wait(test)
            if not armed:
                raise NotSupported(f"no trigger within {self.timeout_s:g} s for '{test.name}'")
        cap = self.scope.read_many(setup.channels)
        if test.kind == "transition" and self.marker in cap.channels:
            cap.channels["T0"] = cap.channels.pop(self.marker)
        cap.meta.update({"test": test.name, "node": test.node})
        return Acquisition(cap)

    def _arm_and_wait(self, test: TestCase) -> bool:
        if test.kind != "transition":
            return self.scope.single(self.timeout_s)
        # arm first, then request the change so the marker edge triggers the capture
        import threading
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("ok", self.scope.single(self.timeout_s)))
        t.start()
        self.dut.trigger_change(test)
        t.join()
        return bool(result.get("ok"))

    def describe(self) -> dict:
        return {"bench": self.name, "scope": self.scope.identify(), "dut": type(self.dut).__name__,
                "sample_rate": self.fs, "signal_channel": self.sig, "marker_channel": self.marker}

    def close(self) -> None:
        self.dut.close()
        self.scope.close()
