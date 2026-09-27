import pytest

from sonarscope import analyze, chain
from sonarscope import thresholds as th
from sonarscope import waveforms as wf
from sonarscope.chain import ChainConfig, ScopeModel
from sonarscope.plan import TestCase, default_plan, get_plan, quick_plan

SCOPE = ScopeModel(fs=10e6, bits=12, noise_vrms=0.3e-3)


def test_criterion_ops():
    assert th.Criterion("x", "<=", 1).check(0.5)
    assert not th.Criterion("x", ">=", 1).check(0.5)
    assert th.Criterion("x", "abs<=", 1).check(-0.9)
    assert th.Criterion("x", "in", (1, 2)).check(1.5)
    assert th.Criterion("x", "==", 0).check(0)
    assert not th.Criterion("x", "<=", 1).check(float("nan"))


def test_evaluate_pass_fail_missing():
    crit = [th.Criterion("a", "<=", 1.0, 0.5), th.Criterion("b", ">=", 2.0)]
    r = th.evaluate({"a": 0.7, "b": 3}, crit)
    assert r["verdict"] == th.PASS and r["criteria"][0]["meets_target"] is False
    assert th.evaluate({"a": 1.5, "b": 3}, crit)["verdict"] == th.FAIL
    assert th.evaluate({"a": 0.7}, crit)["verdict"] == th.FAIL  # missing metric fails


def test_floor_sensitive_failure_is_inconclusive():
    crit = [th.Criterion("image_dbc", "<=", -38.0, floor_sensitive=True)]
    assert th.evaluate({"image_dbc": -35}, crit, floor_dbc=-70)["verdict"] == th.FAIL
    assert th.evaluate({"image_dbc": -35}, crit, floor_dbc=-40)["verdict"] == th.INCONCLUSIVE
    assert th.evaluate({"image_dbc": -45}, crit, floor_dbc=-40)["verdict"] == th.PASS


def test_expected_fail_and_informational():
    crit = [th.Criterion("edge_step_pct", "<=", 5.0)]
    r = th.evaluate({"edge_step_pct": 90}, crit, expected_fail=("edge_step_pct",))
    assert r["criteria"][0]["verdict"] == th.EXPECTED_FAIL and r["verdict"] == th.PASS
    r = th.evaluate({"edge_step_pct": 90}, crit, informational=True)
    assert r["verdict"] == th.INFO


def test_metric_reference_limit():
    crit = [c for c in th.TRANSITION if c.metric == "latency_s"]
    assert th.evaluate({"latency_s": 0.02, "latency_budget_s": 0.033}, crit)["verdict"] == th.PASS
    assert th.evaluate({"latency_s": 0.05, "latency_budget_s": 0.033}, crit)["verdict"] == th.FAIL


def test_plans():
    names = [t.name for t in default_plan()]
    assert names[0] == "floor" and "transition" in names and len(names) == len(set(names))
    assert {t.name for t in quick_plan()} <= set(names)
    with pytest.raises(KeyError):
        get_plan("nope")
    for t in default_plan():
        assert t.instruction()


def _run(test, cfg=None, context=None):
    cfg = cfg or ChainConfig(node=test.node)
    if test.kind == "pulse" or test.kind == "tone":
        data = chain.simulate(test.spec, cfg, SCOPE)
    elif test.kind == "pri":
        data = chain.simulate([test.spec] * test.n_pings, cfg, SCOPE, pri=test.pri)
    else:
        raise AssertionError(test.kind)
    return analyze.run_test(test, data, context if context is not None else {})


def test_reference_chain_passes_pulse_tests():
    plan = {t.name: t for t in default_plan()}
    for name in ("lfm_lo", "lfm_hi", "lfm_down", "lfm_hi_hann", "lfm_full", "geometric", "barker13",
                 "tone_101k", "tone_250k", "tone_487k", "pri"):
        r = _run(plan[name], context={"floor_dbc": -80})
        failing = [c for c in r["criteria"] if c["verdict"] not in (th.PASS,)]
        assert r["verdict"] == th.PASS, (name, failing)


def test_rect_reference_is_expected_fail_not_fail():
    t = {x.name: x for x in default_plan()}["lfm_hi_rect"]
    r = _run(t)
    edge = next(c for c in r["criteria"] if c["metric"] == "edge_step_pct")
    assert edge["verdict"] == th.EXPECTED_FAIL and r["verdict"] == th.PASS


def test_tone_500k_is_informational():
    t = {x.name: x for x in default_plan()}["tone_500k"]
    assert _run(t)["verdict"] == th.INFO


def test_faulty_chain_fails():
    plan = {t.name: t for t in default_plan()}
    r = _run(plan["lfm_hi"], ChainConfig(node="TP4", filt=None))
    img = next(c for c in r["criteria"] if c["metric"] == "image_dbc")
    assert img["verdict"] == th.FAIL and r["verdict"] == th.FAIL
    r = _run(plan["tone_487k"], ChainConfig(node="TP4", slew_rate=3e6))
    assert r["verdict"] == th.FAIL


def test_transition_via_analyze():
    t = {x.name: x for x in default_plan()}["transition"]
    specs, _ = chain.transition_schedule(t.spec, t.new_spec, pri=t.pri, n_pings=t.n_pings,
                                         t_change=t.t_change, adc_period=t.adc_period, synth_time=t.synth_time)
    segs = chain.simulate_segments(specs, t.pri, scope=SCOPE)
    r = analyze.run_test(t, segs, {"t0": t.t_change})
    assert r["verdict"] == th.PASS, r["criteria"]
    first_new = specs.index(t.new_spec)
    segs = chain.simulate_segments(specs, t.pri, scope=SCOPE, mixed_index=first_new)
    assert analyze.run_test(t, segs, {"t0": t.t_change})["verdict"] == th.FAIL


def test_floor_and_context_flow():
    t_floor = TestCase("floor", "floor", criteria=th.FLOOR)
    ctx = {"full_scale_v": 8.0}
    r = analyze.run_test(t_floor, chain.idle_capture(ChainConfig(node="TP4"), SCOPE), ctx)
    assert r["verdict"] == th.PASS and ctx["floor_dbc"] < -60
    with pytest.raises(ValueError):
        analyze.run_test(t_floor, chain.idle_capture(ChainConfig(node="TP4"), SCOPE), {})
