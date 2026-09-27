import copy
import json

import pytest

from sonarscope import regression, report, suite
from sonarscope import thresholds as th
from sonarscope.backends import FileBench, SimulatedBench
from sonarscope.chain import ChainConfig
from sonarscope.plan import default_plan, quick_plan


@pytest.fixture(scope="module")
def quick_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    res = suite.run_suite(quick_plan(), SimulatedBench(), out_dir=out, progress=lambda *a: None)
    return out, res


def test_quick_plan_passes_on_reference_chain(quick_run):
    out, res = quick_run
    assert res["verdict"] == th.PASS, [(t["name"], t["verdict"]) for t in res["tests"]]
    assert (out / "results.json").exists() and (out / "report.html").exists()
    assert (out / "plots" / "lfm_hi.png").exists()
    assert (out / "captures" / "transition.npz").exists()
    assert res["context"]["floor_dbc"] < -48


def test_report_html_embeds_plots_and_verdicts(quick_run):
    out, res = quick_run
    page = (out / "report.html").read_text()
    assert "data:image/png;base64," in page
    assert "Transmitter validation" in page and 'class="v pass"' in page
    assert "<script" not in page


def test_faulty_bench_fails_and_reports_why(tmp_path):
    bench = SimulatedBench(ChainConfig(node="TP4", filt=None))
    plan = [t for t in default_plan() if t.name in ("lfm_hi", "tone_487k")]
    res = suite.run_suite(plan, bench, out_dir=tmp_path, progress=lambda *a: None, make_plots=False)
    assert res["verdict"] == th.FAIL
    failing = {c["metric"] for t in res["tests"] for c in t["criteria"] if c["verdict"] == th.FAIL}
    assert "image_dbc" in failing


def test_injected_mixed_ping_fails_transition(tmp_path):
    plan = [t for t in default_plan() if t.name == "transition"]
    res = suite.run_suite(plan, SimulatedBench(inject_mixed=True), progress=lambda *a: None)
    assert res["tests"][0]["verdict"] == th.FAIL
    assert res["tests"][0]["metrics"]["n_mixed"] == 1


def test_file_bench_replays_saved_run(quick_run, tmp_path):
    out, res = quick_run
    replay = suite.run_suite(quick_plan(), FileBench(out / "captures"), progress=lambda *a: None)
    assert replay["verdict"] == th.PASS
    a = {t["name"]: t["metrics"].get("image_dbc") for t in res["tests"]}
    b = {t["name"]: t["metrics"].get("image_dbc") for t in replay["tests"]}
    assert a == pytest.approx(b)


def test_floor_runs_after_first_tone_without_full_scale():
    names = [t.name for t in suite.order_plan(default_plan(), have_full_scale=False)]
    assert names.index("floor") == names.index("tone_101k") + 1
    assert [t.name for t in suite.order_plan(default_plan(), True)][0] == "floor"


def test_missing_captures_are_skipped(tmp_path):
    res = suite.run_suite(quick_plan(), FileBench(tmp_path), progress=lambda *a: None)
    assert all(t["verdict"] == suite.SKIPPED for t in res["tests"])


def test_regression_compare(quick_run, tmp_path):
    _, res = quick_run
    worse = copy.deepcopy(res)
    for t in worse["tests"]:
        if t["name"] == "lfm_hi":
            t["metrics"]["image_dbc"] += 5.0
            t["metrics"]["freq_r2"] -= 0.01
            t["verdict"] = th.FAIL
    cmp = regression.compare(res, worse)
    flagged = {(r["test"], r["metric"]) for r in cmp["regressions"]}
    assert ("lfm_hi", "image_dbc") in flagged and ("lfm_hi", "freq_r2") in flagged
    assert not cmp["ok"] and cmp["verdict_changes"][0]["new"] == th.FAIL
    assert regression.compare(res, res)["ok"]
    back = regression.compare(worse, res)
    assert {(r["test"], r["metric"]) for r in back["improvements"]} >= flagged


def test_regression_history(quick_run, tmp_path):
    out, _ = quick_run
    dest = regression.record(out / "results.json", tmp_path / "hist")
    assert dest.exists() and json.loads(dest.read_text())["verdict"] == th.PASS
    assert regression.latest(tmp_path / "hist") == dest
    assert regression.latest(tmp_path / "hist", exclude=dest) is None


def test_render_html_handles_skipped_and_errors():
    res = {"verdict": "FAIL", "tests": [
        {"name": "x", "kind": "pulse", "verdict": "SKIPPED", "reason": "no file", "criteria": [], "metrics": {}},
        {"name": "y", "kind": "tone", "verdict": "ERROR", "reason": "boom <b>", "criteria": [], "metrics": {}}]}
    page = report.render_html(res)
    assert "no file" in page and "boom &lt;b&gt;" in page
