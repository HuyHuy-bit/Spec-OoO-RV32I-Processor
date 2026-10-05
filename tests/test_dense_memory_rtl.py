"""Check the dense-memory RTL against the shared reference model."""
from pathlib import Path
import os
import random
import subprocess
import tempfile
import unittest

from model.dense_memory import DenseMemory, Shape, lanes

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "rtl/memory/dense_memory_1rw.sv", ROOT / "verif/unit/dense_memory_1rw_tb.sv"]
# Line-wide data slice, unmasked tag, deepest narrow leaf.
SHAPES = [Shape(16, 128), Shape(64, 21, masked=False), Shape(1024, 8)]
SEEDS, CYCLES = (1, 42, 20261005), 3000


def verilator(shape, out, *flags):
    params = [f"-GDEPTH={shape.depth}", f"-GWIDTH={shape.width}", f"-GMASKED={int(shape.masked)}"]
    return subprocess.run(["verilator", *flags, "--timing", "--assert", "--Wall", "--top-module",
                           "dense_memory_1rw_tb", *params, "--Mdir", str(out), *map(str, SOURCES)],
                          capture_output=True, text=True)


def stimulus(shape, seed):
    rng = random.Random(seed)
    hot = [rng.randrange(shape.depth) for _ in range(4)]
    for _ in range(CYCLES):
        en, we = rng.random() < 0.8, rng.random() < 0.5
        mask = rng.choice([0, (1 << shape.lanes) - 1, rng.getrandbits(shape.lanes)])
        addr = rng.choice(hot) if rng.random() < 0.7 else rng.randrange(shape.depth)
        yield en, we, addr, rng.getrandbits(shape.width), mask


class DenseMemoryRtlTest(unittest.TestCase):
    def test_four_state_masks(self):
        suite = os.environ.get("OSS_CAD_SUITE")
        tool_bin = Path(suite) / "bin" if suite else Path()
        for shape in (Shape(16, 64), Shape(16, 21, masked=False)):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                build = subprocess.run([str(tool_bin / "iverilog"), "-g2012", "-s", "dense_memory_1rw_tb",
                    f"-Pdense_memory_1rw_tb.WIDTH={shape.width}",
                    f"-Pdense_memory_1rw_tb.MASKED={int(shape.masked)}", "-o", str(tmp / "sim"),
                    *map(str, SOURCES)], capture_output=True, text=True, timeout=30)
                self.assertEqual(build.returncode, 0, build.stdout + build.stderr)

                def replay(rows):
                    (tmp / "stim").write_text("\n".join(rows) + "\n")
                    return subprocess.run([str(tool_bin / "vvp"), str(tmp / "sim"),
                        f"+stim={tmp / 'stim'}", f"+out={tmp / 'out'}"],
                        capture_output=True, text=True, timeout=10)

                full = (1 << shape.lanes) - 1
                initial = f"1 1 0 123456 {full:x}"
                masks = ["x", "z", "x1", "z1", "1x", "1z"] if shape.masked else ["x", "z"]
                for mask in masks:
                    with self.subTest(mask=mask):
                        run = replay([initial, f"1 1 0 abcdef {mask}", "1 0 0 0 0"])
                        self.assertNotEqual(run.returncode, 0, "unknown write mask was accepted")
                        self.assertIn("DENSE_MEMORY_ILLEGAL_MASK", run.stdout + run.stderr)

                for mask in ("x", "z"):
                    with self.subTest(ignored_mask=mask):
                        rows = [initial, f"0 1 0 abcdef {mask}", f"0 {mask} {mask} {mask} {mask}",
                                f"1 0 0 0 {mask}", "1 1 0 abcdef 0", f"1 0 0 0 {mask}",
                                f"1 1 0 x {full:x}", "1 0 0 0 0"]
                        run = replay(rows)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        outputs = (tmp / "out").read_text().lower().split()
                        self.assertEqual(len(outputs), len(rows))
                        for cycle in (3, 5):
                            self.assertEqual(int(outputs[cycle], 16), 0x123456)
                        self.assertEqual(set(outputs[-1]), {"x"})

    def test_rtl_matches_model(self):
        for shape in SHAPES:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                build = verilator(shape, tmp / "obj", "--binary", "--build-jobs", "2")
                self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                for seed in SEEDS:
                    steps = list(stimulus(shape, seed))
                    (tmp / "stim").write_text("".join(f"{e:x} {w:x} {a:x} {d:x} {m:x}\n" for e, w, a, d, m in steps))
                    run = subprocess.run([tmp / "obj/Vdense_memory_1rw_tb", f"+stim={tmp / 'stim'}",
                                          f"+out={tmp / 'out'}"], capture_output=True, text=True)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    outputs = (tmp / "out").read_text().split()
                    self.assertEqual(len(outputs), len(steps))
                    model, checked = DenseMemory(shape), 0
                    for cycle, ((en, we, addr, data, mask), out) in enumerate(zip(steps, outputs)):
                        want = model.cycle(en, we, addr, data, mask)
                        got = lanes(shape, int(out, 16))
                        for lane, value in enumerate(want):
                            if value is not None:
                                self.assertEqual(got[lane], value, f"seed {seed} cycle {cycle} lane {lane}")
                                checked += 1
                    self.assertGreater(checked, CYCLES // 4)

    def test_illegal_shape_fails_elaboration(self):
        for shape in (Shape(8, 32), Shape(48, 32), Shape(16, 136), Shape(16, 12)):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                lint = verilator(shape, tmp, "--lint-only")
                self.assertNotEqual(lint.returncode, 0)
                self.assertIn("illegal shape", lint.stdout + lint.stderr)


if __name__ == "__main__":
    unittest.main()
