"""Compare two result files and flag metrics that got worse."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import thresholds as th

# absolute tolerance by unit before a change counts as a regression
TOLERANCE = {"dB": 1.0, "dBc": 1.0, "%": 0.25, "s": 50e-9, "V": 2e-3, "": 0.0}
R2_TOLERANCE = 5e-4


def _tol(c: th.Criterion) -> float:
    if c.metric == "freq_r2":
        return R2_TOLERANCE
    if c.metric in ("latency_s",):
        return 1e-3
    if c.metric in ("rise_ratio", "fall_ratio", "window_rms_error", "symmetry"):
        return 0.02
    return TOLERANCE.get(c.unit, 0.0)


def _worse(c: th.Criterion, old: float, new: float, tol: float) -> bool:
    if c.op == "<=":
        return new > old + tol
    if c.op == ">=":
        return new < old - tol
    if c.op == "abs<=":
        return abs(new) > abs(old) + tol
    if c.op == "==":
        return abs(new - c.limit) > abs(old - c.limit)
    if c.op == "in":
        mid = 0.5 * (c.limit[0] + c.limit[1])
        return abs(new - mid) > abs(old - mid) + tol
    return False


def compare(old: dict, new: dict) -> dict:
    lib = th.all_criteria()
    old_tests = {t["name"]: t for t in old.get("tests", [])}
    regressions, improvements, verdict_changes = [], [], []
    for t in new.get("tests", []):
        o = old_tests.get(t["name"])
        if o is None:
            continue
        if o["verdict"] != t["verdict"]:
            verdict_changes.append({"test": t["name"], "old": o["verdict"], "new": t["verdict"]})
        for row in t.get("criteria", []):
            m = row["metric"]
            c = lib.get(m)
            ov, nv = o.get("metrics", {}).get(m), t.get("metrics", {}).get(m)
            if c is None or not isinstance(ov, (int, float)) or not isinstance(nv, (int, float)):
                continue
            item = {"test": t["name"], "metric": m, "old": ov, "new": nv}
            tol = _tol(c)
            if _worse(c, ov, nv, tol):
                regressions.append(item)
            elif _worse(c, nv, ov, tol):
                improvements.append(item)
    bad_changes = [v for v in verdict_changes if v["old"] == th.PASS and v["new"] != th.PASS]
    return {"old_revision": old.get("revision"), "new_revision": new.get("revision"),
            "regressions": regressions, "improvements": improvements,
            "verdict_changes": verdict_changes, "ok": not regressions and not bad_changes}


def compare_files(old_path: str | Path, new_path: str | Path) -> dict:
    return compare(json.loads(Path(old_path).read_text()), json.loads(Path(new_path).read_text()))


def record(results_path: str | Path, history_dir: str | Path) -> Path:
    """Copy a results file into the history directory as <revision>_<timestamp>.json."""
    res = json.loads(Path(results_path).read_text())
    rev = (res.get("revision") or "norev").replace("/", "_")
    stamp = res.get("timestamp", "").replace(":", "").replace("-", "")
    dest = Path(history_dir) / f"{rev}_{stamp}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(results_path, dest)
    return dest


def latest(history_dir: str | Path, exclude: Path | None = None) -> Path | None:
    files = sorted(Path(history_dir).glob("*.json"), key=lambda p: p.stat().st_mtime)
    files = [f for f in files if exclude is None or f.resolve() != Path(exclude).resolve()]
    return files[-1] if files else None
