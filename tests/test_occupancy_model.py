import json
from pathlib import Path
import unittest

from model.occupancy import Machine, ModelError, Op, simulate, workload

ROOT = Path(__file__).resolve().parents[1]


def loads(*misses):
    return [Op("load", miss=miss) for miss in misses]


class OccupancyModelTest(unittest.TestCase):
    def test_hand_trace_two_miss_slots(self):
        # Two miss slots launch loads at cycles 1, 2 and 11; each response takes 10 cycles.
        ops = loads(True, True, True)
        result = simulate(Machine(mshr=2, credits=2, miss_latency=10), ops)
        self.assertEqual([op.retire_cycle for op in ops], [11, 12, 21])
        self.assertEqual(result["max_outstanding_misses"], 2)
        self.assertEqual(result["total_cycles"], 22)

    def test_blocking_cache_has_no_hit_under_miss(self):
        ops = loads(True, False)
        simulate(Machine(mshr=1, credits=1, miss_latency=10), ops)
        self.assertEqual([op.retire_cycle for op in ops], [11, 13])
        ops = loads(True, False)
        simulate(Machine(mshr=2, credits=2, miss_latency=10), ops)
        self.assertEqual([op.retire_cycle for op in ops], [11, 11])

    def test_serial_chain_cannot_overlap_misses(self):
        ops = workload(7, 400, load_density=0.5, miss_rate=1.0, chains=1)
        result = simulate(Machine(lq=16, mshr=8, credits=8, miss_latency=50), ops)
        self.assertEqual(result["max_outstanding_misses"], 1)

    def test_one_target_credit_caps_eight_slots(self):
        ops = workload(7, 400, load_density=0.5, miss_rate=1.0)
        result = simulate(Machine(lq=16, mshr=8, credits=1, miss_latency=50), ops)
        self.assertEqual(result["max_outstanding_misses"], 1)
        self.assertGreater(result["credit_full_frac"], 0)

    def test_independent_stream_reaches_slot_limit(self):
        ops = workload(7, 400, load_density=0.5, miss_rate=1.0)
        result = simulate(Machine(lq=16, mshr=4, credits=8, miss_latency=100), ops)
        self.assertEqual(result["max_outstanding_misses"], 4)
        self.assertGreater(result["mshr_full_frac"], 0)

    def test_lq_limits_eight_slots(self):
        ops = workload(7, 400, load_density=0.5, miss_rate=1.0)
        result = simulate(Machine(lq=8, mshr=8, credits=8, miss_latency=100), ops)
        self.assertLessEqual(result["max_outstanding_misses"], 8)
        self.assertGreater(result["lq_full_frac"], 0)

    def test_backpressured_completions_drain(self):
        ops = workload(3, 600, load_density=0.4, miss_rate=0.2)
        result = simulate(Machine(mshr=4, credits=4, completion_buffer=1, miss_latency=20), ops)
        self.assertTrue(all(op.retire_cycle is not None for op in ops))
        self.assertGreater(result["wb_congested_frac"], 0)

    def test_ipc_uses_the_measured_cycle_window(self):
        # The bundle crossing warmup belongs to an uncounted cycle.
        result = simulate(Machine(), [Op("alu") for _ in range(4)], warmup=1)
        self.assertEqual((result["cycles"], result["ipc"], result["wb_per_cycle"]), (1, 2.0, 2.0))
        for warmup in range(0, 300, 37):
            result = simulate(Machine(mshr=4, credits=4), workload(3, 400, 0.3, 0.1, 0.2), warmup)
            self.assertLessEqual(result["ipc"], Machine().width)

    def test_seed_reproduces(self):
        run = lambda: simulate(Machine(mshr=4, credits=4), workload(11, 500, 0.4, 0.1, 0.3, 2), 50)
        self.assertEqual(run(), run())

    def test_checks_reject_faults(self):
        machine = Machine(lq=8, mshr=2, credits=2, miss_latency=30)
        for fault in ("overissue", "early_lq_release", "lose_response"):
            with self.subTest(fault=fault), self.assertRaises(ModelError):
                simulate(machine, workload(5, 300, 0.5, miss_rate=1.0), fault=fault)

    def test_sweep_config_matches_model(self):
        config = json.loads((ROOT / "config/occupancy_sweep.json").read_text())
        Machine(**config["machine"])
        for spec in config["workloads"].values():
            workload(config["seed"], 10, **spec)
        self.assertEqual(set(config["sweep"]["lq"]), {8, 16})


if __name__ == "__main__":
    unittest.main()
