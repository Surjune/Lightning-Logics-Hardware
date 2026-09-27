import json

import pytest

from sonarscope import cli, selfcheck


def test_selfcheck_passes_every_check():
    res = selfcheck.run(progress=lambda *a: None)
    failed = [c for c in res["checks"] if not c["ok"]]
    assert res["ok"], failed
    groups = {c["group"] for c in res["checks"]}
    assert groups == {"reference", "clean chain", "discrimination"}
    assert sum(c["group"] == "discrimination" for c in res["checks"]) >= 8


def test_cli_simulate_then_analyze(tmp_path, capsys):
    cap = tmp_path / "hi.npz"
    assert cli.main(["simulate", "--preset", "lfm_hi", "-o", str(cap)]) == 0
    assert cli.main(["analyze", str(cap), "--test", "lfm_hi", "--plot", str(tmp_path / "hi.png"),
                     "--json", str(tmp_path / "hi.json")]) == 0
    out = capsys.readouterr().out
    assert "lfm_hi: PASS" in out and (tmp_path / "hi.png").exists()
    assert json.loads((tmp_path / "hi.json").read_text())["verdict"] == "PASS"


def test_cli_analyze_custom_spec_csv(tmp_path):
    cap = tmp_path / "t.csv"
    assert cli.main(["simulate", "--preset", "tone_250k", "-o", str(cap)]) == 0
    assert cli.main(["analyze", str(cap), "--preset", "tone_250k"]) == 0


def test_cli_fault_makes_analyze_fail(tmp_path):
    cap = tmp_path / "bad.npz"
    assert cli.main(["simulate", "--preset", "lfm_hi", "--fault", "filt=none", "-o", str(cap)]) == 0
    assert cli.main(["analyze", str(cap), "--test", "lfm_hi"]) == 1


def test_cli_suite_history_and_compare(tmp_path, capsys):
    hist = tmp_path / "hist"
    args = ["suite", "--plan", "quick", "--no-plots", "--history", str(hist)]
    assert cli.main(args + ["--out", str(tmp_path / "a")]) == 0
    assert cli.main(args + ["--out", str(tmp_path / "b")]) == 0
    assert "no regressions" in capsys.readouterr().out
    # a degraded chain is flagged against history and exits non-zero
    assert cli.main(args + ["--out", str(tmp_path / "c"), "--fault", "slew_rate=3e6", "--only",
                            "tone_487k,floor,idle"]) == 1
    assert cli.main(["compare", str(tmp_path / "a" / "results.json"), str(tmp_path / "b" / "results.json")]) == 0


def test_cli_suite_files_bench_replays(tmp_path):
    assert cli.main(["suite", "--plan", "quick", "--no-plots", "--out", str(tmp_path / "a")]) == 0
    assert cli.main(["suite", "--bench", "files", "--captures", str(tmp_path / "a" / "captures"), "--plan",
                     "quick", "--no-plots", "--out", str(tmp_path / "r")]) == 0


def test_cli_filter_and_presets(capsys):
    assert cli.main(["filter"]) == 0
    out = capsys.readouterr().out
    assert "R1 1300 ohm" in out and "1500 kHz" in out
    assert cli.main(["presets"]) == 0
    assert "muddy_estuary" in capsys.readouterr().out


def test_cli_rejects_bad_input(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["suite", "--only", "nope"])
    with pytest.raises(SystemExit):
        cli.main(["simulate", "--fault", "slew_rate", "-o", str(tmp_path / "x.npz")])
    with pytest.raises(SystemExit):
        cli.main(["suite", "--bench", "rigol"])
