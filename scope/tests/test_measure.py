"""Measurements against the simulated chain: reference values and injected faults."""

import functools

import numpy as np
import pytest

from sonarscope import chain
from sonarscope import measure as M
from sonarscope import waveforms as wf
from sonarscope.chain import ChainConfig, ScopeModel

SCOPE = ScopeModel(fs=10e6, bits=12, noise_vrms=0.3e-3)


@functools.lru_cache(maxsize=None)
def sim(name, node="TP3", **faults):
    return chain.simulate(wf.preset(name), ChainConfig(node=node, **faults), SCOPE)


# ---- envelope -------------------------------------------------------------------------------------
def test_tukey_chirp_envelope_is_clean():
    e = M.envelope_metrics(sim("lfm_hi"), wf.preset("lfm_hi"))
    assert e["edge_step_pct"] < 1.0
    assert e["window_rms_error"] < 0.01
    assert e["symmetry"] > 0.99
    assert 0.9 < e["rise_ratio"] < 1.1 and 0.9 < e["fall_ratio"] < 1.1
    assert abs(e["pulse_start_s"]) < 2e-6
    assert abs(e["idle_dc_v"]) < 2e-3


def test_rectangular_pulse_edge_step_detected():
    e = M.envelope_metrics(sim("lfm_hi_rect"), wf.preset("lfm_hi_rect"))
    assert e["edge_step_pct"] > 50


def test_hamming_pedestal_detected():
    spec = wf.PulseSpec("lfm", 400e3, 500e3, 2e-3, "hamming")
    e = M.envelope_metrics(chain.simulate(spec, ChainConfig(), SCOPE), spec)
    assert 5 < e["edge_step_pct"] < 12


def test_dc_offset_measured_in_idle_region():
    spec = wf.preset("lfm_hi")
    cap = chain.simulate(spec, ChainConfig(node="TP4", dc_offset_v=0.015), SCOPE)
    assert M.envelope_metrics(cap, spec)["idle_dc_v"] == pytest.approx(0.015, abs=2e-3)


# ---- frequency ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["lfm_hi", "lfm_lo", "lfm_down", "lfm_full"])
def test_lfm_linearity_and_endpoints(name):
    spec = wf.preset(name)
    f = M.frequency_metrics(sim(name), spec)
    assert f["freq_r2"] > 0.9999
    assert abs(f["sweep_rate_error_pct"]) < 0.2
    assert abs(f["f_start_error_pct"]) < 0.2 and abs(f["f_end_error_pct"]) < 0.2
    assert abs(f["bw10_error_pct"]) < 3


def test_geometric_sweep_fit():
    f = M.frequency_metrics(sim("geometric"), wf.preset("geometric"))
    assert f["freq_r2"] > 0.999
    assert abs(f["sweep_rate_error_pct"]) < 0.5


def test_droop_tracks_chain_response():
    hi = M.frequency_metrics(sim("lfm_hi"), wf.preset("lfm_hi"))["droop_db"]
    lo = M.frequency_metrics(sim("lfm_lo"), wf.preset("lfm_lo"))["droop_db"]
    assert -1.0 < hi < -0.2
    assert abs(lo) < 0.15


def test_frequency_metrics_skip_non_sweeps():
    assert M.frequency_metrics(sim("barker13"), wf.preset("barker13")) == {}


# ---- spurs and tones ------------------------------------------------------------------------------------
def test_image_level_matches_reference_model():
    s = M.spur_metrics(sim("lfm_hi"), wf.preset("lfm_hi"))
    assert -45.5 < s["image_dbc"] < -39.5
    assert 1.45e6 < s["image_peak_hz"] < 1.65e6
    lo = M.spur_metrics(sim("lfm_lo"), wf.preset("lfm_lo"))
    assert lo["image_dbc"] < -55


def test_missing_filter_shows_images():
    s = M.spur_metrics(sim("lfm_hi", filt=None), wf.preset("lfm_hi"))
    assert s["image_dbc"] > -15


def test_tone_487k_thd_and_image():
    t = M.tone_metrics(sim("tone_487k"), wf.preset("tone_487k"))
    assert t["thd_db"] < -60
    assert t["tone_image_dbc"] == pytest.approx(-41.1, abs=2.0)
    assert t["harmonic_image_coincidence"] == []


def test_tone_500k_reports_image_on_third_harmonic():
    t = M.tone_metrics(sim("tone_500k"), wf.preset("tone_500k"))
    assert t["thd_db"] == pytest.approx(-40.3, abs=2.0)
    assert any(c["harmonic"] == 3 for c in t["harmonic_image_coincidence"])


def test_dac_nonlinearity_raises_thd():
    clean = M.tone_metrics(sim("tone_250k"), wf.preset("tone_250k"))["thd_db"]
    bowed = M.tone_metrics(sim("tone_250k", dac_inl_lsb=2.0), wf.preset("tone_250k"))["thd_db"]
    assert bowed > clean + 10


def test_slew_limited_driver_detected():
    spec = wf.preset("tone_487k")
    cap = chain.simulate(spec, ChainConfig(node="TP4", slew_rate=3e6), SCOPE)
    assert M.tone_metrics(cap, spec)["thd_db"] > -30


def test_swapped_sample_pairs_create_spur():
    t = M.tone_metrics(sim("tone_250k", swap_pairs=True), wf.preset("tone_250k"))
    assert t["sfdr_dbc"] > -30


