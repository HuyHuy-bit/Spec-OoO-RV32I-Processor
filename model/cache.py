"""Transaction-level reference model of a blocking write-back L1 cache.

No timing: each access completes before the next. Scoreboards compare data,
hit/miss, and the downstream line traffic recorded in Memory.log.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMANTICS = {
    "write_policy": "write_back",
    "write_miss": "allocate",
    "replacement": "lru",
    "victim_choice": "lowest_invalid_way_else_lru",
    "replacement_update": "every_hit_and_fill",
    "miss_order": "victim_writeback_before_fill",
    "fill_error": "victim_already_evicted_line_not_installed",
    "writeback_error": "fatal",
    "alignment": "natural_required",
    "unwritten_memory": "zero",
}


class CacheError(ValueError):
    pass


class CacheFatal(RuntimeError):
    pass


def check_contract(contract: dict) -> dict:
    """Reject unsupported cache semantics."""
    semantics = contract.get("semantics", {})
    for key in sorted(SEMANTICS.keys() | semantics.keys()):
        if semantics.get(key) != SEMANTICS.get(key):
            raise CacheError(f"contract.semantics.{key}: {semantics.get(key)!r} is not implemented")
    for name, spec in contract.get("caches", {}).items():
        Geometry(spec["size_bytes"], spec["ways"], spec["line_bytes"])
        if type(spec.get("writable")) is not bool:
            raise CacheError(f"contract.caches.{name}.writable: {spec.get('writable')!r}")
    return contract


@dataclass(frozen=True)
class Geometry:
    size_bytes: int
    ways: int
    line_bytes: int

    def __post_init__(self):
        for key in ("size_bytes", "ways", "line_bytes"):
            value = getattr(self, key)
            if type(value) is not int or value < 1 or value & (value - 1):
                raise CacheError(f"geometry.{key}: {value!r} must be a power of two")
        if self.line_bytes < 4 or self.size_bytes < self.ways * self.line_bytes:
            raise CacheError(f"geometry.shape: {self}")

    @property
    def sets(self) -> int:
        return self.size_bytes // (self.ways * self.line_bytes)

    def split(self, addr: int) -> tuple[int, int, int]:
        """Return (tag, set, offset)."""
        line = addr // self.line_bytes
        return line // self.sets, line % self.sets, addr % self.line_bytes


CONTRACT = check_contract(json.loads((ROOT / "config/cache.json").read_text()))


def configured(name: str) -> "Cache":
    """Build the named cache, with its permission, over an empty memory."""
    spec = CONTRACT["caches"][name]
    geometry = Geometry(spec["size_bytes"], spec["ways"], spec["line_bytes"])
    return Cache(geometry, Memory(geometry.line_bytes), spec["writable"])


class Memory:
    """Sparse line-granular backing memory; unwritten bytes read as zero."""

    def __init__(self, line_bytes: int):
        self.line_bytes = line_bytes
        self.lines: dict[int, bytearray] = {}
        self.read_errors: set[int] = set()
        self.write_errors: set[int] = set()
        self.log: list[tuple] = []

    def line(self, base: int) -> bytearray:
        return self.lines.setdefault(base, bytearray(self.line_bytes))

    def fill(self, base: int) -> bytes | None:
        self.log.append(("fill", base))
        return None if base in self.read_errors else bytes(self.line(base))

    def writeback(self, base: int, data: bytes) -> None:
        self.log.append(("writeback", base, bytes(data)))
        if base in self.write_errors:
            raise CacheFatal(f"writeback error at {base:#x}")
        self.line(base)[:] = data

    def load(self, addr: int, size: int) -> int:
        """Direct read of backing memory, bypassing any cache."""
        base = addr - addr % self.line_bytes
        return int.from_bytes(self.line(base)[addr - base:addr - base + size], "little")


@dataclass
class Way:
    tag: int = 0
    valid: bool = False
    dirty: bool = False
    data: bytearray | None = None


@dataclass(frozen=True)
class Result:
    hit: bool
    error: bool = False
    data: int | None = None


class Cache:
    def __init__(self, geometry: Geometry, memory: Memory, writable: bool = True):
        if memory.line_bytes != geometry.line_bytes:
            raise CacheError("memory.line_bytes differs from geometry")
        self.g, self.memory, self.writable = geometry, memory, writable
        self.sets = [[Way() for _ in range(geometry.ways)] for _ in range(geometry.sets)]
        # Per set, way indices from least to most recently used.
        self.lru = [list(range(geometry.ways)) for _ in range(geometry.sets)]
        self.stats: Counter = Counter()

    def _touch(self, index: int, way: int) -> None:
        self.lru[index].remove(way)
        self.lru[index].append(way)

    def _check(self, addr: int, size: int) -> None:
        if type(size) is not int or size not in (1, 2, 4) or type(addr) is not int or addr < 0 or addr % size:
            raise CacheError(f"access.alignment: addr={addr!r} size={size!r}")

    def _base(self, tag: int, index: int) -> int:
        return (tag * self.g.sets + index) * self.g.line_bytes

    def _lookup(self, addr: int, kind: str) -> tuple[Way | None, int, bool]:
        """Return the way holding addr (filling on a miss), its offset, and hit."""
        tag, index, offset = self.g.split(addr)
        ways = self.sets[index]
        for n, way in enumerate(ways):
            if way.valid and way.tag == tag:
                self._touch(index, n)
                self.stats[f"{kind}_hit"] += 1
                return way, offset, True
        self.stats[f"{kind}_miss"] += 1
        victim = next((n for n, w in enumerate(ways) if not w.valid), self.lru[index][0])
        way = ways[victim]
        if way.valid:
            self.stats["eviction"] += 1
            if way.dirty:
                self.stats["writeback"] += 1
                self.memory.writeback(self._base(way.tag, index), way.data)
            way.valid = way.dirty = False
        data = self.memory.fill(self._base(tag, index))
        if data is None:
            self.stats["fill_error"] += 1
            return None, offset, False
        way.tag, way.valid, way.data = tag, True, bytearray(data)
        self._touch(index, victim)
        return way, offset, False

    def load(self, addr: int, size: int) -> Result:
        self._check(addr, size)
        way, offset, hit = self._lookup(addr, "load")
        if way is None:
            return Result(hit, error=True)
        return Result(hit, data=int.from_bytes(way.data[offset:offset + size], "little"))

    def store(self, addr: int, size: int, value: int) -> Result:
        if not self.writable:
            raise CacheError("access.store_to_read_only_cache")
        self._check(addr, size)
        if type(value) is not int or not 0 <= value < 1 << 8 * size:
            raise CacheError(f"access.store_value: {value!r}")
        way, offset, hit = self._lookup(addr, "store")
        if way is None:
            return Result(hit, error=True)
        way.data[offset:offset + size] = value.to_bytes(size, "little")
        way.dirty = True
        return Result(hit)

    def flush(self) -> None:
        for index, ways in enumerate(self.sets):
            for way in ways:
                if way.valid and way.dirty:
                    self.stats["writeback"] += 1
                    self.memory.writeback(self._base(way.tag, index), way.data)
                    way.dirty = False
