"""Reference semantics of the target-neutral dense-memory wrapper (C0 oracle, not a provider).

Data is a tuple of lanes, one per write-mask bit; a lane of None is unknown.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "config/dense_memory.json").read_text())


class DenseMemoryError(ValueError):
    pass


@dataclass(frozen=True)
class Shape:
    depth: int
    width: int
    masked: bool = True

    @property
    def granule(self) -> int:
        return CONTRACT["byte_granularity"] if self.masked else self.width

    @property
    def lanes(self) -> int:
        return self.width // self.granule


def check_shape(shape: Shape) -> None:
    rules = CONTRACT["legal_shapes"]
    if not rules["depth_min"] <= shape.depth <= rules["depth_max"]:
        raise DenseMemoryError(f"shape.depth_range: {shape.depth}")
    if rules["depth_power_of_two"] and shape.depth & (shape.depth - 1):
        raise DenseMemoryError(f"shape.depth_power_of_two: {shape.depth}")
    if not rules["width_min"] <= shape.width <= rules["width_max"]:
        raise DenseMemoryError(f"shape.width_range: {shape.width}")
    if shape.masked and shape.width % rules["masked_width_multiple"]:
        raise DenseMemoryError(f"shape.masked_width_multiple: {shape.width}")


def lanes(shape: Shape, value: int | None) -> tuple:
    """Split a word into lanes; None makes every lane unknown."""
    mask = (1 << shape.granule) - 1
    return tuple(None if value is None else (value >> (shape.granule * n)) & mask
                 for n in range(shape.lanes))


def defined(data: tuple) -> bool:
    return None not in data


class DenseMemory:
    """One 1RW leaf. cycle() is one rising edge and returns the output visible after it."""

    def __init__(self, shape: Shape):
        if CONTRACT["port"] != "1rw" or CONTRACT["accesses_per_cycle"] != 1:
            raise DenseMemoryError("model implements only the single-port contract")
        check_shape(shape)
        self.shape = shape
        self.array = [(None,) * shape.lanes for _ in range(shape.depth)]
        self.reset()

    def reset(self) -> None:
        self.pipe = deque([None] * (CONTRACT["read_latency"] - 1))

    def cycle(self, en=False, we=None, addr=None, wdata=None, wmask=None) -> tuple:
        result = None
        if en:
            if we not in (False, True):
                raise DenseMemoryError("request.write_enable_unknown")
            if not isinstance(addr, int) or not 0 <= addr < self.shape.depth:
                raise DenseMemoryError(f"request.address_range: {addr}")
            if we:
                if not isinstance(wmask, int) or not 0 <= wmask < 1 << self.shape.lanes:
                    raise DenseMemoryError(f"request.mask_width: {wmask}")
                if isinstance(wdata, int) and not 0 <= wdata < 1 << self.shape.width:
                    raise DenseMemoryError("request.data_width")
                data = wdata if isinstance(wdata, tuple) else lanes(self.shape, wdata)
                if len(data) != self.shape.lanes:
                    raise DenseMemoryError("request.data_width")
                self.array[addr] = tuple(data[n] if wmask >> n & 1 else old
                                         for n, old in enumerate(self.array[addr]))
            else:
                result = self.array[addr]
        self.pipe.append(result)
        output = self.pipe.popleft()
        return output if output is not None else (None,) * self.shape.lanes
