"""Compare the firmware's DAC codes with the sonarscope reference model.

Loads every sonarscope preset into the transmitter over the serial test-mode link
(CFG), reads back the codes it will stream (DUMP) and compares them sample by sample
with sonarscope.waveforms.dac_codes(). Also reports the on-target synthesis time.

    python firmware/tools/check_codes.py COM7          # Windows
    python firmware/tools/check_codes.py /dev/ttyUSB0  # Linux

Needs pyserial and sonarscope (pip install -e "scope[serial]").
Exit status is non-zero if any sample differs by more than one DAC code.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import serial

from sonarscope import waveforms as wf


def command(link: serial.Serial, line: str, timeout: float = 10.0) -> str:
    """Send one command and return its OK/ERR reply, skipping '#' log lines."""
    link.write((line + "\n").encode())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = link.readline().decode(errors="replace").strip()
        if reply.startswith(("OK", "ERR")):
            return reply
    raise TimeoutError(f"no reply to {line.split()[0]}")


def status(link: serial.Serial) -> dict:
    fields = command(link, "STATUS").split()[1:]
    return dict(f.split("=", 1) for f in fields)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    link = serial.Serial(args.port, args.baud, timeout=1.0)
    time.sleep(2.0)  # opening the port resets most ESP32 boards; wait for the boot log
    link.reset_input_buffer()

    worst = 0
    print(f"{'preset':<14} {'samples':>7} {'max |diff|':>10} {'differing':>9} {'synth ms':>9}")
    for name in wf.PRESETS:
        spec = wf.preset(name)
        reply = command(link, "CFG " + json.dumps(spec.to_dict()))
        if not reply.startswith("OK"):
            print(f"{name:<14} {reply}")
            worst = max(worst, 255)
            continue
        synth_ms = int(status(link)["synth_us"]) / 1000
        _, n, hexcodes = command(link, "DUMP", timeout=30.0).split(" ", 2)
        got = np.frombuffer(bytes.fromhex(hexcodes), dtype=np.uint8).astype(int)
        want = wf.dac_codes(spec)
        if len(got) != len(want):
            print(f"{name:<14} length {len(got)} != reference {len(want)}")
            worst = max(worst, 255)
            continue
        diff = np.abs(got - want)
        worst = max(worst, int(diff.max()))
        print(f"{name:<14} {int(n):>7} {int(diff.max()):>10} {int((diff > 0).sum()):>9} {synth_ms:>9.2f}")

    st = status(link)
    print(f"\nDMA: {st['dma_samples']} samples per buffer, {st['underruns']} underruns, {st['pings']} pings")
    command(link, "DEMO")
    link.close()
    print("PASS: firmware matches the reference model" if worst <= 1 else "FAIL: codes differ from the reference")
    return 0 if worst <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