# ---- compression --------------------------------------------------------------------------------------
def test_sidelobes_match_reference_model():
    rect = M.compression_metrics(sim("lfm_hi_rect"), wf.preset("lfm_hi_rect"))
    hann = M.compression_metrics(sim("lfm_hi_hann"), wf.preset("lfm_hi_hann"))
    tuk = M.compression_metrics(sim("lfm_hi"), wf.preset("lfm_hi"), rx_weight="hamming")
    assert rect["psl_matched_db"] == pytest.approx(-13.4, abs=1.0)
    assert hann["psl_matched_db"] == pytest.approx(-46.7, abs=3.0)
    assert tuk["psl_weighted_db"] == pytest.approx(-31.1, abs=2.0)
    assert -1.3 < tuk["mismatch_loss_db"] < -0.6
    # range resolution ~ c / (2B) for the unweighted chirp
    assert tuk["range_resolution_matched_m"] == pytest.approx(1500 / (2 * 100e3), rel=0.5)


def test_barker_sidelobes():
    b = M.compression_metrics(sim("barker13"), wf.preset("barker13"), rx_weight=None)
    assert -19.0 < b["psl_matched_db"] < -14.0


# ---- PRI and transitions ------------------------------------------------------------------------------------
def test_pri_measurement():
    spec = wf.preset("lfm_hi")
    cap = chain.simulate([spec] * 4, ChainConfig(), SCOPE, pri=5e-3)
    p = M.pri_metrics(cap, spec)
    assert p["n_pings"] == 4
    assert p["pri_mean_s"] == pytest.approx(5e-3, abs=10e-9)
    assert p["pri_jitter_pp_s"] < 20e-9


def _transition(mixed=None):
    old, new = wf.preset("clear_reef"), wf.preset("muddy_estuary")
    specs, t_new = chain.transition_schedule(old, new, pri=20e-3, n_pings=8, t_change=31e-3)
    segs = chain.simulate_segments(specs, pri=20e-3, scope=SCOPE, mixed_index=mixed)
    return old, new, segs, t_new


def test_clean_transition_has_no_mixed_pings_and_right_latency():
    old, new, segs, t_new = _transition()
    t = M.transition_metrics(segs, old, new, t0=31e-3)
    assert t["n_mixed"] == 0 and t["n_unknown"] == 0 and t["n_reverted"] == 0
    assert t["labels"] == ["old"] * 3 + ["new"] * 5
    assert t["latency_s"] == pytest.approx(t_new - 31e-3, abs=5e-6)


def test_mixed_ping_is_caught():
    old, new, segs, _ = _transition(mixed=3)
    t = M.transition_metrics(segs, old, new, t0=31e-3)
    assert t["n_mixed"] == 1 and t["labels"][3] == "mixed"


def test_segments_from_continuous_capture():
    spec = wf.preset("lfm_hi")
    cap = chain.simulate([spec] * 3, ChainConfig(), SCOPE, pri=5e-3)
    assert len(M.segments_from_capture(cap, spec)) == 3


def test_t0_channel_edge():
    cap = chain.simulate(wf.preset("lfm_hi"), t0_channel=1.0e-3)
    assert M.t0_from_channel(cap) == pytest.approx(1.0e-3, abs=2e-7)
    assert M.t0_from_channel(cap, "NOPE") is None


# ---- floor ---------------------------------------------------------------------------------------------
def test_floor_and_supply_spur():
    quiet = M.floor_metrics(chain.idle_capture(ChainConfig(), SCOPE), reference_amplitude_v=1.6)
    assert quiet["floor_dbc"] < -60
    spur = M.floor_metrics(chain.idle_capture(ChainConfig(supply_spur_hz=100e3, supply_spur_v=5e-3), SCOPE),
                           reference_amplitude_v=1.6)
    assert spur["floor_peak_hz"] == pytest.approx(100e3, rel=0.02)
    assert spur["floor_dbc"] > quiet["floor_dbc"] + 10


def test_mixed_ping_of_equal_length_pulses_is_caught():
    old = wf.preset("lfm_hi")
    new = old.replace(f0=300e3, f1=400e3)
    segs = chain.simulate_segments([old, old, new, new], pri=10e-3, scope=SCOPE, mixed_index=2)
    t = M.transition_metrics(segs, old, new)
    assert t["labels"] == ["old", "old", "mixed", "new"]


# ---- degenerate inputs --------------------------------------------------------------------------------
def test_metrics_survive_band_outside_capture():
    from sonarscope.capture import Capture
    rng = np.random.default_rng(0)
    cap = Capture(1 / 80e3, {"CH1": rng.normal(0, 1e-3, 4000)})
    assert M.spur_metrics(cap, wf.PulseSpec("lfm", 100e3, 200e3, 1e-3)) == {}
    fl = M.floor_metrics(cap, reference_amplitude_v=1.0)
    assert "floor_dbc" not in fl and "floor_rms_v" in fl


def test_pulse_touching_record_edge_cannot_pass_edge_check():
    spec = wf.preset("lfm_hi_rect")
    cap = chain.simulate(spec, ChainConfig(), SCOPE)
    cut = cap.slice_time(1e-6, cap.time[-1])       # record starts after the hard edge
    e = M.envelope_metrics(cut, spec)
    assert e["pulse_truncated"] and np.isnan(e["edge_step_pct"])
    from sonarscope import thresholds as th
    assert th.evaluate(e, th.EDGE[:1])["verdict"] == th.FAIL
