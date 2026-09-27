"""Run a test plan on a bench and collect results."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path

from . import __version__, analyze
from . import capture as cp
from . import thresholds as th
from .backends.base import Bench, NotSupported
from .plan import TestCase

SKIPPED = "SKIPPED"
ERROR = "ERROR"


def git_revision(path: str | Path = ".") -> str | None:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=path, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if not sha:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=path,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def order_plan(plan: list[TestCase], have_full_scale: bool) -> list[TestCase]:
    """Without a full-scale reference the floor test runs after the first tone test."""
    if have_full_scale:
        return list(plan)
    floor = [t for t in plan if t.kind == "floor"]
    rest = [t for t in plan if t.kind != "floor"]
    first_tone = next((i for i, t in enumerate(rest) if t.kind == "tone"), None)
    if not floor or first_tone is None:
        return rest
    return rest[: first_tone + 1] + floor + rest[first_tone + 1:]


def overall_verdict(tests: list[dict]) -> str:
    verdicts = {t["verdict"] for t in tests}
    if th.FAIL in verdicts or ERROR in verdicts:
        return th.FAIL
    if th.INCONCLUSIVE in verdicts:
        return th.INCONCLUSIVE
    return th.PASS


def run_suite(plan: list[TestCase], bench: Bench, *, out_dir: str | Path | None = None,
              save_captures: bool = True, make_plots: bool = True, progress=print) -> dict:
    out = Path(out_dir) if out_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    context: dict = {}
    if bench.full_scale_v:
        context["full_scale_v"] = bench.full_scale_v
    tests = []
    for test in order_plan(plan, bool(bench.full_scale_v)):
        try:
            acq = bench.acquire(test)
        except NotSupported as e:
            tests.append({"name": test.name, "kind": test.kind, "description": test.description,
                          "verdict": SKIPPED, "reason": str(e), "criteria": [], "metrics": {}})
            progress(f"  {test.name:<14} {SKIPPED}  ({e})")
            continue
        ctx = dict(context)
        ctx.update(acq.context)
        if out and save_captures:
            first = acq.data[0] if isinstance(acq.data, list) else acq.data
            if "t0" in acq.context:
                first.meta["t0"] = acq.context["t0"]
            (out / "captures").mkdir(parents=True, exist_ok=True)
            cp.save(out / "captures" / f"{test.name}.npz", acq.data)
        try:
            result = analyze.run_test(test, acq.data, ctx)
        except Exception as e:  # keep going: one broken capture must not hide the others
            tests.append({"name": test.name, "kind": test.kind, "description": test.description,
                          "verdict": ERROR, "reason": f"{type(e).__name__}: {e}", "criteria": [],
                          "metrics": {}})
            progress(f"  {test.name:<14} {ERROR}  ({e})")
            continue
        for key in ("floor_dbc", "full_scale_v"):
            if key in ctx and key not in context:
                context[key] = ctx[key]
        if "t0" in acq.context:
            result["t0"] = acq.context["t0"]
        if out and make_plots:
            from . import plots
            png = plots.render(test, acq.data, result, out / "plots" / f"{test.name}.png")
            if png:
                result["plot"] = str(png.relative_to(out))
        tests.append(result)
        progress(f"  {test.name:<14} {result['verdict']}")
    summary = {}
    for t in tests:
        summary[t["verdict"]] = summary.get(t["verdict"], 0) + 1
    results = {
        "toolkit": f"sonarscope {__version__}",
        "revision": git_revision(Path(__file__).parent),
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "bench": bench.describe(),
        "context": {k: v for k, v in context.items() if k in ("floor_dbc", "full_scale_v")},
        "verdict": overall_verdict(tests),
        "summary": summary,
        "tests": tests,
    }
    if out:
        (out / "results.json").write_text(json.dumps(results, indent=1, default=_default))
        from . import report
        report.write_html(results, out / "report.html")
    return results


def _default(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
    except ImportError:  # pragma: no cover
        pass
    return str(o)
