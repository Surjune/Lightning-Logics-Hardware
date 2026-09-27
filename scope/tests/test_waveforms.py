import numpy as np
import pytest
from scipy import signal

from sonarscope import waveforms as wf
from sonarscope.waveforms import PulseSpec


def test_lfm_instantaneous_frequency_matches_law():
    spec = PulseSpec("lfm", 400e3, 500e3, 2e-3, "rect")
    fs = 20e6
    x = wf.synthesize(spec, fs)
    ph = np.unwrap(np.angle(signal.hilbert(x)))
    f = np.diff(ph) * fs / (2 * np.pi)
    t = (np.arange(len(f)) + 0.5) / fs
    core = slice(len(f) // 10, -len(f) // 10)
    err = np.abs(f[core] - wf.inst_freq(spec, t[core]))
    assert np.median(err) < 500  # Hz


def test_down_chirp_sweeps_downward():
    spec = PulseSpec("lfm", 500e3, 400e3, 2e-3)
    assert wf.inst_freq(spec, 0.0) == pytest.approx(500e3)
    assert wf.inst_freq(spec, spec.duration) == pytest.approx(400e3)


def test_geometric_law_endpoints_and_ratio():
    spec = PulseSpec("geometric", 100e3, 500e3, 2e-3)
    T = spec.duration
    assert wf.inst_freq(spec, 0.0) == pytest.approx(100e3)
    assert wf.inst_freq(spec, T) == pytest.approx(500e3)
    # equal time steps give equal frequency ratios
    r1 = wf.inst_freq(spec, T / 4) / wf.inst_freq(spec, 0)
    r2 = wf.inst_freq(spec, T / 2) / wf.inst_freq(spec, T / 4)
    assert r1 == pytest.approx(r2)
    # phase derivative equals 2*pi*f(t)
    t = np.linspace(0, T, 1001)
    dphi = np.gradient(wf.phase(spec, t), t)
    assert np.allclose(dphi[5:-5], 2 * np.pi * wf.inst_freq(spec, t[5:-5]), rtol=1e-3)


def test_barker_duration_is_13_chips_and_code_signs():
    spec = PulseSpec("barker13", 300e3, 300e3, 1.0, chip=40e-6)
    assert spec.duration == pytest.approx(13 * 40e-6)
    t = (np.arange(13) + 0.5) * spec.chip
    assert np.array_equal(wf.barker_code(spec, t, 2e6), wf.BARKER13)


def test_barker_rc_shaping_smooths_transitions():
    hard = PulseSpec("barker13", 300e3, 300e3, chip=40e-6, rc_shaping=0.0)
    soft = hard.replace(rc_shaping=0.3)
    fs = 2e6
    t = np.arange(int(13 * 40e-6 * fs)) / fs
    assert np.max(np.abs(np.diff(wf.barker_code(hard, t, fs)))) == pytest.approx(2.0)
    assert np.max(np.abs(np.diff(wf.barker_code(soft, t, fs)))) < 1.0


@pytest.mark.parametrize("name", wf.WINDOWS)
def test_windows_are_symmetric_and_peak_one(name):
    w = wf.window(name, 1001)
    assert np.allclose(w, w[::-1])
    assert w.max() == pytest.approx(1.0, abs=1e-3)


def test_window_energy_figures():
    assert wf.window_energy_db("rect") == pytest.approx(0.0)
    assert wf.window_energy_db("hann") == pytest.approx(-4.26, abs=0.02)
    assert wf.window_energy_db("tukey", 0.2) == pytest.approx(-0.58, abs=0.02)


def test_quantize_midscale_and_range():
    codes = wf.quantize(np.array([-1.0, 0.0, 1.0, 2.0]))
    assert list(codes) == [1, 128, 255, 255]
    assert np.allclose(wf.codes_to_unit(wf.quantize(np.array([0.5]))), 0.5, atol=1 / 127)


def test_dac_codes_length_and_idle_edges():
    spec = PulseSpec("lfm", 400e3, 500e3, 2e-3, "tukey")
    codes = wf.dac_codes(spec)
    assert len(codes) == 4000
    assert codes[0] == 128 and codes[-1] == 128  # tapered to mid-scale: no step


def test_amplitude_scales_codes():
    spec = PulseSpec("lfm", 400e3, 500e3, 2e-3, "rect", amplitude=0.5)
    codes = wf.dac_codes(spec)
    assert codes.max() <= 128 + 64 and codes.min() >= 128 - 64


def test_spec_json_roundtrip():
    spec = wf.preset("barker13")
    assert PulseSpec.from_json(spec.to_json()) == spec


@pytest.mark.parametrize("kwargs", [
    dict(kind="chirp"), dict(window="kaiser"), dict(duration=0), dict(amplitude=1.5),
    dict(kind="geometric", f0=100e3, f1=100e3),
])
def test_invalid_specs_rejected(kwargs):
    with pytest.raises(ValueError):
        PulseSpec(**kwargs)


def test_all_presets_build():
    for name in wf.PRESETS:
        spec = wf.preset(name)
        assert len(wf.dac_codes(spec)) > 0
