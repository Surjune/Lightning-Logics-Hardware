"""SCPI oscilloscope drivers (pyvisa).

Implemented from the vendors' programming guides:

* Rigol MSO/DS1000Z — ``:WAVeform:PREamble?`` returns ten comma-separated values
  ``format,type,points,count,xincrement,xorigin,xreference,yincrement,yorigin,yreference``;
  RAW-mode BYTE reads are limited to 250 000 points per ``:WAV:DATA?`` and require the
  scope to be stopped; volts = (code - yorigin - yreference) * yincrement.
* Siglent SDS (HD / X Plus family) — ``:WAVeform:PREamble?`` returns a binary WAVEDESC
  block: vdiv float @156, voffset float @160, code_per_div float @164, adc_bit short @172,
  interval float @176, delay double @180, timebase enum short @324, probe float @328;
  data are signed bytes (adc_bit <= 8) or 16-bit words; volts = code / code_per_div * vdiv
  - voffset (vdiv, voffset scaled by the probe factor); time = delay - tdiv * 10 / 2 +
  index * interval.

Both drivers were checked against simulated instruments that emit these documented
formats (see tests). Validate on the real scope during the first bench session:
``sonarscope capture`` of a known function-generator sine, then compare amplitude,
frequency and trigger position.
"""

from __future__ import annotations

import struct
import time

import numpy as np

from ..capture import Capture
from .base import ScopeDriver, ScopeSetup


def parse_ieee_block(raw: bytes) -> bytes:
    """Strip an IEEE 488.2 definite-length block header ``#<n><len>``."""
    i = raw.find(b"#")
    if i < 0:
        raise ValueError("no block header")
    n = int(raw[i + 1: i + 2])
    length = int(raw[i + 2: i + 2 + n])
    start = i + 2 + n
    body = raw[start: start + length]
    if len(body) != length:
        raise ValueError(f"short block: expected {length} bytes, got {len(body)}")
    return body


def open_resource(resource: str, timeout_ms: int = 10000):
    try:
        import pyvisa
    except ImportError as e:  # pragma: no cover - depends on environment
        raise RuntimeError("SCPI backends need pyvisa: pip install 'sonarscope[visa]'") from e
    rm = pyvisa.ResourceManager("@py") if resource.startswith("TCPIP") else pyvisa.ResourceManager()
    inst = rm.open_resource(resource)
    inst.timeout = timeout_ms
    inst.chunk_size = 20 * 1024 * 1024
    return inst


class ScpiScope(ScopeDriver):
    divisions = 10
    channel_names: dict[str, str] = {}

    def __init__(self, resource=None, inst=None, timeout_ms: int = 10000):
        self.inst = inst if inst is not None else open_resource(resource, timeout_ms)
        self.setup: ScopeSetup | None = None

    def w(self, cmd: str) -> None:
        self.inst.write(cmd)

    def q(self, cmd: str) -> str:
        return str(self.inst.query(cmd)).strip()

    def raw(self, cmd: str) -> bytes:
        self.inst.write(cmd)
        return self.inst.read_raw()

    def src(self, channel: str) -> str:
        return self.channel_names.get(channel, channel)

    def identify(self) -> str:
        try:
            return self.q("*IDN?")
        except Exception:  # pragma: no cover
            return self.name

    def close(self) -> None:
        try:
            self.inst.close()
        except Exception:  # pragma: no cover
            pass


class RigolDS1000Z(ScpiScope):
    name = "rigol-ds1000z"
    divisions = 12
    max_points = 250_000
    channel_names = {"CH1": "CHAN1", "CH2": "CHAN2", "CH3": "CHAN3", "CH4": "CHAN4", "T0": "CHAN2"}

    def configure(self, setup: ScopeSetup) -> None:
        self.setup = setup
        self.w(":STOP")
        for ch in setup.channels:
            self.w(f":{self.src(ch).replace('CHAN', 'CHANnel')}:DISPlay ON")
            if ch in setup.vdiv:
                self.w(f":{self.src(ch).replace('CHAN', 'CHANnel')}:SCALe {setup.vdiv[ch]:.4g}")
        scale = setup.record_s / self.divisions
        self.w(f":TIMebase:MAIN:SCALe {scale:.4g}")
        # screen centre relative to the trigger: put the trigger pre_trigger_s from the left edge
        self.w(f":TIMebase:MAIN:OFFSet {setup.record_s / 2 - setup.pre_trigger_s:.6g}")
        self.w(":TRIGger:MODE EDGE")
        self.w(f":TRIGger:EDGe:SOURce {self.src(setup.trigger_channel)}")
        self.w(f":TRIGger:EDGe:SLOPe {'POSitive' if setup.trigger_slope == 'rise' else 'NEGative'}")
        self.w(f":TRIGger:EDGe:LEVel {setup.trigger_level_v:.4g}")

    def single(self, timeout_s: float = 10.0) -> bool:
        self.w(":SINGle")
        t_end = time.time() + timeout_s
        time.sleep(0.05)
        while time.time() < t_end:
            if self.q(":TRIGger:STATus?").upper().startswith("STOP"):
                return True
            time.sleep(0.05)
        self.w(":STOP")
        return False

    def force(self, timeout_s: float = 5.0) -> bool:
        self.w(":TRIGger:SWEep SINGle")
        self.w(":SINGle")
        time.sleep(0.1)
        self.w(":TFORce")
        return self._wait_stop(timeout_s)

    def _wait_stop(self, timeout_s: float) -> bool:
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            if self.q(":TRIGger:STATus?").upper().startswith("STOP"):
                return True
            time.sleep(0.05)
        return False

    def read(self, channel: str) -> Capture:
        self.w(f":WAVeform:SOURce {self.src(channel)}")
        self.w(":WAVeform:MODE RAW")
        self.w(":WAVeform:FORMat BYTE")
        pre = [float(v) for v in self.q(":WAVeform:PREamble?").split(",")]
        points = int(pre[2])
        xinc, xorg, xref, yinc, yorg, yref = pre[4], pre[5], pre[6], pre[7], pre[8], pre[9]
        chunks = []
        start = 1
        while start <= points:
            stop = min(points, start + self.max_points - 1)
            self.w(f":WAVeform:STARt {start}")
            self.w(f":WAVeform:STOP {stop}")
            chunks.append(parse_ieee_block(self.raw(":WAVeform:DATA?")))
            start = stop + 1
        codes = np.frombuffer(b"".join(chunks), dtype=np.uint8).astype(float)
        volts = (codes - yorg - yref) * yinc
        t0 = xorg - xref * xinc
        return Capture(xinc, {channel: volts}, t0, {"scope": self.name, "preamble": pre})


