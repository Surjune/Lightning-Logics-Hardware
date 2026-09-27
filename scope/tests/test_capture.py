import numpy as np
import pytest

from sonarscope import capture as cp
from sonarscope.capture import Capture


def _cap(n=100, dt=1e-7):
    t = np.arange(n) * dt
    return Capture(dt, {"CH1": np.sin(2e5 * t), "CH2": np.cos(2e5 * t)}, t0=-5e-6,
                   meta={"spec": {"kind": "lfm"}})


def test_basic_properties():
    c = _cap()
    assert c.n == 100 and c.fs == pytest.approx(1e7)
    assert c.time[0] == pytest.approx(-5e-6)
    assert c.names == ["CH1", "CH2"]
    assert np.array_equal(c.ch(), c.ch("CH1"))
    with pytest.raises(KeyError):
        c.ch("CH9")


def test_rejects_ragged_channels():
    with pytest.raises(ValueError):
        Capture(1e-7, {"A": np.zeros(3), "B": np.zeros(4)})


def test_slice_time():
    c = _cap()
    s = c.slice_time(0.0, 2e-6)
    assert s.n == 20 and s.t0 == pytest.approx(0.0)


def test_npz_roundtrip_single_and_segmented(tmp_path):
    c = _cap()
    cp.save(tmp_path / "a.npz", c)
    r = cp.load(tmp_path / "a.npz")
    assert isinstance(r, Capture)
    assert np.allclose(r.ch("CH2"), c.ch("CH2")) and r.meta == c.meta and r.t0 == c.t0
    segs = [_cap(), _cap()]
    segs[1].timestamp = 0.02
    cp.save(tmp_path / "s.npz", segs)
    rs = cp.load(tmp_path / "s.npz")
    assert isinstance(rs, list) and len(rs) == 2 and rs[1].timestamp == 0.02


def test_generic_csv_roundtrip(tmp_path):
    c = _cap()
    cp.save(tmp_path / "a.csv", c)
    r = cp.load(tmp_path / "a.csv")
    assert r.dt == pytest.approx(c.dt, rel=1e-6)
    assert r.t0 == pytest.approx(c.t0, rel=1e-6)
    assert np.allclose(r.ch("CH1"), c.ch("CH1"), atol=1e-5)


def test_rigol_style_csv():
    text = ("X,CH1,Start,Increment,\n"
            "Sequence,Volt,-1.000000e-05,1.000000e-07,\n"
            "0,1.20e-01,\n1,1.40e-01,\n2,1.60e-01,\n3,1.80e-01,\n")
    c = cp.parse_csv_text(text)
    assert c.names == ["CH1"]
    assert c.dt == pytest.approx(1e-7) and c.t0 == pytest.approx(-1e-5)
    assert np.allclose(c.ch(), [0.12, 0.14, 0.16, 0.18])


def test_rigol_style_two_channels():
    text = ("X,CH1,CH2,Start,Increment,\n"
            "Sequence,Volt,Volt,0.0,2.0e-07,\n"
            "0,1.0,-1.0,\n1,2.0,-2.0,\n2,3.0,-3.0,\n")
    c = cp.parse_csv_text(text)
    assert c.names == ["CH1", "CH2"] and np.allclose(c.ch("CH2"), [-1, -2, -3])


def test_siglent_style_csv():
    text = ("Record Length,Analog:4\n"
            "Sample Interval,1.000000E-07\n"
            "Vertical Units,V\n"
            "Horizontal Units,s\n"
            "Source,CH1\n"
            "Second,Value\n"
            "-2.000000E-07,0.004\n-1.000000E-07,0.006\n0.000000E+00,0.008\n1.000000E-07,0.010\n")
    c = cp.parse_csv_text(text)
    assert c.dt == pytest.approx(1e-7) and c.t0 == pytest.approx(-2e-7)
    assert c.names == ["CH1"] and np.allclose(c.ch(), [0.004, 0.006, 0.008, 0.010])


def test_headerless_numeric_csv():
    c = cp.parse_csv_text("0,1\n1e-6,2\n2e-6,3\n")
    assert c.dt == pytest.approx(1e-6) and c.names == ["CH1"]


def test_nonuniform_time_rejected():
    with pytest.raises(ValueError):
        cp.parse_csv_text("t,CH1\n0,1\n1e-6,2\n5e-6,3\n6e-6,4\n")


def test_empty_rejected():
    with pytest.raises(ValueError):
        cp.parse_csv_text("time,CH1\n")
