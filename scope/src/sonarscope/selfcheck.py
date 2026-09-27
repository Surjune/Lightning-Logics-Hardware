"""Self-check: prove the analyzer before trusting it on hardware.

Three kinds of check, all on the reference model of the transmit chain:

1. Reference values — the analyzer must reproduce figures computed independently
   for the design (sidelobe levels, image rejection, THD at the H3/image coincidence).
2. Clean chain — every non-informational test in the default plan passes.
3. Discrimination — each injected hardware fault must be caught by the test that
   targets it (a checker that passes everything is worthless), and faults the
   analysis is designed to be immune to (trigger jitter) must not cause failures.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import analyze
from . import thresholds as th
from .backends import SimulatedBench
from .chain import ChainConfig, ScopeModel
from .plan import default_plan
from .suite import run_suite

# (test, metric, expected, tolerance) — expected values from the design reference model
REFERENCE = [
    ("lfm_hi_hann", "psl_matched_db", -46.7, 3.0),
    ("lfm_hi", "psl_weighted_db", -31.1, 2.0),
    ("lfm_hi", "image_dbc", -41.8, 3.0),
    ("lfm_hi_rect", "psl_matched_db", -13.3, 1.0),
    ("lfm_lo", "image_dbc", -63.9, 4.0),
    ("tone_487k", "tone_image_dbc", -41.1, 2.0),
    ("tone_500k", "thd_db", -40.3, 2.0),
    ("barker13", "psl_matched_db", -16.5, 2.0),
]
MINIMUMS = [
    ("lfm_hi", "freq_r2", 0.999),
    ("lfm_lo", "freq_r2", 0.999),
    ("lfm_full", "freq_r2", 0.999),
    ("geometric", "freq_r2", 0.999),
]


@dataclass
class Fault:
    name: str
    test: str
    expect: str                       # verdict the test must return
    cfg: dict | None = None
    scope: dict | None = None
    inject_mixed: bool = False
    metric: tuple[str, ...] = ()      # at least one of these criteria must be failing


FAULTS = [
    Fault("reconstruction filter missing", "lfm_hi", th.FAIL, {"filt": None}, metric=("image_dbc",)),
    Fault("slew-limited driver (3 V/us)", "tone_487k", th.FAIL, {"slew_rate": 3e6}, metric=("thd_db",)),
    Fault("DMA emits sample pairs swapped", "tone_250k", th.FAIL, {"swap_pairs": True}, metric=("sfdr_dbc",)),
    Fault("50 mV supply ripple at 100 kHz", "tone_250k", th.FAIL,
          {"supply_spur_hz": 100e3, "supply_spur_v": 0.05}, metric=("sfdr_dbc",)),
    Fault("50 mV output offset", "idle", th.FAIL, {"dc_offset_v": 0.05}, metric=("idle_dc_v",)),
    Fault("buffer swap mid-ping (mixed ping)", "transition", th.FAIL, inject_mixed=True, metric=("n_mixed",)),
    Fault("rectangular window (no taper)", "lfm_hi", th.FAIL, {}, metric=("edge_step_pct",)),
    Fault("5 us trigger jitter (must not matter)", "lfm_hi", th.PASS, scope={"trigger_jitter_s": 5e-6}),
    Fault("DAC bow 6 LSB (H2 spur)", "tone_250k", th.FAIL, {"dac_inl_lsb": 6.0}, metric=("thd_db", "sfdr_dbc")),
    Fault("scope overdriven (V/div too small)", "lfm_hi", th.FAIL, scope={"vdiv": 0.5}, metric=("clip_runs",)),
]


def _fault_run(f: Fault, plan: dict) -> dict:
    test = plan[f.test]
    if f.name.startswith("rectangular"):
        test = plan["lfm_hi_rect"]
        test = type(test)(**{**test.__dict__, "expected_fail": ()})
    cfg = ChainConfig(node=test.node, **(f.cfg or {}))
    scope = ScopeModel(fs=10e6, bits=12, noise_vrms=0.3e-3, **(f.scope or {}))
    bench = SimulatedBench(cfg, scope, inject_mixed=f.inject_mixed)
    acq = bench.acquire(test)
    ctx = {"full_scale_v": bench.full_scale_v, "floor_dbc": -80.0, **acq.context}
    return analyze.run_test(test, acq.data, ctx)


def run(out_dir=None, progress=print) -> dict:
    checks = []
    progress("reference chain: running the default plan")
    res = run_suite(default_plan(), SimulatedBench(), out_dir=out_dir, progress=lambda *a: None)
    by_name = {t["name"]: t for t in res["tests"]}

    for name, metric, expected, tol in REFERENCE:
        v = by_name[name]["metrics"].get(metric)
        ok = v is not None and abs(v - expected) <= tol
        checks.append({"group": "reference", "check": f"{name}.{metric}", "expected": f"{expected:g} ± {tol:g}",
                       "measured": v, "ok": ok})
    for name, metric, minimum in MINIMUMS:
        v = by_name[name]["metrics"].get(metric)
        checks.append({"group": "reference", "check": f"{name}.{metric}", "expected": f">= {minimum:g}",
                       "measured": v, "ok": v is not None and v >= minimum})
    coincide = by_name["tone_500k"]["metrics"].get("harmonic_image_coincidence") or []
    checks.append({"group": "reference", "check": "tone_500k flags image on H3", "expected": "flagged",
                   "measured": [c["harmonic"] for c in coincide], "ok": any(c["harmonic"] == 3 for c in coincide)})
    for t in res["tests"]:
        want = th.INFO if t["name"] == "tone_500k" else th.PASS
        checks.append({"group": "clean chain", "check": f"{t['name']} verdict", "expected": want,
                       "measured": t["verdict"], "ok": t["verdict"] == want})

    plan = {t.name: t for t in default_plan()}
    for f in FAULTS:
        r = _fault_run(f, plan)
        failing = [c["metric"] for c in r["criteria"] if c["verdict"] == th.FAIL]
        ok = r["verdict"] == f.expect and (not f.metric or any(m in failing for m in f.metric))
        checks.append({"group": "discrimination", "check": f.name, "expected": f"{f.test} {f.expect}"
                       + (f" on {'/'.join(f.metric)}" if f.metric else ""),
                       "measured": f"{r['verdict']}" + (f" ({', '.join(failing)})" if failing else ""), "ok": ok})
    n_ok = sum(c["ok"] for c in checks)
    return {"ok": n_ok == len(checks), "passed": n_ok, "total": len(checks), "checks": checks,
            "reference_results": res}


def format_table(result: dict) -> str:
    lines = []
    group = None
    for c in result["checks"]:
        if c["group"] != group:
            group = c["group"]
            lines.append(f"\n{group}")
        m = c["measured"]
        ms = f"{m:.4g}" if isinstance(m, float) else str(m)
        lines.append(f"  [{'ok' if c['ok'] else 'XX'}] {c['check']:<42} expected {c['expected']:<26} got {ms}")
    lines.append(f"\n{result['passed']}/{result['total']} checks passed")
    return "\n".join(lines)
