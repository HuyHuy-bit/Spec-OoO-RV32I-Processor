import copy
import json
from pathlib import Path
import unittest

from model.dense_memory import CONTRACT, DenseMemory, DenseMemoryError, Shape, check_contract, defined, lanes

ROOT = Path(__file__).resolve().parents[1]
WORD = Shape(256, 32)
U = (None,) * 4


def word(data):
    return None if not defined(data) else sum(v << (8 * n) for n, v in enumerate(data))


class DenseMemoryTest(unittest.TestCase):
    def test_contract_covers_every_architecture_item(self):
        contract = json.loads((ROOT / "config/dense_memory.json").read_text())
        self.assertEqual(contract, CONTRACT)
        self.assertEqual((contract["port"], contract["read_latency"], contract["accesses_per_cycle"]), ("1rw", 1, 1))
        for item in ("legal_shapes", "throughput", "output_hold", "polarity", "byte_granularity", "collision",
                     "reset", "unknown_data", "enable_low", "write_mask"):
            self.assertIn(item, contract)

    def test_contradictory_or_unsupported_contract_fails(self):
        changes = [
            (lambda c: c["semantics"].update(reset_payload="cleared"), "semantics.reset_payload"),
            (lambda c: c["semantics"].update(output_hold="until_next_read"), "semantics.output_hold"),
            (lambda c: c["semantics"].pop("collision"), "semantics.collision"),
            (lambda c: c["semantics"].update(read_during_write="old_data"), "semantics.read_during_write"),
            (lambda c: c.pop("semantics"), "semantics"),
            (lambda c: c.update(port="2rw"), "contract.port"),
            (lambda c: c.update(read_latency=0), "contract.read_latency"),
        ]
        for change, rule in changes:
            contract = copy.deepcopy(CONTRACT)
            change(contract)
            with self.subTest(rule=rule), self.assertRaisesRegex(DenseMemoryError, rule):
                check_contract(contract)

    def test_read_latency_and_output_validity(self):
        # Each call is one edge; its result is the output in the following cycle.
        m = DenseMemory(WORD)
        self.assertEqual(m.cycle(True, True, 3, 0xdeadbeef, 0xf), U)
        self.assertEqual(word(m.cycle(True, False, 3)), 0xdeadbeef)
        self.assertEqual(m.cycle(), U)
        self.assertEqual(word(m.cycle(True, False, 3)), 0xdeadbeef)
        self.assertEqual(m.cycle(True, True, 4, 1, 0xf), U)

    def test_partial_mask_and_zero_mask_write(self):
        m = DenseMemory(WORD)
        m.cycle(True, True, 7, 0x11223344, 0xf)
        m.cycle(True, True, 7, 0xaabbccdd, 0b0101)
        m.cycle(True, True, 7, 0x55555555, 0)
        self.assertEqual(word(m.cycle(True, False, 7)), 0x11bb33dd)

    def test_unwritten_partial_and_unknown_data_stay_unknown(self):
        m = DenseMemory(WORD)
        self.assertEqual(m.cycle(True, False, 9), U)
        m.cycle(True, True, 9, 0xab, 0b0001)
        m.cycle(True, True, 10, None, 0b0010)
        self.assertEqual(m.cycle(True, False, 9), (0xab, None, None, None))
        self.assertEqual(m.cycle(True, False, 10), U)

    def test_reset_keeps_payload_and_output_is_unknown_after_it(self):
        m = DenseMemory(WORD)
        m.cycle(True, True, 1, 0x12345678, 0xf)
        m.cycle(True, False, 1)
        m.reset()
        self.assertEqual(m.cycle(), U)
        self.assertEqual(word(m.cycle(True, False, 1)), 0x12345678)

    def test_enable_low_ignores_unknown_inputs(self):
        m = DenseMemory(WORD)
        m.cycle(True, True, 2, 7, 0xf)
        self.assertEqual(m.cycle(False, None, None, None, None), U)
        self.assertEqual(m.cycle(False, True, 2, 9, 0xf), U)
        self.assertEqual(word(m.cycle(True, False, 2)), 7)

    def test_unmasked_tag_shape_has_one_lane(self):
        tag = Shape(64, 21, masked=False)
        m = DenseMemory(tag)
        m.cycle(True, True, 5, 0x1abcd, 1)
        self.assertEqual(m.cycle(True, False, 5), (0x1abcd,))
        self.assertEqual(lanes(tag, 0x1abcd), (0x1abcd,))

    def test_rejects_illegal_shapes_and_requests(self):
        for shape, rule in ((Shape(8, 32), "depth_range"), (Shape(96, 32), "depth_power_of_two"),
                            (Shape(256, 256), "width_range"), (Shape(256, 21), "masked_width_multiple")):
            with self.assertRaisesRegex(DenseMemoryError, rule): DenseMemory(shape)
        m = DenseMemory(WORD)
        for request, rule in (((True, None, 0), "write_enable_unknown"), ((True, False, 256), "address_range"),
                              ((True, False, None), "address_range"), ((True, True, 0, 1, 0x10), "mask_width"),
                              ((True, True, 0, 1 << 32, 0xf), "data_width"), ((True, True, 0, (1, 2), 0xf), "data_width")):
            with self.assertRaisesRegex(DenseMemoryError, rule): m.cycle(*request)

    def test_rejects_out_of_width_lanes_without_changing_contents(self):
        tag = Shape(64, 21, masked=False)
        cases = [(Shape(16, 8), 0x5a, [(256,), (-1,), ("not data",), (True,), (1.0,)]),
                 (WORD, 0x11223344, [(1, 2, 3, 256), (None, None, None, -1), "0x1", 1.5]),
                 (tag, 0x1abcd, [(1 << 21,), (-1,), ("x",)])]
        for shape, value, bad in cases:
            m = DenseMemory(shape)
            m.cycle(True, True, 0, value, (1 << shape.lanes) - 1)
            for data in bad:
                with self.subTest(shape=shape, data=data), self.assertRaisesRegex(DenseMemoryError, "data_width"):
                    m.cycle(True, True, 0, data, (1 << shape.lanes) - 1)
            self.assertEqual(m.cycle(True, False, 0), lanes(shape, value))
        m = DenseMemory(Shape(16, 8))
        m.cycle(True, True, 1, (None,), 1)
        self.assertEqual(m.cycle(True, False, 1), (None,))


if __name__ == "__main__":
    unittest.main()
