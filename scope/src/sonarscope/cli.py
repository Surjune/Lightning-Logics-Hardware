"""Command-line interface: ``sonarscope <command> ...``."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

import numpy as np

from . import __version__

EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}


# ---- helpers ------------------------------------------------------------------------------------------------
def _parse_faults(items: list[str] | None) -> dict:
    """``--fault key=value`` pairs for the simulated chain (``filt=none`` removes the filter)."""
    out = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        if not raw:
            raise SystemExit(f"--fault expects key=value, got {item!r}")
        if key == "filt":
            if raw.lower() not in ("none", "off", "0"):
                raise SystemExit("--fault filt only accepts 'none'")
            out["filt"] = None
        elif raw.lower() in ("true", "false"):
            out[key] = raw.lower() == "true"
        else:
            out[key] = float(raw)
    return out


def _spec_from_args(args):
    from . import waveforms as wf
    if getattr(args, "spec", None):
        text = Path(args.spec).read_text() if Path(args.spec).exists() else args.spec
        return wf.PulseSpec.from_json(text)
    return wf.preset(args.preset)


def _make_bench(args):
    from .backends import FileBench, InstrumentBench, SimulatedBench
    from .chain import ChainConfig, ScopeModel
    if args.bench == "sim":
        cfg = ChainConfig(node=args.node, **_parse_faults(args.fault))
        scope = ScopeModel(fs=args.sample_rate, bits=args.scope_bits, noise_vrms=args.scope_noise)
        return SimulatedBench(cfg, scope, seed=args.seed, inject_mixed=args.inject_mixed)
    if args.bench == "files":
        if not args.captures:
            raise SystemExit("--bench files needs --captures DIR")
        return FileBench(args.captures, full_scale_v=args.full_scale)
    scope = _make_scope(args.bench, args.resource)
    return InstrumentBench(scope, _make_dut(args), sample_rate=args.sample_rate,
                           trigger_level_v=args.trigger_level, full_scale_v=args.full_scale)


def _make_scope(kind: str, resource: str | None):
    if kind in ("rigol", "siglent"):
        from .backends.scpi import DRIVERS
        if not resource:
            raise SystemExit(f"--bench {kind} needs --resource (e.g. TCPIP::192.168.1.50::INSTR)")
        return DRIVERS[kind](resource)
    if kind == "ad3":
        from .backends.ad3 import AnalogDiscovery
        return AnalogDiscovery()
    raise SystemExit(f"unknown scope {kind!r}")


def _make_dut(args):
    from .dut import ManualDut, NullDut, SerialDut
    if args.dut == "manual":
        return ManualDut()
    if args.dut == "serial":
        if not args.port:
            raise SystemExit("--dut serial needs --port")
        return SerialDut(args.port, args.baud)
    return NullDut()


def _print_result(r: dict) -> None:
    print(f"{r['name']}: {r['verdict']}")
    for c in r.get("criteria", []):
        v = c.get("value")
        vs = f"{v:.5g}" if isinstance(v, float) else str(v)
        print(f"  {c['verdict']:<13} {c['metric']:<22} {vs:>12}   limit {c.get('limit')}")


# ---- commands --------------------------------------------------------------------------------------------------
def cmd_simulate(args) -> int:
    from . import capture as cp
    from . import chain
    from .chain import ChainConfig, ScopeModel
    spec = _spec_from_args(args)
    cfg = ChainConfig(node=args.node, **_parse_faults(args.fault))
    scope = ScopeModel(fs=args.sample_rate, bits=args.scope_bits, noise_vrms=args.scope_noise)
    specs = [spec] * args.pings
    cap = chain.simulate(specs if args.pings > 1 else spec, cfg, scope, pri=args.pri if args.pings > 1 else None,
                         seed=args.seed)
    cp.save(args.output, cap)
    print(f"wrote {args.output}: {cap.n} samples at {cap.fs / 1e6:g} MSa/s, node {cfg.node}")
    return 0


def cmd_analyze(args) -> int:
    from . import analyze, capture as cp, plots
    from .plan import TestCase, default_plan
    from . import thresholds as th
    data = cp.load(args.file)
    plan = {t.name: t for t in default_plan()}
    if args.test:
        if args.test not in plan:
            raise SystemExit(f"unknown test {args.test!r}; choose from {sorted(plan)}")
        test = plan[args.test]
    else:
        spec = _spec_from_args(args)
        crit = th.SANITY + (th.TONE if spec.kind == "cw" else th.EDGE + th.SWEEP + th.IMAGES_HI + th.PSL_WEIGHTED)
        test = TestCase("custom", "tone" if spec.kind == "cw" else "pulse", spec, criteria=crit,
                        description=spec.label)
    ctx = {}
    if args.full_scale:
        ctx["full_scale_v"] = args.full_scale
    if args.t0 is not None:
        ctx["t0"] = args.t0
    r = analyze.run_test(test, data, ctx)
    _print_result(r)
    if args.plot:
        plots.render(test, data, r, args.plot)
        print(f"plot: {args.plot}")
    if args.json:
        Path(args.json).write_text(json.dumps(r, indent=1, default=str))
    return EXIT.get(r["verdict"], 0)


def cmd_suite(args) -> int:
    from . import regression
    from .plan import get_plan
    from .suite import run_suite
    plan = get_plan(args.plan)
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {t.name for t in plan}
        if unknown:
            raise SystemExit(f"unknown test(s): {sorted(unknown)}")
        plan = [t for t in plan if t.name in wanted]
    out = Path(args.out or f"out/{_dt.datetime.now():%Y%m%d-%H%M%S}")
    bench = _make_bench(args)
    print(f"bench: {bench.describe().get('bench')}   plan: {args.plan} ({len(plan)} tests)   output: {out}")
    try:
        res = run_suite(plan, bench, out_dir=out, make_plots=not args.no_plots)
    finally:
        bench.close()
    print(f"\nverdict: {res['verdict']}   {res['summary']}")
    print(f"report:  {out / 'report.html'}")
    code = EXIT.get(res["verdict"], 1)
    if args.history:
        prev = regression.latest(args.history)
        dest = regression.record(out / "results.json", args.history)
        print(f"history: {dest}")
        if prev is not None:
            cmp = regression.compare_files(prev, dest)
            _print_compare(cmp)
            if not cmp["ok"]:
                code = code or 1
    return code


def _print_compare(cmp: dict) -> None:
    print(f"\ncompared with {cmp['old_revision']}:")
    for v in cmp["verdict_changes"]:
        print(f"  verdict {v['test']}: {v['old']} -> {v['new']}")
    for r in cmp["regressions"]:
        print(f"  WORSE    {r['test']}.{r['metric']}: {r['old']:.5g} -> {r['new']:.5g}")
    for r in cmp["improvements"]:
        print(f"  better   {r['test']}.{r['metric']}: {r['old']:.5g} -> {r['new']:.5g}")
    if cmp["ok"]:
        print("  no regressions")


def cmd_compare(args) -> int:
    from . import regression
    cmp = regression.compare_files(args.old, args.new)
    _print_compare(cmp)
    return 0 if cmp["ok"] else 1


def cmd_selfcheck(args) -> int:
    from . import selfcheck
    res = selfcheck.run(out_dir=args.out)
    print(selfcheck.format_table(res))
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in res.items() if k != "reference_results"}
        (Path(args.out) / "selfcheck.json").write_text(json.dumps(slim, indent=1, default=str))
        print(f"reference report: {Path(args.out) / 'report.html'}")
    return 0 if res["ok"] else 1


def cmd_capture(args) -> int:
    from . import capture as cp
    from .backends import ScopeSetup
    scope = _make_scope(args.scope, args.resource)
    try:
        chans = tuple(args.channels.split(","))
        scope.configure(ScopeSetup(chans, args.record, args.pre, args.sample_rate, args.trigger_channel,
                                   args.trigger_level))
        ok = scope.force(args.timeout) if args.force else scope.single(args.timeout)
        if not ok:
            print("no trigger (use --force for a free-running capture)", file=sys.stderr)
            return 1
        cap = scope.read_many(chans)
    finally:
        scope.close()
    cp.save(args.output, cap)
    print(f"wrote {args.output}: {cap.n} samples, dt {cap.dt:.3g} s, channels {cap.names}")
    return 0


def cmd_filter(args) -> int:
    from . import afe
    f = afe.ReconstructionFilter.butterworth(args.fc) if args.fc != 600e3 else afe.ReconstructionFilter()
    print(f"4th-order Butterworth Sallen-Key, fc = {args.fc / 1e3:g} kHz (unity-gain stages)")
    for s in f.describe():
        print(f"  stage {s['stage']}: R1 {s['R1_ohm']:g} ohm, R2 {s['R2_ohm']:g} ohm, "
              f"C1 {s['C1_F'] * 1e12:g} pF (feedback), C2 {s['C2_F'] * 1e12:g} pF (to ground)"
              f"  -> f0 {s['f0_hz'] / 1e3:.1f} kHz, Q {s['Q']:.3f}")
    print("\n  freq      filter   filter x DAC hold")
    for fr in (100e3, 250e3, 400e3, 500e3, 600e3, 1e6, 1.5e6, 2e6, 2.5e6, 3.5e6):
        h = f.gain_db(fr)
        z = 20 * np.log10(afe.zoh_response(fr, args.fs_dac))
        print(f"  {fr / 1e3:6.0f} kHz  {h:7.2f} dB  {h + z:7.2f} dB")
    return 0


def cmd_presets(args) -> int:
    from . import waveforms as wf
    from .plan import get_plan
    print("pulse presets:")
    for name in wf.PRESETS:
        s = wf.preset(name)
        print(f"  {name:<14} {s.label}")
    print(f"\n{args.plan} plan:")
    for t in get_plan(args.plan):
        print(f"  {t.name:<14} [{t.kind}] {t.instruction()}")
    return 0


# ---- parser -------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sonarscope", description="Oscilloscope/FFT validation toolkit for the "
                                "software-defined sonar transmitter.")
    p.add_argument("--version", action="version", version=f"sonarscope {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def spec_args(sp):
        sp.add_argument("--preset", default="lfm_hi", help="named pulse (see 'sonarscope presets')")
        sp.add_argument("--spec", help="pulse as JSON text or a JSON file (overrides --preset)")

    def sim_args(sp):
        sp.add_argument("--node", default="TP4", choices=["TP1", "TP2", "TP3", "TP4"])
        sp.add_argument("--fault", action="append", metavar="KEY=VALUE",
                        help="chain non-ideality, e.g. filt=none, slew_rate=3e6, dac_inl_lsb=2, swap_pairs=true")
        sp.add_argument("--scope-bits", type=int, default=12)
        sp.add_argument("--scope-noise", type=float, default=0.3e-3, help="scope noise, V rms")
        sp.add_argument("--seed", type=int, default=0)

    s = sub.add_parser("simulate", help="write a simulated capture of a pulse")
    spec_args(s)
    sim_args(s)
    s.add_argument("--sample-rate", type=float, default=10e6)
    s.add_argument("--pings", type=int, default=1)
    s.add_argument("--pri", type=float, default=20e-3)
    s.add_argument("-o", "--output", required=True, help=".npz or .csv")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("analyze", help="analyze one capture file")
    s.add_argument("file")
    s.add_argument("--test", help="judge as this plan test (spec and criteria come from the plan)")
    spec_args(s)
    s.add_argument("--full-scale", type=float, help="full-scale amplitude (V) for floor tests")
    s.add_argument("--t0", type=float, help="time of the input change (transition tests)")
    s.add_argument("--plot", help="write a diagnostic PNG")
    s.add_argument("--json", help="write the result as JSON")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("suite", help="run a test plan on a bench and write report.html")
    s.add_argument("--bench", default="sim", choices=["sim", "files", "rigol", "siglent", "ad3"])
    s.add_argument("--resource", help="VISA resource for rigol/siglent")
    s.add_argument("--captures", help="directory of <test>.npz files for --bench files")
    s.add_argument("--dut", default="none", choices=["none", "manual", "serial"])
    s.add_argument("--port", help="serial port for --dut serial")
    s.add_argument("--baud", type=int, default=115200)
    s.add_argument("--plan", default="default", choices=["default", "quick"])
    s.add_argument("--only", help="comma-separated test names")
    s.add_argument("--out", help="output directory (default out/<timestamp>)")
    s.add_argument("--history", help="results history directory: record this run and compare with the last one")
    s.add_argument("--full-scale", type=float, help="full-scale amplitude at the node (V)")
    s.add_argument("--sample-rate", type=float, default=10e6)
    s.add_argument("--trigger-level", type=float, default=0.1)
    s.add_argument("--inject-mixed", action="store_true", help="simulated bench: inject a mixed ping")
    s.add_argument("--no-plots", action="store_true")
    sim_args(s)
    s.set_defaults(func=cmd_suite)

    s = sub.add_parser("compare", help="compare two results.json files")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("selfcheck", help="verify the analyzer on the reference model and injected faults")
    s.add_argument("--out", help="also write the reference report here")
    s.set_defaults(func=cmd_selfcheck)

    s = sub.add_parser("capture", help="grab one acquisition from a scope")
    s.add_argument("--scope", required=True, choices=["rigol", "siglent", "ad3"])
    s.add_argument("--resource")
    s.add_argument("--channels", default="CH1")
    s.add_argument("--record", type=float, default=3e-3, help="record length, s")
    s.add_argument("--pre", type=float, default=100e-6, help="pre-trigger, s")
    s.add_argument("--sample-rate", type=float, default=10e6)
    s.add_argument("--trigger-channel", default="CH1")
    s.add_argument("--trigger-level", type=float, default=0.1)
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--force", action="store_true", help="free-running capture (no trigger)")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("filter", help="print the reconstruction filter design and response")
    s.add_argument("--fc", type=float, default=600e3)
    s.add_argument("--fs-dac", type=float, default=2e6)
    s.set_defaults(func=cmd_filter)

    s = sub.add_parser("presets", help="list pulse presets and the test plan")
    s.add_argument("--plan", default="default", choices=["default", "quick"])
    s.set_defaults(func=cmd_presets)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
