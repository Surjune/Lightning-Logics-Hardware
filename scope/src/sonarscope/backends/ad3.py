"""Digilent Analog Discovery (2/3) driver through the WaveForms SDK (``dwf`` library).

Uses the documented SDK calls: FDwfDeviceOpen, FDwfAnalogInFrequencySet,
FDwfAnalogInBufferSizeSet, FDwfAnalogInChannelEnableSet / RangeSet,
FDwfAnalogInTrigger{Source,Type,Channel,Level,Condition,Position}Set,
FDwfAnalogInConfigure, FDwfAnalogInStatus and FDwfAnalogInStatusData.

Single-buffer acquisitions only (the default buffer holds a single pulse at 10 MSa/s).
Multi-ping PRI and transition tests need a deep-memory oscilloscope and are skipped.
The trigger position set here is the time of the buffer centre relative to the trigger.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import byref, c_double, c_int, c_ubyte

import numpy as np

from ..capture import Capture
from .base import ScopeDriver, ScopeSetup

TRIGSRC_DETECTOR_ANALOG_IN = c_ubyte(2)
TRIGTYPE_EDGE = c_int(0)
SLOPE_RISE, SLOPE_FALL = c_int(0), c_int(1)
STATE_DONE = 2


def load_dwf():
    if sys.platform.startswith("win"):
        return ctypes.cdll.dwf
    if sys.platform == "darwin":
        return ctypes.cdll.LoadLibrary("/Library/Frameworks/dwf.framework/dwf")
    return ctypes.cdll.LoadLibrary("libdwf.so")


class AnalogDiscovery(ScopeDriver):
    name = "analog-discovery"
    channel_index = {"CH1": 0, "CH2": 1, "T0": 1}

    def __init__(self, dwf=None, device_index: int = -1):
        self.dwf = dwf if dwf is not None else load_dwf()
        self.h = c_int()
        self.dwf.FDwfDeviceOpen(c_int(device_index), byref(self.h))
        if self.h.value == 0:
            raise RuntimeError("no Analog Discovery found (is WaveForms installed and the device free?)")
        self.setup: ScopeSetup | None = None
        self.n = 0
        self.fs = 0.0

    def configure(self, setup: ScopeSetup) -> None:
        self.setup = setup
        self.fs = setup.sample_rate
        self.n = int(round(setup.record_s * self.fs))
        d, h = self.dwf, self.h
        d.FDwfAnalogInFrequencySet(h, c_double(self.fs))
        d.FDwfAnalogInBufferSizeSet(h, c_int(self.n))
        for ch in setup.channels:
            idx = c_int(self.channel_index[ch])
            d.FDwfAnalogInChannelEnableSet(h, idx, c_int(1))
            rng = 8 * setup.vdiv[ch] if ch in setup.vdiv else 10.0
            d.FDwfAnalogInChannelRangeSet(h, idx, c_double(rng))
        d.FDwfAnalogInTriggerSourceSet(h, TRIGSRC_DETECTOR_ANALOG_IN)
        d.FDwfAnalogInTriggerTypeSet(h, TRIGTYPE_EDGE)
        d.FDwfAnalogInTriggerChannelSet(h, c_int(self.channel_index[setup.trigger_channel]))
        d.FDwfAnalogInTriggerLevelSet(h, c_double(setup.trigger_level_v))
        d.FDwfAnalogInTriggerConditionSet(h, SLOPE_RISE if setup.trigger_slope == "rise" else SLOPE_FALL)
        d.FDwfAnalogInTriggerPositionSet(h, c_double(setup.record_s / 2 - setup.pre_trigger_s))

    def single(self, timeout_s: float = 10.0) -> bool:
        d, h = self.dwf, self.h
        d.FDwfAnalogInConfigure(h, c_int(1), c_int(1))
        sts = c_ubyte()
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            d.FDwfAnalogInStatus(h, c_int(1), byref(sts))
            if sts.value == STATE_DONE:
                return True
            time.sleep(0.01)
        return False

    def force(self, timeout_s: float = 5.0) -> bool:
        self.dwf.FDwfAnalogInTriggerSourceSet(self.h, c_ubyte(0))  # trigsrcNone: free-running
        ok = self.single(timeout_s)
        self.dwf.FDwfAnalogInTriggerSourceSet(self.h, TRIGSRC_DETECTOR_ANALOG_IN)
        return ok

    def read(self, channel: str) -> Capture:
        buf = (c_double * self.n)()
        self.dwf.FDwfAnalogInStatusData(self.h, c_int(self.channel_index[channel]), buf, c_int(self.n))
        v = np.frombuffer(buf, dtype=float).copy()
        t0 = (self.setup.record_s / 2 - self.setup.pre_trigger_s) - self.n / (2 * self.fs)
        return Capture(1 / self.fs, {channel: v}, t0, {"scope": self.name})

    def close(self) -> None:
        self.dwf.FDwfDeviceCloseAll()
