"""Bench backed by the transmit-chain reference model."""

from __future__ import annotations

from dataclasses import replace

from .. import chain
from ..chain import ChainConfig, ScopeModel
from ..plan import TestCase
from .base import Acquisition, Bench


class SimulatedBench(Bench):
    name = "simulated"

    def __init__(self, cfg: ChainConfig | None = None, scope: ScopeModel | None = None, seed: int = 0,
                 inject_mixed: bool = False):
        self.cfg = cfg or ChainConfig(node="TP4")
        self.scope = scope or ScopeModel(fs=10e6, bits=12, noise_vrms=0.3e-3)
        self.seed = seed
        self.inject_mixed = inject_mixed

    @property
    def full_scale_v(self) -> float:
        return self.cfg.node_gain()

    def _cfg(self, test: TestCase) -> ChainConfig:
        return replace(self.cfg, node=test.node) if test.node != self.cfg.node else self.cfg

    def acquire(self, test: TestCase) -> Acquisition:
        cfg = self._cfg(test)
        seed = self.seed + sum(map(ord, test.name))
        if test.kind in ("floor", "idle"):
            cap = chain.idle_capture(cfg, self.scope, seed=seed)
            cap.meta["full_scale_v"] = replace(cfg).node_gain()
            return Acquisition(cap)
        if test.kind in ("tone", "pulse"):
            return Acquisition(chain.simulate(test.spec, cfg, self.scope, seed=seed))
        if test.kind == "pri":
            return Acquisition(chain.simulate([test.spec] * test.n_pings, cfg, self.scope, pri=test.pri,
                                              seed=seed))
        if test.kind == "transition":
            specs, _ = chain.transition_schedule(test.spec, test.new_spec, pri=test.pri,
                                                 n_pings=test.n_pings, t_change=test.t_change,
                                                 adc_period=test.adc_period, synth_time=test.synth_time)
            mixed = specs.index(test.new_spec) if self.inject_mixed and test.new_spec in specs else None
            segs = chain.simulate_segments(specs, test.pri, cfg, self.scope, mixed_index=mixed, seed=seed)
            return Acquisition(segs, {"t0": test.t_change})
        raise ValueError(test.kind)

    def describe(self) -> dict:
        c, s = self.cfg, self.scope
        return {"bench": self.name, "node": c.node, "driver_gain": c.driver_gain,
                "filter": c.filt.describe() if c.filt else None, "scope_fs": s.fs, "scope_bits": s.bits,
                "scope_noise_vrms": s.noise_vrms,
                "faults": {k: getattr(c, k) for k in ("dac_inl_lsb", "swap_pairs", "slew_rate", "rail_v",
                                                       "supply_spur_hz", "dc_offset_v") if getattr(c, k)}}
