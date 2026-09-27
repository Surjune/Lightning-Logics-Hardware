"""Oscilloscope capture container and file I/O.

A :class:`Capture` is one acquisition: uniformly sampled channels sharing a time
base. Segmented ("sequence" / "record") acquisitions are lists of captures, each
carrying the trigger timestamp of its segment.

Supported files
---------------
* ``.npz``  — native format (lossless, keeps metadata and segments).
* ``.csv``  — generic ``time,CH1,CH2,...`` files, plus the CSV exports of common
  bench scopes: Rigol-style (``X,CH1,Start,Increment`` header with a sample-index
  column) and Siglent-style (key/value preamble such as ``Sample Interval`` followed
  by ``Second,Value`` data). Units in column headers are ignored.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Capture:
    """One acquisition. ``t0`` is the time of the first sample relative to the trigger."""

    dt: float
    channels: dict[str, np.ndarray]
    t0: float = 0.0
    meta: dict = field(default_factory=dict)
    timestamp: float | None = None  # trigger time of this segment (segmented captures)

    def __post_init__(self):
        if self.dt <= 0:
            raise ValueError("dt must be positive")
        if not self.channels:
            raise ValueError("capture needs at least one channel")
        lengths = {len(v) for v in self.channels.values()}
        if len(lengths) != 1:
            raise ValueError(f"channels have different lengths: {lengths}")
        self.channels = {k: np.asarray(v, dtype=float) for k, v in self.channels.items()}

    @property
    def fs(self) -> float:
        return 1.0 / self.dt

    @property
    def n(self) -> int:
        return len(next(iter(self.channels.values())))

    @property
    def duration(self) -> float:
        return self.n * self.dt

    @property
    def time(self) -> np.ndarray:
        return self.t0 + np.arange(self.n) * self.dt

    @property
    def names(self) -> list[str]:
        return list(self.channels)

    def ch(self, name: str | None = None) -> np.ndarray:
        """Channel data; defaults to the first channel."""
        if name is None:
            return next(iter(self.channels.values()))
        if name not in self.channels:
            raise KeyError(f"no channel {name!r}; have {self.names}")
        return self.channels[name]

    def slice_time(self, start: float, stop: float) -> "Capture":
        # half-open [start, stop); the epsilon absorbs float error in (t - t0) / dt
        i0 = max(0, int(np.ceil((start - self.t0) / self.dt - 1e-9)))
        i1 = min(self.n, int(np.ceil((stop - self.t0) / self.dt - 1e-9)))
        return Capture(self.dt, {k: v[i0:i1] for k, v in self.channels.items()},
                       self.t0 + i0 * self.dt, dict(self.meta), self.timestamp)


# ---- native format ----------------------------------------------------------------------
def save_npz(path: str | Path, captures: Capture | list[Capture]) -> None:
    """Save one capture or a list of segments (segments may differ in length)."""
    segs = captures if isinstance(captures, list) else [captures]
    if not segs:
        raise ValueError("nothing to save")
    names = segs[0].names
    arrays = {f"s{i}_{name}": s.ch(name) for i, s in enumerate(segs) for name in names}
    header = {
        "dt": segs[0].dt,
        "t0": [s.t0 for s in segs],
        "timestamps": [s.timestamp for s in segs],
        "meta": segs[0].meta,
        "names": names,
        "segmented": isinstance(captures, list),
    }
    np.savez_compressed(path, header=json.dumps(header, default=_json_default), **arrays)


def load_npz(path: str | Path) -> Capture | list[Capture]:
    with np.load(path, allow_pickle=False) as z:
        header = json.loads(str(z["header"]))
        segs = []
        for i, (t0, ts) in enumerate(zip(header["t0"], header["timestamps"])):
            chans = {name: z[f"s{i}_{name}"] for name in header["names"]}
            segs.append(Capture(header["dt"], chans, t0, dict(header["meta"]), ts))
    return segs if header["segmented"] else segs[0]


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not JSON serialisable: {type(o)}")


# ---- CSV ------------------------------------------------------------------------------------
def save_csv(path: str | Path, cap: Capture) -> None:
    """Generic CSV: ``time,<ch1>,<ch2>...`` with one header row."""
    t = cap.time
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time"] + cap.names)
        cols = [cap.ch(n) for n in cap.names]
        for i in range(cap.n):
            w.writerow([f"{t[i]:.9e}"] + [f"{c[i]:.6e}" for c in cols])


_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _is_num(s: str) -> bool:
    return bool(_NUM.match(s.strip()))


def _clean_name(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s).strip()
    return s or "CH"


def parse_csv_text(text: str) -> Capture:
    """Parse a scope CSV export (see module docstring for supported layouts)."""
    rows = [r for r in csv.reader(io.StringIO(text))]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    rows = [r[:-1] if r and r[-1] == "" else r for r in rows]  # trailing commas

    start = inc = None
    kv: dict[str, str] = {}
    names: list[str] | None = None
    data: list[list[float]] = []
    i = 0
    while i < len(rows):
        r = rows[i]
        numeric = len(r) >= 2 and _is_num(r[0]) and _is_num(r[1])
        if numeric:
            break
        low = [c.lower() for c in r]
        # Rigol: "X,CH1,Start,Increment" followed by "Sequence,Volt,<start>,<inc>"
        if low and low[0] == "x" and "start" in low and "increment" in low and i + 1 < len(rows):
            nxt = rows[i + 1]
            try:
                start = float(nxt[low.index("start")])
                inc = float(nxt[low.index("increment")])
            except (ValueError, IndexError):
                pass
            names = [_clean_name(c) for c in r[1:low.index("start")]]
            i += 2
            continue
        if len(r) >= 2 and _is_num(r[1]) and not _is_num(r[0]):
            kv[low[0]] = r[1]
        elif len(r) >= 2:
            names = [_clean_name(c) for c in r[1:]]
        i += 1

    for r in rows[i:]:
        vals = [c for c in r if c != ""]
        if len(vals) < 2 or not all(_is_num(c) for c in vals):
            continue
        data.append([float(c) for c in vals])
    if not data:
        raise ValueError("no numeric data rows found")
    width = min(len(d) for d in data)
    arr = np.array([d[:width] for d in data])
    first = arr[:, 0]

    if inc is None:
        for key in ("sample interval", "xincrement", "increment", "horizontal interval"):
            if key in kv:
                inc = float(kv[key])
                break
    is_index = np.allclose(first, np.round(first)) and np.all(np.diff(first) == 1)
    if inc is not None and is_index:
        t0 = (start if start is not None else 0.0) + first[0] * inc
        dt = inc
    else:
        d = np.diff(first)
        if len(d) == 0 or np.median(d) <= 0:
            raise ValueError("cannot determine the time base from the first column")
        dt = float(np.median(d))
        if np.max(np.abs(d - dt)) > 0.01 * dt:
            raise ValueError("time column is not uniformly sampled")
        t0 = float(first[0])

    nch = arr.shape[1] - 1
    if not names or len(names) < nch:
        names = [f"CH{k + 1}" for k in range(nch)]
    names = [n if n.lower() not in ("volt", "value", "v") else f"CH{k + 1}" for k, n in enumerate(names[:nch])]
    channels = {names[k]: arr[:, k + 1] for k in range(nch)}
    return Capture(dt, channels, t0, {"source": "csv", "csv_header": kv})


def load_csv(path: str | Path) -> Capture:
    return parse_csv_text(Path(path).read_text(errors="replace"))


def load(path: str | Path) -> Capture | list[Capture]:
    p = Path(path)
    if p.suffix.lower() == ".npz":
        return load_npz(p)
    if p.suffix.lower() in (".csv", ".txt"):
        return load_csv(p)
    raise ValueError(f"unsupported capture file: {p.suffix}")


def save(path: str | Path, cap: Capture | list[Capture]) -> None:
    p = Path(path)
    if p.suffix.lower() == ".npz":
        save_npz(p, cap)
    elif p.suffix.lower() == ".csv":
        if isinstance(cap, list):
            raise ValueError("segmented captures can only be saved as .npz")
        save_csv(p, cap)
    else:
        raise ValueError(f"unsupported capture file: {p.suffix}")
