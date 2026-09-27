"""Turn a capture (or segments) for one test case into metrics and a verdict."""

from __future__ import annotations

import numpy as np

from . import measure as M
from . import thresholds as th
from .capture import Capture
from .plan import TestCase


def metrics_for(test: TestCase, data: Capture | list[Capture], context: dict | None = None) -> dict:
    """Compute every metric the test's kind supports."""
    context = context or {}
    if test.kind == "floor":
        cap = _single(data)
        ref = context.get("full_scale_v") or cap.meta.get("full_scale_v")
        if not ref:
            raise ValueError("floor test needs a full-scale reference amplitude (run a tone test first "
                             "or set meta['full_scale_v'])")
        return M.floor_metrics(cap, ref)
    if test.kind == "idle":
        cap = _single(data)
        x = cap.ch()
        return {"idle_dc_v": float(np.mean(x)), "idle_rms_v": float(np.std(x))}
    if test.kind == "tone":
        cap = _single(data)
        out = {}
        out.update(M.tone_metrics(cap, test.spec))
        out.update(M.spur_metrics(cap, test.spec))
        out.update({k: v for k, v in M.envelope_metrics(cap, test.spec).items()
                    if k in ("edge_step_pct", "idle_dc_v", "peak_v", "correlation")})
        return out
    if test.kind == "pulse":
        cap = _single(data)
        out = {}
        out.update(M.envelope_metrics(cap, test.spec))
        out.update(M.frequency_metrics(cap, test.spec))
        out.update(M.spur_metrics(cap, test.spec))
        out.update(M.compression_metrics(cap, test.spec, rx_weight=test.rx_weight))
        return out
    if test.kind == "pri":
        return M.pri_metrics(_single(data), test.spec)
    if test.kind == "transition":
        segs = data if isinstance(data, list) else M.segments_from_capture(data, test.spec)
        t0 = context.get("t0")
        if t0 is None and not isinstance(data, list):
            t0 = M.t0_from_channel(data)
        out = M.transition_metrics(segs, test.spec, test.new_spec, t0=t0)
        out["latency_budget_s"] = test.latency_budget
        return out
    raise ValueError(test.kind)


def _single(data):
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError("this test expects a single capture, got segments")
        return data[0]
    return data


def judge(test: TestCase, metrics: dict, context: dict | None = None) -> dict:
    context = context or {}
    floor = context.get("floor_dbc") if test.kind in ("tone", "pulse") else None
    return th.evaluate(metrics, test.criteria, floor_dbc=floor, expected_fail=test.expected_fail,
                       informational=test.informational)


def run_test(test: TestCase, data, context: dict | None = None) -> dict:
    """Metrics + verdict for one test; updates ``context`` with values later tests use."""
    context = context if context is not None else {}
    metrics = metrics_for(test, data, context)
    result = judge(test, metrics, context)
    if test.kind == "floor":
        context["floor_dbc"] = metrics.get("floor_dbc")
    if test.kind == "tone" and "tone_amplitude_v" in metrics and "full_scale_v" not in context:
        context["full_scale_v"] = metrics["tone_amplitude_v"]
    return {"name": test.name, "kind": test.kind, "description": test.description,
            "node": test.node, "metrics": _jsonable(metrics), **result}


def _jsonable(d):
    if isinstance(d, dict):
        return {k: _jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_jsonable(v) for v in d]
    if isinstance(d, (np.floating,)):
        return float(d)
    if isinstance(d, (np.integer,)):
        return int(d)
    if isinstance(d, float) and np.isnan(d):
        return None
    return d