class SiglentSDS(ScpiScope):
    name = "siglent-sds"
    divisions = 10
    channel_names = {"CH1": "C1", "CH2": "C2", "CH3": "C3", "CH4": "C4", "T0": "C2"}
    TDIV_ENUM = [200e-12, 500e-12, 1e-9, 2e-9, 5e-9, 10e-9, 20e-9, 50e-9, 100e-9, 200e-9, 500e-9,
                 1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6, 200e-6, 500e-6,
                 1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3,
                 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]

    def configure(self, setup: ScopeSetup) -> None:
        self.setup = setup
        for ch in setup.channels:
            n = self.src(ch)[1:]
            self.w(f":CHANnel{n}:SWITch ON")
            if ch in setup.vdiv:
                self.w(f":CHANnel{n}:SCALe {setup.vdiv[ch]:.4E}")
        self.w(f":TIMebase:SCALe {setup.record_s / self.divisions:.4E}")
        self.w(f":TIMebase:DELay {setup.record_s / 2 - setup.pre_trigger_s:.4E}")
        self.w(":TRIGger:TYPE EDGE")
        self.w(f":TRIGger:EDGE:SOURce {self.src(setup.trigger_channel)}")
        self.w(f":TRIGger:EDGE:SLOPe {'RISing' if setup.trigger_slope == 'rise' else 'FALLing'}")
        self.w(f":TRIGger:EDGE:LEVel {setup.trigger_level_v:.4E}")

    def single(self, timeout_s: float = 10.0) -> bool:
        self.w(":TRIGger:MODE SINGle")
        t_end = time.time() + timeout_s
        time.sleep(0.05)
        while time.time() < t_end:
            if self.q(":TRIGger:STATus?").lower().startswith("stop"):
                return True
            time.sleep(0.05)
        self.w(":TRIGger:STOP")
        return False

    def force(self, timeout_s: float = 5.0) -> bool:
        self.w(":TRIGger:MODE FTRIG")
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            if self.q(":TRIGger:STATus?").lower().startswith("stop"):
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def parse_wavedesc(desc: bytes) -> dict:
        def f32(o):
            return struct.unpack_from("<f", desc, o)[0]
        return {
            "comm_type": struct.unpack_from("<h", desc, 32)[0],
            "comm_order": struct.unpack_from("<h", desc, 34)[0],
            "points": struct.unpack_from("<i", desc, 116)[0],
            "vdiv": f32(156), "voffset": f32(160), "code_per_div": f32(164),
            "adc_bit": struct.unpack_from("<h", desc, 172)[0],
            "interval": f32(176), "delay": struct.unpack_from("<d", desc, 180)[0],
            "tdiv_index": struct.unpack_from("<h", desc, 324)[0],
            "probe": f32(328),
        }

    def read(self, channel: str) -> Capture:
        self.w(":WAVeform:STARt 0")
        self.w(f":WAVeform:SOURce {self.src(channel)}")
        d = self.parse_wavedesc(parse_ieee_block(self.raw(":WAVeform:PREamble?")))
        points = int(float(self.q(":ACQuire:POINts?")))
        piece = int(float(self.q(":WAVeform:MAXPoint?")))
        word = d["adc_bit"] > 8
        self.w(f":WAVeform:WIDTh {'WORD' if word else 'BYTE'}")
        if points > piece:
            self.w(f":WAVeform:POINt {piece}")
        data = b""
        for start in range(0, points, piece):
            self.w(f":WAVeform:STARt {start}")
            data += parse_ieee_block(self.raw(":WAVeform:DATA?"))
        if word:
            codes = np.frombuffer(data, dtype=">i2" if d["comm_order"] == 1 else "<i2").astype(float)
        else:
            codes = np.frombuffer(data, dtype=np.int8).astype(float)
        probe = d["probe"] or 1.0
        vdiv, voff = d["vdiv"] * probe, d["voffset"] * probe
        volts = codes / d["code_per_div"] * vdiv - voff
        tdiv = self.TDIV_ENUM[d["tdiv_index"]] if 0 <= d["tdiv_index"] < len(self.TDIV_ENUM) else 0.0
        t0 = d["delay"] - tdiv * self.divisions / 2
        return Capture(d["interval"], {channel: volts}, t0, {"scope": self.name, "wavedesc": d})


DRIVERS = {"rigol": RigolDS1000Z, "siglent": SiglentSDS}
