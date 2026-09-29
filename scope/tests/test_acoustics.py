import pytest

from sonarscope import acoustics as ac
from sonarscope import waveforms as wf


def test_mackenzie_check_value():
    # Mackenzie (1981) gives 1550.744 m/s at 25 degC, 35 PSU, 1000 m
    assert ac.sound_speed(25, 35, 1000) == pytest.approx(1550.744, abs=1e-3)


def test_francois_garrison_magnitudes():
    # published seawater absorption at 10 degC: about 1 dB/km at 10 kHz, 30-40 at 100 kHz, 110-140 at 500 kHz
    assert ac.seawater_absorption(10e3, 10, 35, 0) == pytest.approx(1.0, abs=0.2)
    assert 30 < ac.seawater_absorption(100e3, 10, 35, 0) < 40
    assert 110 < ac.seawater_absorption(500e3, 10, 35, 0) < 140
    # fresh water lacks the boric-acid and MgSO4 relaxations
    assert ac.seawater_absorption(100e3, 25, 0, 0) < 0.1 * ac.seawater_absorption(100e3, 25, 35, 0)


def test_band_edges_match_presets():
    assert ac.bandwidth_for(ac.FC_MAX_HZ) == pytest.approx(100e3)
    assert ac.bandwidth_for(ac.FC_MIN_HZ) == pytest.approx(40e3)


def test_turbidity_moves_the_band_down():
    clear = ac.decide(ac.Environment(0, 60))
    muddy = ac.decide(ac.Environment(100, 60))
    assert clear.fc_hz > 400e3 and muddy.fc_hz == ac.FC_MIN_HZ
    fcs = [ac.decide(ac.Environment(ntu, 60)).fc_hz for ntu in range(0, 101, 10)]
    assert all(a >= b for a, b in zip(fcs, fcs[1:]))


def test_band_keeps_absorption_within_budget_unless_limited():
    for ntu in (0, 30, 100):
        for rng in (5, 20, 60, 200):
            d = ac.decide(ac.Environment(ntu, rng))
            assert d.range_limited or d.absorption_2way_db <= ac.ABSORPTION_BUDGET_DB + 1e-9
            if d.range_limited:
                assert d.fc_hz == ac.FC_MIN_HZ


def test_range_sets_energy_and_ping_interval():
    near, far = ac.decide(ac.Environment(0, 20)), ac.decide(ac.Environment(100, 200))
    assert near.spec.duration < far.spec.duration and far.spec.amplitude == 1.0
    for d, rng in ((near, 20), (far, 200)):
        assert d.pri_s >= 2 * rng / d.sound_speed  # the echo returns before the next ping
    assert near.energy_db < far.energy_db


def test_short_range_uses_barker_and_keeps_blind_zone_small():
    d = ac.decide(ac.Environment(10, 5))
    assert d.spec.kind == "barker13"
    assert d.blind_zone_m <= ac.BLIND_FRACTION * 5 + 1e-9


def test_manual_modes_and_valid_pulses():
    env = ac.Environment(40, 80, 28, 30, 50)
    for mod in ac.MODULATIONS:
        for win in ("auto", "rect", "blackman"):
            d = ac.decide(env, mod, win)
            assert mod == "auto" or d.spec.kind == mod
            codes = wf.dac_codes(d.spec)  # the firmware can synthesise it
            assert 2 <= len(codes) <= 20000
    with pytest.raises(ValueError):
        ac.decide(env, "fm")
