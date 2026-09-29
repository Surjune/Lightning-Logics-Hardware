"""Compare the firmware's adaptation decisions with the sonarscope reference model.

Sets a grid of environments on the board with `ENV`, reads the decision back from its
telemetry and compares it with sonarscope.acoustics.decide(): band, pulse length,
amplitude, window, modulation, ping interval and the physics figures.

    python firmware/tools/check_adaptation.py COM7

Needs pyserial and sonarscope. Exit status is non-zero on any mismatch.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time

import serial

from sonarscope import acoustics as ac


def command(link: serial.Serial, line: str, timeout: float = 5.0) -> str:
    link.write((line + "\n").encode())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = link.readline().decode(errors="replace").strip()
        if reply.startswith(("OK", "ERR")):
            return reply
    raise TimeoutError(f"no reply to {line.split()[0]}")


def latest_telemetry(link: serial.Serial, env: ac.Environment, timeout: float = 3.0) -> dict:
    """The newest telemetry frame whose environment matches `env`."""
    deadline, found = time.monotonic() + timeout, None
    want = (env.turbidity_ntu, env.range_m, env.temp_c, env.salinity_psu, env.depth_m)
    while time.monotonic() < deadline:
        line = link.readline().decode(errors="replace").strip()
        if not line.startswith("@"):
            continue
        t = json.loads(line[1:])
        e = t["env"]
        if "phys" in t and all(abs(a - b) < 0.006 for a, b in
                               zip((e["turbidity"], e["range"], e["temp"], e["salinity"], e["depth"]), want)):
            found = t
            if time.monotonic() > deadline - timeout + 0.4:  # let a few frames pass, keep the newest
                return found
    if found is None:
        raise TimeoutError("no telemetry for the requested environment")
    return found


def compare(t: dict, d: ac.Decision) -> list[str]:
    p, s, ph = t["pulse"], d.spec, t["phys"]
    checks = [
        ("kind", p["kind"] == s.kind),
        ("window", p["window"] == s.window),
        ("f0", abs(p["f0"] - s.f0) < 0.2), ("f1", abs(p["f1"] - s.f1) < 0.2),
        ("duration", abs(p["duration"] - s.duration) < 0.6e-6),
        ("amplitude", abs(p["amplitude"] - s.amplitude) < 1.5e-3),
        ("pri", abs(t["pri"] - d.pri_s) < 1e-6),
        ("fc", abs(ph["fc"] - d.fc_hz) < 1),
        ("c", abs(ph["c"] - d.sound_speed) < 0.01),
        ("alpha", abs(ph["alpha"] - d.alpha_db_km) < 0.01 + 1e-4 * d.alpha_db_km),
        ("tl", abs(ph["tl"] - d.tl_2way_db) < 0.01),
        ("demand", abs(ph["demand"] - d.demand) < 1e-3),
        ("limited", bool(ph["limited"]) == d.range_limited),
        ("energy_db", abs(ph["energy_db"] - d.energy_db) < 0.02),
    ]
    if s.kind == "barker13":
        checks.append(("chip", abs(p["chip"] - s.chip) < 1e-8))
    return [name for name, ok in checks if not ok]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    link = serial.Serial(args.port, args.baud, timeout=0.2)
    time.sleep(2.0)  # opening the port resets most ESP32 boards
    link.reset_input_buffer()
    command(link, "DEMO")
    command(link, "TELEM ON")

    waters = [(25.0, 35.0, 10.0), (5.0, 35.0, 200.0), (28.0, 0.0, 2.0)]
    cases = [(ac.Environment(ntu, rng, *w), mod, "auto")
             for w in waters for ntu in (0, 15, 40, 70, 100) for rng in (5, 12, 30, 60, 120, 200)
             for mod in ("auto",)]
    cases += [(ac.Environment(40, 80, 25, 35, 10), mod, win)
              for mod, win in itertools.product(ac.MODULATIONS, ("auto", "rect", "blackman"))]

    failures = 0
    for env, mod, win in cases:
        command(link, f"MOD {mod}")
        command(link, f"WIN {win}")
        command(link, f"ENV turbidity={env.turbidity_ntu} range={env.range_m} temp={env.temp_c} "
                      f"salinity={env.salinity_psu} depth={env.depth_m}")
        t = latest_telemetry(link, env)
        bad = compare(t, ac.decide(env, mod, win))
        if bad:
            failures += 1
            print(f"MISMATCH {env} mod={mod} win={win}: {', '.join(bad)}")
    command(link, "ENV AUTO")
    command(link, "MOD auto")
    command(link, "WIN auto")
    command(link, "TELEM OFF")
    link.close()
    print(f"{len(cases) - failures} of {len(cases)} environments match the reference model")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
