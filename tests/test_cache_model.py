import copy
import json
from pathlib import Path
import random
import unittest

from model.cache import CONTRACT, Cache, CacheError, CacheFatal, Geometry, Memory, check_contract, configured

ROOT = Path(__file__).resolve().parents[1]
SMALL = Geometry(128, 2, 32)  # 2 sets: line bases 0x00, 0x40, 0x80 share set 0


def cache(geometry=SMALL, writable=True):
    return Cache(geometry, Memory(geometry.line_bytes), writable)


class CacheModelTest(unittest.TestCase):
    def test_contract_matches_config(self):
        self.assertEqual(json.loads((ROOT / "config/cache.json").read_text()), CONTRACT)
        dcache, icache = configured("dcache"), configured("icache")
        self.assertEqual((dcache.g.sets, dcache.writable), (16, True))
        self.assertEqual((icache.g, icache.writable), (dcache.g, False))
        with self.assertRaisesRegex(CacheError, "store_to_read_only"):
            icache.store(0, 4, 0)

    def test_unsupported_contract_fails(self):
        changes = [
            (lambda c: c["semantics"].update(write_policy="write_through"), "semantics.write_policy"),
            (lambda c: c["semantics"].update(replacement="random"), "semantics.replacement"),
            (lambda c: c["semantics"].pop("miss_order"), "semantics.miss_order"),
            (lambda c: c["semantics"].update(extra="x"), "semantics.extra"),
            (lambda c: c["caches"]["dcache"].update(ways=3), "geometry.ways"),
            (lambda c: c["caches"]["dcache"].update(size_bytes=512, ways=1), "dcache.geometry"),
            (lambda c: c["caches"]["icache"].update(size_bytes=512, ways=1), "icache.geometry"),
            (lambda c: c["caches"]["dcache"].update(size_bytes=4096), "dcache.geometry"),
            (lambda c: c["caches"]["icache"].update(writable="no"), "icache.writable"),
            (lambda c: c["caches"]["icache"].pop("writable"), "icache.writable"),
        ]
        for change, rule in changes:
            contract = copy.deepcopy(CONTRACT)
            change(contract)
            with self.subTest(rule=rule), self.assertRaisesRegex(CacheError, rule):
                check_contract(contract)

    def test_miss_then_hit_and_lru_victim(self):
        c = cache()
        c.memory.line(0x40)[0:4] = (0x11223344).to_bytes(4, "little")
        self.assertEqual((c.load(0x40, 4).hit, c.load(0x40, 4).data), (False, 0x11223344))
        self.assertTrue(c.load(0x42, 2).hit)
        c.load(0x00, 4)          # set 0 now holds 0x40 (LRU) and 0x00
        c.load(0x40, 1)          # 0x00 becomes LRU
        c.load(0x80, 4)          # evicts 0x00
        self.assertTrue(c.load(0x40, 4).hit)
        self.assertFalse(c.load(0x00, 4).hit)
        self.assertNotIn("writeback", c.stats)
        self.assertEqual(c.stats["eviction"], 2)

    def test_dirty_victim_written_back_before_fill(self):
        c = cache()
        self.assertFalse(c.store(0x04, 4, 0xdeadbeef).hit)   # write-allocate
        self.assertEqual(c.memory.load(0x04, 4), 0)
        c.load(0x40, 4)
        c.load(0x80, 4)                                     # evicts dirty 0x00
        self.assertEqual(c.memory.log[-2][:2], ("writeback", 0x00))
        self.assertEqual(c.memory.log[-1], ("fill", 0x80))
        self.assertEqual(c.memory.load(0x04, 4), 0xdeadbeef)
        self.assertEqual(c.load(0x04, 4).data, 0xdeadbeef)

    def test_fill_error_evicts_victim_and_installs_nothing(self):
        c = cache()
        c.store(0x00, 1, 0xaa)
        c.load(0x40, 4)
        c.memory.read_errors.add(0x80)
        self.assertEqual(c.load(0x80, 4).error, True)       # victim 0x00 already left
        self.assertEqual(c.memory.load(0x00, 1), 0xaa)
        self.assertEqual([(w.valid, w.tag) for w in c.sets[0] if w.valid], [(True, 1)])
        self.assertTrue(c.store(0x80, 4, 1).error)
        c.memory.read_errors.clear()
        self.assertEqual(c.load(0x80, 4).data, 0)           # failed store wrote nothing
        self.assertTrue(c.load(0x40, 4).hit)

    def test_writeback_error_is_fatal(self):
        c = cache()
        c.store(0x00, 4, 1)
        c.memory.write_errors.add(0x00)
        with self.assertRaises(CacheFatal):
            c.flush()
        self.assertEqual(c.stats["writeback"], 1)

    def test_illegal_accesses(self):
        c = cache()
        for addr, size in ((1, 2), (2, 4), (0, 3), (-4, 4), (0, 4.0), (0, True), (0.0, 4)):
            with self.subTest(addr=addr, size=size), self.assertRaisesRegex(CacheError, "access.alignment"):
                c.load(addr, size)
            with self.subTest(store=True, addr=addr, size=size), self.assertRaisesRegex(CacheError, "access.alignment"):
                c.store(addr, size, 0)
        self.assertEqual((c.memory.log, dict(c.stats)), ([], {}))   # rejected before any lookup
        with self.assertRaisesRegex(CacheError, "access.store_value"):
            c.store(0, 1, 0x100)
        with self.assertRaisesRegex(CacheError, "store_to_read_only"):
            cache(writable=False).store(0, 4, 0)
        with self.assertRaisesRegex(CacheError, "geometry.size_bytes"):
            Geometry(96, 2, 32)

    def test_random_against_flat_memory_and_lru_oracle(self):
        for geometry in (SMALL, Geometry(256, 1, 16), Geometry(512, 4, 32), configured("dcache").g):
            for seed in (1, 42, 20261004):
                with self.subTest(geometry=geometry, seed=seed):
                    self._random(geometry, random.Random(seed))

    def test_capacity_does_not_change_architectural_results(self):
        geometries = (configured("dcache").g, Geometry(4096, 2, 32), Geometry(512, 1, 32))
        rng = random.Random(20261013)
        trace = [(rng.random() < 0.4, rng.randrange(0, 16384, size), size, rng.getrandbits(8 * size))
                 for size in (rng.choice((1, 2, 4)) for _ in range(5000))]
        results = []
        for g in geometries:
            c = cache(g)
            loads = [c.store(a, n, v).data if st else c.load(a, n).data for st, a, n, v in trace]
            c.flush()
            results.append((loads, {a: c.memory.load(a, 1) for _, a, n, _ in trace for a in range(a, a + n)}))
        for g, result in zip(geometries[1:], results[1:]):
            with self.subTest(geometry=g):
                self.assertEqual(result, results[0])

    def _random(self, g, rng):
        c, flat = cache(g), {}
        recent = [[] for _ in range(g.sets)]  # per set, line numbers oldest first
        span = g.size_bytes * 3
        for _ in range(3000):
            size = rng.choice((1, 2, 4))
            addr = rng.randrange(0, span, size)
            line = addr // g.line_bytes
            hit = line in recent[line % g.sets]
            if hit:
                recent[line % g.sets].remove(line)
            recent[line % g.sets] = (recent[line % g.sets] + [line])[-g.ways:]
            if rng.random() < 0.4:
                value = rng.getrandbits(8 * size)
                result = c.store(addr, size, value)
                flat.update({addr + n: value >> 8 * n & 0xff for n in range(size)})
            else:
                result = c.load(addr, size)
                expect = sum(flat.get(addr + n, 0) << 8 * n for n in range(size))
                self.assertEqual(result.data, expect)
            self.assertEqual(result.hit, hit)
        dirty = sum(w.valid and w.dirty for ways in c.sets for w in ways)
        before = c.stats["writeback"]
        c.flush()
        self.assertEqual(c.stats["writeback"], before + dirty)
        self.assertEqual(c.stats["writeback"], sum(1 for e in c.memory.log if e[0] == "writeback"))
        for addr, byte in flat.items():
            self.assertEqual(c.memory.load(addr, 1), byte)
        fills = sum(1 for e in c.memory.log if e[0] == "fill")
        self.assertEqual(fills, c.stats["load_miss"] + c.stats["store_miss"])


if __name__ == "__main__":
    unittest.main()
