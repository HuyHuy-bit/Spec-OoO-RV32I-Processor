"""Scoreboard: the l1_cache RTL against model/cache.py, through the g0 provider."""
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

from model.cache import Cache, CacheFatal, configured

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / p for p in ("rtl/generated/memory_protocol_pkg.sv", "rtl/memory/g0/dense_memory_1rw.sv",
                              "rtl/memory/dense_memory_1rw_array.sv", "rtl/memory/l1_cache.sv",
                              "verif/unit/l1_cache_tb.sv")]
OK, ACCESS_FAULT, PROTOCOL_ERROR = 0, 1, 2
MMIO = 0x4000_0000
# tags 0-5 over four sets: two ways overflow constantly, so victims and dirty writebacks are common.
LINES = [(tag * 16 + s) * 32 for tag in range(6) for s in (0, 3, 9, 15)]
FRESH = 0x0010_0000  # never touched by the random pool


def expect(ops, writable):
    """Replay ops on the model; return the expected records and memory traffic."""
    cache = configured("dcache" if writable else "icache")
    memory, records = cache.memory, []

    def line_read(addr):
        first = cache.load(addr, 4)
        if first.error:
            return ACCESS_FAULT, 0
        words = [first.data] + [cache.load(addr + 4 * n, 4).data for n in range(1, 8)]
        return OK, sum(w << 32 * n for n, w in enumerate(words))

    def line_write(addr, data, mask):
        # the first byte fills the line; a fill error stops there, like the single rtl access.
        for b in (b for b in range(32) if mask >> b & 1):
            if cache.store(addr + b, 1, data >> 8 * b & 0xff).error:
                return ACCESS_FAULT
        return OK

    bad = 0
    for op in ops:
        kind, args = op[0], op[1:]
        if kind == "i":
            memory.line(args[0] * 32)[:] = args[1].to_bytes(32, "little")
            continue
        if kind == "e":
            (memory.read_errors if args[0] == "r" else memory.write_errors).add(args[1] * 32)
            continue
        if kind == "x":
            bad = args[0]
            continue
        try:
            if kind == "l":
                status, line = line_read(args[0])
                rec = f"R {status} {line:064x} {0:08x}"
            elif not writable and kind in "suw":
                rec = f"R {PROTOCOL_ERROR} {0:064x} {0:08x}"
            elif kind == "s":
                rec = f"R {line_write(*args)} {0:064x} {0:08x}"
            elif kind == "u":
                rec = f"R {OK} {0:064x} {args[0] ^ 0x5a5a5a5a:08x}"
            elif kind == "w":
                rec = f"R {OK} {0:064x} {0:08x}"
            elif kind == "f":
                if writable:
                    cache.flush()
                else:
                    for ways in cache.sets:
                        for way in ways:
                            way.valid = False
                rec = "F"
            else:  # "z": reset drops every line, dirty or not; memory keeps what it has.
                cache = Cache(cache.g, memory, writable)
                rec = "Z"
        except CacheFatal:
            records.append("FATAL 1")
            break
        if bad:
            records.append("FATAL 2")
            break
        records.append(rec)
    else:
        records.append("DONE")
    traffic = [f"M fill {e[1]:08x}" if e[0] == "fill" else f"M wb {e[1]:08x} {int.from_bytes(e[2], 'little'):064x}"
               for e in memory.log]
    return records, traffic


def stim_text(ops):
    rows = []
    for kind, *args in ops:
        if kind == "s":
            rows.append(f"s {args[0]:x} {args[1]:064x} {args[2]:08x}")
        elif kind == "e":
            rows.append(f"e {args[0]} {args[1]:x}")
        elif kind == "x":
            rows.append(f"x {args[0]}")
        else:
            rows.append(" ".join([kind, *(f"{a:x}" for a in args)]))
    return "\n".join(rows) + "\n"


