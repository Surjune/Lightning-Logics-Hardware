"""Bench that replays saved captures: ``<directory>/<test name>.npz`` (or ``.csv``)."""

from __future__ import annotations

from pathlib import Path

from .. import capture as cp
from ..plan import TestCase
from .base import Acquisition, Bench, NotSupported


class FileBench(Bench):
    name = "files"

    def __init__(self, directory: str | Path, full_scale_v: float | None = None):
        self.dir = Path(directory)
        if not self.dir.is_dir():
            raise FileNotFoundError(self.dir)
        self.full_scale_v = full_scale_v

    def path_for(self, test: TestCase) -> Path | None:
        for ext in (".npz", ".csv"):
            p = self.dir / f"{test.name}{ext}"
            if p.exists():
                return p
        return None

    def acquire(self, test: TestCase) -> Acquisition:
        p = self.path_for(test)
        if p is None:
            raise NotSupported(f"no capture file for '{test.name}' in {self.dir}")
        data = cp.load(p)
        first = data[0] if isinstance(data, list) else data
        ctx = {}
        if "t0" in first.meta:
            ctx["t0"] = first.meta["t0"]
        if self.full_scale_v is None and "full_scale_v" in first.meta:
            self.full_scale_v = first.meta["full_scale_v"]
        return Acquisition(data, ctx)

    def describe(self) -> dict:
        return {"bench": self.name, "directory": str(self.dir)}
