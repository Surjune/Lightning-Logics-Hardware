"""Control of the transmitter (device under test) during a bench run.

``ManualDut`` prints what to set up and waits for the operator. ``SerialDut``
drives the firmware's test mode over a serial line with a small text protocol:

    -> CFG {"kind": "lfm", "f0": 400000.0, ...}   select the pulse (PulseSpec JSON)
    -> PRI 0.02                                   ping repetition interval, seconds
    -> RUN                                        start pinging
    -> IDLE                                       stop pinging, DAC at mid-scale
    -> NEXT {"kind": ...}                         request a change to the new pulse through the
                                                  normal adaptation path (back buffer + swap at the
                                                  next ping boundary) and raise the T0 marker GPIO
                                                  at the moment of the request
    <- OK | ERR <message>                         one reply line per command
"""

from __future__ import annotations

import json
import time

from .plan import TestCase


class DutError(RuntimeError):
    pass


class NullDut:
    """No control (simulation, file replay)."""

    def prepare(self, test: TestCase) -> None:
        pass

    def trigger_change(self, test: TestCase) -> None:
        pass

    def close(self) -> None:
        pass


class ManualDut:
    def __init__(self, prompt=input, echo=print):
        self.prompt, self.echo = prompt, echo

    def prepare(self, test: TestCase) -> None:
        self.echo(f"[{test.name}] {test.instruction()}")
        self.prompt("Press Enter when ready... ")

    def trigger_change(self, test: TestCase) -> None:
        self.echo(f"[{test.name}] Scope armed: press the preset button now.")

    def close(self) -> None:
        pass


class SerialDut:
    def __init__(self, port: str | None = None, baud: int = 115200, timeout: float = 2.0, link=None):
        if link is None:
            try:
                import serial  # pyserial
            except ImportError as e:  # pragma: no cover - depends on environment
                raise DutError("SerialDut needs pyserial: pip install 'sonarscope[serial]'") from e
            link = serial.Serial(port, baud, timeout=timeout)
        self.link = link

    def command(self, line: str) -> str:
        self.link.write((line + "\n").encode())
        reply = self.link.readline().decode(errors="replace").strip()
        if not reply.startswith("OK"):
            raise DutError(f"{line.split()[0]} failed: {reply or 'no reply'}")
        return reply

    def prepare(self, test: TestCase) -> None:
        if test.kind in ("floor", "idle"):
            self.command("IDLE")
            return
        self.command("CFG " + json.dumps(test.spec.to_dict()))
        self.command(f"PRI {test.pri:.6g}")
        self.command("RUN")
        time.sleep(max(0.05, 2 * test.pri))  # let a few pings go out before arming

    def trigger_change(self, test: TestCase) -> None:
        self.command("NEXT " + json.dumps(test.new_spec.to_dict()))

    def close(self) -> None:
        try:
            self.command("IDLE")
        except Exception:
            pass
        self.link.close()