def random_ops(seed, count, writable):
    rng = random.Random(seed)
    # distinct nonzero lines, so a fill that drops or misplaces data cannot match the model.
    ops = [("i", line // 32, rng.getrandbits(256) | 1) for line in LINES]
    ops += [("e", "r", LINES[5] // 32), ("e", "r", LINES[17] // 32)]
    # the i$ rejects uncached requests itself, so "z" (an uncached request held in flight) is d$ only.
    weights = {"l": 45, "s": 40, "u": 5, "w": 3, "f": 2, "z": 1} if writable else \
              {"l": 80, "s": 5, "u": 3, "w": 2, "f": 10}
    for _ in range(count):
        kind = rng.choices(list(weights), list(weights.values()))[0]
        if kind == "l":
            ops.append(("l", rng.choice(LINES)))
        elif kind == "s":
            mask = rng.choice([0xffffffff, 0xf << 4 * rng.randrange(8), rng.getrandbits(32)]) or 1
            ops.append(("s", rng.choice(LINES), rng.getrandbits(256), mask))
        elif kind in "uw":
            addr = MMIO + 4 * rng.randrange(64)
            ops.append(("u", addr) if kind == "u" else ("w", addr, rng.getrandbits(32)))
        elif kind == "z":
            ops.append(("z", MMIO))
        else:
            ops.append(("f",))
    return ops


class L1CacheRtlTest(unittest.TestCase):
    builds: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for writable in (1, 0):
            obj = Path(cls.tmp.name) / f"obj{writable}"
            build = subprocess.run(["verilator", "--binary", "--timing", "--assert", "-Wno-fatal",
                                    "--build-jobs", "4", "--top-module", "l1_cache_tb",
                                    f"-GWRITABLE={writable}", "--Mdir", str(obj), *map(str, SOURCES)],
                                   capture_output=True, text=True)
            if build.returncode:
                raise AssertionError(build.stdout + build.stderr)
            cls.builds[writable] = obj / "Vl1_cache_tb"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_ops(self, ops, writable, seed=1):
        tmp = Path(self.tmp.name)
        (tmp / "stim").write_text(stim_text(ops))
        run = subprocess.run([self.builds[writable], f"+stim={tmp / 'stim'}", f"+out={tmp / 'out'}",
                              f"+seed={seed}"], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        lines = (tmp / "out").read_text().splitlines()
        self.assertEqual([l for l in lines if l.startswith("ERR")], [])
        records = [l for l in lines if not l.startswith("M ")]
        traffic = [l for l in lines if l.startswith("M ")]
        want_records, want_traffic = expect(ops, writable)
        for n, (got, want) in enumerate(zip(records, want_records)):
            self.assertEqual(got, want, f"record {n}")
        self.assertEqual(len(records), len(want_records))
        self.assertEqual(traffic, want_traffic)
        return records, traffic

    def test_dcache_random_against_model(self):
        for seed in (1, 42, 20261013):
            with self.subTest(seed=seed):
                records, traffic = self.run_ops(random_ops(seed, 2000, True), 1, seed)
                self.assertEqual(records[-1], "DONE")
                self.assertGreater(sum(t.startswith("M wb") for t in traffic), 100)
                self.assertGreater(sum(r.startswith(f"R {ACCESS_FAULT}") for r in records), 5)
                self.assertGreater(records.count("F"), 0)
                self.assertGreater(records.count("Z"), 0)

    def test_icache_random_against_model(self):
        records, traffic = self.run_ops(random_ops(7, 2000, False), 0, 7)
        self.assertEqual(records[-1], "DONE")
        self.assertFalse(any(t.startswith("M wb") for t in traffic))
        self.assertGreater(records.count("F"), 50)
        self.assertGreater(sum(r.startswith(f"R {PROTOCOL_ERROR}") for r in records), 50)

    def test_writeback_error_is_fatal_and_reason_sticks(self):
        a, b, c = (tag * 16 * 32 for tag in (1, 2, 3))  # three tags in set 0
        ops = [("e", "w", a // 32), ("s", a, 0xabc, 0xf), ("l", b), ("l", c)]
        records, _ = self.run_ops(ops, 1)
        # the bench sends an unsolicited response after fatal; reason 1 must survive it.
        self.assertEqual(records[-1], "FATAL 1")

    def test_protocol_violations_are_fatal_without_completing_a_handshake(self):
        cases = {
            "wrong id": [("l", LINES[0]), ("x", 1), ("l", FRESH)],
            "uncached data on a fill": [("l", LINES[0]), ("x", 2), ("l", FRESH)],
            "unsolicited during core response": [("x", 3), ("l", LINES[0])],
            "unsolicited during uncached write": [("x", 4), ("w", MMIO, 0x1234)],
        }
        for name, ops in cases.items():
            with self.subTest(name):
                records, _ = self.run_ops(ops, 1)
                self.assertEqual(records[-1], "FATAL 2")

    def test_reset_with_request_in_flight(self):
        ops = [("s", LINES[0], 0x55, 0x1), ("z", MMIO), ("l", LINES[0])]
        records, traffic = self.run_ops(ops, 1)
        # the dirty line died with reset, so memory still reads zero.
        self.assertEqual(records, [f"R 0 {0:064x} {0:08x}", "Z", f"R 0 {0:064x} {0:08x}", "DONE"])
        self.assertEqual(traffic, [f"M fill {LINES[0]:08x}"] * 2)


if __name__ == "__main__":
    unittest.main()
