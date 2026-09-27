"""Backends against simulated instruments that speak the documented formats."""

import struct

import numpy as np
import pytest

from sonarscope import capture as cp
from sonarscope import chain
from sonarscope import waveforms as wf
from sonarscope.backends import FileBench, InstrumentBench, NotSupported, ScopeDriver, SimulatedBench
from sonarscope.backends.ad3 import AnalogDiscovery
from sonarscope.backends.scpi import RigolDS1000Z, SiglentSDS, parse_ieee_block
from sonarscope.capture import Capture
from sonarscope.dut import ManualDut, NullDut, SerialDut
from sonarscope.plan import default_plan

PLAN = {t.name: t for t in default_plan()}


def block(payload: bytes) -> bytes:
    return b"#9" + f"{len(payload):09d}".encode() + payload + b"\n"


def test_parse_ieee_block():
    assert parse_ieee_block(b"junk#3005hello\n") == b"hello"
    with pytest.raises(ValueError):
        parse_ieee_block(b"#3010abc")


class FakeInst:
    def __init__(self):
        self.writes = []
        self.pending = b""

    def write(self, cmd):
        self.writes.append(cmd)
        self.on_write(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        return self.on_query(cmd)

    def read_raw(self):
        out, self.pending = self.pending, b""
        return out

    def close(self):
        pass


class FakeRigol(FakeInst):
    """RAW-mode BYTE transfers per the DS1000Z programming guide."""

    def __init__(self, volts, dt, t0):
        super().__init__()
        self.yinc, self.yorg, self.yref = 0.02, -10.0, 127.0
        self.codes = np.clip(np.round(volts / self.yinc + self.yorg + self.yref), 0, 255).astype(np.uint8)
        self.dt, self.t0 = dt, t0
        self.start, self.stop = 1, len(self.codes)

    def on_write(self, cmd):
        if cmd.startswith(":WAVeform:STARt"):
            self.start = int(cmd.split()[1])
        elif cmd.startswith(":WAVeform:STOP"):
            self.stop = int(cmd.split()[1])
        elif cmd == ":WAVeform:DATA?":
            n = self.stop - self.start + 1
            assert n <= 250_000, "RAW BYTE reads are limited to 250000 points"
            self.pending = block(self.codes[self.start - 1: self.stop].tobytes())

    def on_query(self, cmd):
        if cmd == ":WAVeform:PREamble?":
            return f"0,2,{len(self.codes)},1,{self.dt:e},{self.t0:e},0,{self.yinc:e},{self.yorg:e},{self.yref:e}\n"
        if cmd == ":TRIGger:STATus?":
            return "STOP\n"
        if cmd == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1054Z,FAKE,00.04\n"
        raise AssertionError(cmd)


def test_rigol_read_decodes_volts_time_and_chunks():
    n = 600_000
    t = np.arange(n) * 1e-7 - 1e-4
    v = 1.5 * np.sin(2 * np.pi * 250e3 * t)
    inst = FakeRigol(v, 1e-7, -1e-4)
    scope = RigolDS1000Z(inst=inst)
    cap = scope.read("CH1")
    assert cap.n == n and cap.dt == pytest.approx(1e-7) and cap.t0 == pytest.approx(-1e-4)
    assert np.max(np.abs(cap.ch() - v)) <= 0.011  # half a code step
    assert sum(1 for w in inst.writes if w == ":WAVeform:DATA?") == 3
    assert ":WAVeform:MODE RAW" in inst.writes and ":WAVeform:SOURce CHAN1" in inst.writes


def test_rigol_configure_and_single():
    inst = FakeRigol(np.zeros(10), 1e-7, 0)
    scope = RigolDS1000Z(inst=inst)
    from sonarscope.backends import ScopeSetup
    scope.configure(ScopeSetup(("CH1",), record_s=2.4e-3, pre_trigger_s=1e-4, trigger_level_v=0.2))
    assert ":TIMebase:MAIN:SCALe 0.0002" in inst.writes
    assert ":TRIGger:EDGe:SOURce CHAN1" in inst.writes
    assert scope.single(timeout_s=1)


class FakeSiglent(FakeInst):
    """WAVEDESC + signed data per the SDS series programming guide."""

    def __init__(self, volts, interval, *, adc_bit=12, probe=10.0, order=0, piece=100_000):
        super().__init__()
        self.vdiv_raw, self.voff_raw, self.cpd = 0.05, 0.01, 30.0 * (16 if adc_bit > 8 else 1)
        self.probe, self.adc_bit, self.order, self.interval = probe, adc_bit, order, interval
        vdiv, voff = self.vdiv_raw * probe, self.voff_raw * probe
        dtype = np.int16 if adc_bit > 8 else np.int8
        lim = np.iinfo(dtype)
        self.codes = np.clip(np.round((volts + voff) * self.cpd / vdiv), lim.min, lim.max).astype(dtype)
        self.piece, self.start, self.points = piece, 0, len(self.codes)
        self.tdiv_index, self.delay = 17, 3.0e-4   # 100 us/div

    def desc(self):
        d = bytearray(346)
        d[0:8] = b"WAVEDESC"
        struct.pack_into("<h", d, 32, 1 if self.adc_bit > 8 else 0)
        struct.pack_into("<h", d, 34, self.order)
        struct.pack_into("<i", d, 116, self.points)
        struct.pack_into("<f", d, 156, self.vdiv_raw)
        struct.pack_into("<f", d, 160, self.voff_raw)
        struct.pack_into("<f", d, 164, self.cpd)
        struct.pack_into("<h", d, 172, self.adc_bit)
        struct.pack_into("<f", d, 176, self.interval)
        struct.pack_into("<d", d, 180, self.delay)
        struct.pack_into("<h", d, 324, self.tdiv_index)
        struct.pack_into("<f", d, 328, self.probe)
        return bytes(d)

    def on_write(self, cmd):
        if cmd.startswith(":WAVeform:STARt"):
            self.start = int(cmd.split()[1])
        elif cmd == ":WAVeform:PREamble?":
            self.pending = block(self.desc())
        elif cmd == ":WAVeform:DATA?":
            chunk = self.codes[self.start: self.start + self.piece]
            if chunk.dtype == np.int16:
                chunk = chunk.astype(">i2" if self.order == 1 else "<i2")
            self.pending = block(chunk.tobytes())

    def on_query(self, cmd):
        if cmd == ":ACQuire:POINts?":
            return f"{self.points}\n"
        if cmd == ":WAVeform:MAXPoint?":
            return f"{self.piece}\n"
        if cmd == ":TRIGger:STATus?":
            return "Stop\n"
        raise AssertionError(cmd)


@pytest.mark.parametrize("adc_bit,order", [(12, 0), (12, 1), (8, 0)])
def test_siglent_read_decodes_wavedesc(adc_bit, order):
    n = 250_000
    t = np.arange(n) * 1e-7
    v = 2.0 * np.sin(2 * np.pi * 101.3e3 * t)
    inst = FakeSiglent(v, 1e-7, adc_bit=adc_bit, order=order)
    cap = SiglentSDS(inst=inst).read("CH1")
    step = inst.vdiv_raw * inst.probe / inst.cpd
    assert cap.n == n and cap.dt == pytest.approx(1e-7)
    assert np.max(np.abs(cap.ch() - v)) <= step * 0.51 + 1e-6
    assert cap.t0 == pytest.approx(3.0e-4 - 100e-6 * 10 / 2)
    assert sum(1 for w in inst.writes if w == ":WAVeform:DATA?") == 3
    assert (":WAVeform:WIDTh WORD" in inst.writes) == (adc_bit > 8)


class FakeDwf:
    """Minimal WaveForms SDK stand-in: returns a stored waveform once 'done'."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def __getattr__(self, name):
        def fn(*args):
            self.calls.append(name)
            if name == "FDwfDeviceOpen":
                args[1]._obj.value = 1
            elif name == "FDwfAnalogInStatus":
                args[2]._obj.value = 2
            elif name == "FDwfAnalogInStatusData":
                buf, n = args[2], args[3].value
                for i in range(n):
                    buf[i] = self.data[i]
            return 1
        return fn


def test_analog_discovery_flow():
    from sonarscope.backends import ScopeSetup
    data = np.linspace(-1, 1, 30000)
    dwf = FakeDwf(data)
    ad = AnalogDiscovery(dwf=dwf)
    ad.configure(ScopeSetup(("CH1",), record_s=3e-3, pre_trigger_s=1e-4, sample_rate=10e6))
    assert ad.single(1.0)
    cap = ad.read("CH1")
    assert cap.n == 30000 and np.allclose(cap.ch(), data)
    assert cap.t0 == pytest.approx(-1e-4)
    assert "FDwfAnalogInTriggerPositionSet" in dwf.calls


class SimScopeDriver(ScopeDriver):
    """A ScopeDriver whose 'hardware' is the reference model (exercises InstrumentBench)."""

    name = "sim-driver"

    def __init__(self):
        self.setup = None
        self.test = None

    def configure(self, setup):
        self.setup = setup

    def single(self, timeout_s=10.0):
        return True

    def read(self, channel):
        t = self.test
        if t.kind in ("floor", "idle"):
            cap = chain.idle_capture()
        elif t.kind == "transition":
            specs, _ = chain.transition_schedule(t.spec, t.new_spec, pri=t.pri, n_pings=t.n_pings,
                                                 t_change=t.t_change)
            cap = chain.simulate(specs, pri=t.pri, t0_channel=t.t_change)
            return Capture(cap.dt, {channel: cap.ch("T0" if channel == "CH2" else "CH1")}, cap.t0)
        elif t.kind == "pri":
            cap = chain.simulate([t.spec] * t.n_pings, pri=t.pri)
        else:
            cap = chain.simulate(t.spec)
        return Capture(cap.dt, {channel: cap.ch()}, cap.t0)


class RecordingDut(NullDut):
    def __init__(self, scope):
        self.scope, self.events = scope, []

    def prepare(self, test):
        self.scope.test = test
        self.events.append(("prepare", test.name))

    def trigger_change(self, test):
        self.events.append(("change", test.name))


def test_instrument_bench_flows():
    drv = SimScopeDriver()
    dut = RecordingDut(drv)
    bench = InstrumentBench(drv, dut)
    acq = bench.acquire(PLAN["lfm_hi"])
    assert acq.data.names == ["CH1"] and drv.setup.trigger_channel == "CH1"
    acq = bench.acquire(PLAN["transition"])
    assert set(acq.data.names) == {"CH1", "T0"} and drv.setup.trigger_channel == "CH2"
    assert ("change", "transition") in dut.events
    bench.acquire(PLAN["idle"])


def test_instrument_bench_skips_multiping_on_short_memory_driver():
    drv = SimScopeDriver()
    drv.name = "analog-discovery"
    with pytest.raises(NotSupported):
        InstrumentBench(drv, RecordingDut(drv)).acquire(PLAN["pri"])


class FakeLink:
    def __init__(self, replies=None):
        self.lines, self.replies = [], replies

    def write(self, b):
        self.lines.append(b.decode().strip())

    def readline(self):
        return b"OK\n" if self.replies is None else self.replies.pop(0)

    def close(self):
        pass


def test_serial_dut_protocol():
    link = FakeLink()
    dut = SerialDut(link=link)
    dut.prepare(PLAN["lfm_hi"])
    assert link.lines[0].startswith("CFG {") and link.lines[1].startswith("PRI ") and link.lines[2] == "RUN"
    dut.trigger_change(PLAN["transition"])
    assert link.lines[-1].startswith("NEXT {")
    dut.prepare(PLAN["idle"])
    assert link.lines[-1] == "IDLE"
    bad = SerialDut(link=FakeLink([b"ERR busy\n"]))
    with pytest.raises(Exception):
        bad.prepare(PLAN["idle"])


def test_manual_dut_prompts():
    seen = []
    dut = ManualDut(prompt=lambda s: seen.append(s), echo=lambda s: seen.append(s))
    dut.prepare(PLAN["lfm_hi"])
    assert any("Transmit" in s for s in seen)


def test_simulated_bench_and_file_replay(tmp_path):
    bench = SimulatedBench()
    for name in ("lfm_hi", "transition", "idle"):
        acq = bench.acquire(PLAN[name])
        data = acq.data
        if name == "transition":
            data[0].meta["t0"] = acq.context["t0"]
        cp.save(tmp_path / f"{name}.npz", data)
    fb = FileBench(tmp_path)
    assert isinstance(fb.acquire(PLAN["lfm_hi"]).data, Capture)
    tr = fb.acquire(PLAN["transition"])
    assert isinstance(tr.data, list) and tr.context["t0"] == pytest.approx(PLAN["transition"].t_change)
    with pytest.raises(NotSupported):
        fb.acquire(PLAN["barker13"])
    assert fb.full_scale_v is None or fb.full_scale_v > 0
    assert bench.full_scale_v == pytest.approx(5 * 3.3 / 255 * 127)
