import numpy as np
import pytest

from sonarscope import afe, chain
from sonarscope import waveforms as wf
from sonarscope.chain import ChainConfig, ScopeModel


def test_default_filter_parts_and_response():
    f = afe.ReconstructionFilter()
    parts = f.describe()
    assert (parts[0]["R1_ohm"], parts[0]["R2_ohm"]) == (1300.0, 511.0)
    assert (parts[1]["R1_ohm"], parts[1]["R2_ohm"]) == (1740.0, 715.0)
    assert parts[0]["Q"] == pytest.approx(0.541, abs=0.005)
    assert parts[1]["Q"] == pytest.approx(1.307, abs=0.01)
    g = f.gain_db([100e3, 500e3, 600e3, 1.5e6, 2.5e6])
    assert g[0] == pytest.approx(0.0, abs=0.01)
    assert g[1] == pytest.approx(-0.89, abs=0.05)
    assert g[2] == pytest.approx(-2.92, abs=0.1)
    assert g[3] == pytest.approx(-31.66, abs=0.2)
    assert g[4] == pytest.approx(-49.41, abs=0.3)


def test_design_rejects_impossible_capacitor_ratio():
    with pytest.raises(ValueError):
        afe.SallenKeyStage.design(600e3, 1.3066, 100e-12, 100e-12)


def test_discrete_model_matches_analog_magnitude():
    f = afe.ReconstructionFilter()
    fs = 40e6
    for ftone in (200e3, 500e3, 1.5e6):
        t = np.arange(int(2e-3 * fs)) / fs
        y = f.apply(np.sin(2 * np.pi * ftone * t), fs)
        amp = np.sqrt(2) * np.std(y[len(y) // 2:])
        assert 20 * np.log10(amp) == pytest.approx(f.gain_db(ftone), abs=0.3)


def test_zoh_and_images():
    assert afe.zoh_response(500e3, 2e6) == pytest.approx(0.9003, abs=1e-3)
    assert afe.image_frequencies(500e3, 2e6, 1) == [1.5e6, 2.5e6]


def test_oversample_choice():
    assert chain._choose_oversample(2e6, 10e6) == (20, 4)
    assert chain._choose_oversample(2e6, 5e6) == (20, 8)
    with pytest.raises(ValueError):
        chain._choose_oversample(2e6, 1e9)


def _spectrum_dbc_above(cap, f_lim):
    x = cap.ch()
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)), 1 << 20))
    f = np.fft.rfftfreq(1 << 20, cap.dt)
    return 20 * np.log10(X[f > f_lim].max() / X.max())


def test_tp3_images_match_reference_model():
    cap = chain.simulate(wf.preset("lfm_hi"), ChainConfig(node="TP3"), ScopeModel(fs=10e6, bits=16))
    # reference model: images ~ -41.8 dBc for the 400-500 kHz chirp
    assert _spectrum_dbc_above(cap, 1.0e6) == pytest.approx(-41.8, abs=2.0)


def test_tp1_has_strong_images():
    cap = chain.simulate(wf.preset("lfm_hi"), ChainConfig(node="TP1"), ScopeModel(fs=10e6, bits=16))
    x = cap.ch() - np.mean(cap.ch())
    X = np.abs(np.fft.rfft(x, 1 << 20))
    f = np.fft.rfftfreq(1 << 20, cap.dt)
    assert 20 * np.log10(X[f > 1e6].max() / X.max()) > -15


def test_tp4_amplitude_and_attenuation():
    spec = wf.PulseSpec("cw", 250e3, 250e3, 2e-3, "tukey")
    cap = chain.simulate(spec, ChainConfig(node="TP4", driver_gain=5), ScopeModel(bits=16))
    peak = np.max(np.abs(cap.ch()))
    expected = 5 * 3.3 / 255 * 127 * abs(afe.ReconstructionFilter().response(250e3))
    assert peak == pytest.approx(expected, rel=0.03)
    cap6 = chain.simulate(spec, ChainConfig(node="TP4", driver_gain=5, attenuation_db=6.0206), ScopeModel(bits=16))
    assert np.max(np.abs(cap6.ch())) == pytest.approx(peak / 2, rel=0.03)


def test_idle_node_is_zero_and_offset_applies():
    cap = chain.idle_capture(ChainConfig(node="TP4"), ScopeModel(bits=16))
    assert np.max(np.abs(cap.ch())) < 1e-3
    cap = chain.idle_capture(ChainConfig(node="TP4", dc_offset_v=0.015), ScopeModel(bits=16))
    assert np.mean(cap.ch()) == pytest.approx(0.015, abs=1e-3)


def test_multi_ping_timeline_and_pri():
    spec = wf.preset("lfm_hi")
    cap = chain.simulate([spec] * 3, ChainConfig(), ScopeModel(), pri=5e-3)
    x = np.abs(cap.ch())
    t = cap.time
    for k in range(3):
        on = (t > k * 5e-3 + 0.5e-3) & (t < k * 5e-3 + 1.5e-3)
        assert x[on].max() > 0.5 * x.max()
    for k in range(2):
        off = (t > k * 5e-3 + 2.5e-3) & (t < k * 5e-3 + 4.5e-3)
        assert x[off].max() < 0.02 * x.max()


def test_t0_channel_step():
    cap = chain.simulate(wf.preset("lfm_hi"), t0_channel=1e-3)
    t0 = cap.ch("T0")
    assert t0[cap.time < 1e-3 - 1e-7].max() == 0
    assert t0[cap.time > 1e-3 + 1e-7].min() == pytest.approx(3.3)


def test_slew_limit_distorts_high_frequency():
    spec = wf.PulseSpec("cw", 487.3e3, 487.3e3, 2e-3, "tukey")
    clean = chain.simulate(spec, ChainConfig(node="TP4"), ScopeModel(bits=16))
    slewed = chain.simulate(spec, ChainConfig(node="TP4", slew_rate=0.3e6 * 10), ScopeModel(bits=16))
    assert np.max(np.abs(slewed.ch())) < 0.8 * np.max(np.abs(clean.ch()))


def test_transition_schedule():
    old, new = wf.preset("clear_reef"), wf.preset("muddy_estuary")
    specs, t_new = chain.transition_schedule(old, new, pri=20e-3, n_pings=10, t_change=31e-3,
                                             adc_period=10e-3, synth_time=3e-3)
    # seen at 40 ms, ready at 43 ms, first ping boundary after that is 60 ms (ping 3)
    assert t_new == pytest.approx(60e-3)
    assert specs[:3] == [old] * 3 and specs[3:] == [new] * 7


def test_segments_carry_timestamps_and_mixed_fault():
    a, b = wf.preset("lfm_hi"), wf.preset("lfm_lo")
    segs = chain.simulate_segments([a, a, b, b], pri=10e-3, mixed_index=2)
    assert [s.timestamp for s in segs] == [0.0, 0.01, 0.02, 0.03]
    assert segs[2].meta.get("injected_fault") == "mixed"


def test_jitter_shifts_time_axis():
    spec = wf.preset("lfm_hi")
    a = chain.simulate(spec, scope=ScopeModel(trigger_jitter_s=0), seed=1)
    b = chain.simulate(spec, scope=ScopeModel(trigger_jitter_s=1e-6), seed=1)
    assert a.t0 != b.t0
