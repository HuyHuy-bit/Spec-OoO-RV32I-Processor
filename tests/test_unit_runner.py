"""Failure-path checks for the shared verification runner."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.run_unit import ROOT, UnitRun, digest, local_headers, mutate, simulation_result
from tools.unit_profiles import PROFILES


class UnitRunnerTests(unittest.TestCase):
    def test_profiles_preserve_accepted_results_and_require_coverage(self):
        for name, profile in PROFILES.items():
            with self.subTest(profile=name):
                config = json.loads((ROOT/f'config/{name}.json').read_text())
                receipt = ROOT/f'evidence/receipts/{name}.json'
                if not receipt.exists():
                    receipt = ROOT/f'out/{name}/receipt.json'
                if not receipt.exists():
                    self.skipTest(f'Local receipt absent; run tools/run_unit.py {name}')
                results = json.loads(receipt.read_text())['simulations']
                for fields in results:
                    output = profile['prefix']+' '.join(f'{k}={v}' for k, v in fields.items())+'\n'
                    with self.subTest(profile=name, seed=fields['seed']):
                        self.assertEqual(simulation_result(output, profile, config, fields['seed']), fields)
                        with self.assertRaises(RuntimeError):
                            simulation_result(output, profile, config, fields['seed']+1)
                        for key in profile['required']:
                            broken = dict(fields, **{key: 0})
                            text = profile['prefix']+' '.join(f'{k}={v}' for k, v in broken.items())+'\n'
                            with self.assertRaises(RuntimeError):
                                simulation_result(text, profile, config, fields['seed'])

    def test_all_mutation_selectors_still_match(self):
        for name, profile in PROFILES.items():
            for mutation, (path, old, new, count) in profile['mutations'].items():
                with self.subTest(profile=name, mutation=mutation):
                    original = (ROOT/path).read_text()
                    self.assertNotEqual(mutate(original, old, new, count), original)
                    with self.assertRaises(RuntimeError):
                        mutate(original, old, new, count+1)

    def test_header_inventory_is_transitive_and_handles_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text('#include "first.hpp"\n#include "generated.hpp"\n')
            (root/'first.hpp').write_text('#include "second.hpp"\n')
            (root/'second.hpp').write_text('#include "first.hpp"\n')
            self.assertEqual(local_headers(root, root/'test.cpp'), {root/'first.hpp', root/'second.hpp'})

    def test_failed_commands_need_nonzero_exit_and_the_expected_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(UnitRun)
            run.root = run.out = Path(directory)
            run.profile = {'timeout': 1}
            run.commands, run.artifacts = [], set()
            for code, text, accepted in [(0,'detected',False), (1,'compiler error',False), (1,'detected',True)]:
                with patch('tools.run_unit.subprocess.run', return_value=subprocess.CompletedProcess([],code,text)):
                    if accepted:
                        self.assertEqual(run.run(['unused'], 'test.log', failure='detected'), text)
                    else:
                        with self.assertRaises(RuntimeError):
                            run.run(['unused'], 'test.log', failure='detected')
            with patch('tools.run_unit.subprocess.run', side_effect=subprocess.TimeoutExpired('unused',1)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    run.run(['unused'], 'timeout.log', failure='detected')

    def test_changed_shared_input_cannot_publish_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            run = object.__new__(UnitRun)
            run.root = run.out = Path(directory)
            source = run.root/'shared.hpp'
            source.write_text('before')
            run.inputs = {'shared.hpp': digest(source)}
            source.write_text('after')
            with self.assertRaisesRegex(RuntimeError, 'input changed'):
                run.publish({})
            self.assertFalse((run.out/'receipt.json').exists())


class StorageTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', [str(ROOT/'tools'), *sys.path]):
            import run_storage_pilot
        cls.read_timing = staticmethod(run_storage_pilot.timing_measurements)
        cls.check_rc = staticmethod(run_storage_pilot.verify_corner_rc)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.corner = 'nom_tt_025C_1v80'
        self.metrics = {k+'__corner:'+self.corner: 0 for k in (
            'design__max_slew_violation__count', 'design__max_cap_violation__count',
            'design__max_fanout_violation__count', 'timing__setup_vio__count', 'timing__hold_vio__count')}
        self.metrics.update({'timing__setup__ws__corner:'+self.corner: 1.0,
                             'timing__hold__ws__corner:'+self.corner: 0.1})
        (self.stage/'checks.rpt').write_text('check_setup -verbose\n====\n')

    def read(self):
        (self.stage/'or_metrics_out.json').write_text(json.dumps(self.metrics))
        return self.read_timing(self.stage, self.corner)

    def test_fresh_corner_passes(self):
        self.assertTrue(self.read()['reported_checks_passed'])

    def test_stale_state_metrics_cannot_hide_fresh_violations(self):
        (self.stage/'state_out.json').write_text(json.dumps({'metrics': self.metrics}))
        self.metrics['timing__hold_vio__count__corner:'+self.corner] = 1
        self.assertFalse(self.read()['reported_checks_passed'])

    def test_negative_slack_fails_even_with_zero_counts(self):
        self.metrics['timing__hold__ws__corner:'+self.corner] = -0.001
        self.assertFalse(self.read()['reported_checks_passed'])

    def test_nonfinite_and_missing_path_slack_rejected(self):
        for value in (float('nan'), float('inf'), 1e30):
            with self.subTest(value=value):
                self.metrics['timing__setup__ws__corner:'+self.corner] = value
                with self.assertRaisesRegex(RuntimeError, 'invalid or missing-path'):
                    self.read()

    def test_missing_constraint_rejected(self):
        (self.stage/'checks.rpt').write_text('check_setup -verbose\n====\nWarning: missing input delay\n')
        with self.assertRaisesRegex(RuntimeError, 'coverage is incomplete'):
            self.read()

    def test_wrong_or_mixed_corner_rejected(self):
        self.metrics['timing__hold__ws__corner:max_ss_100C_1v60'] = 1.0
        with self.assertRaisesRegex(RuntimeError, 'requested corner'):
            self.read()
        self.metrics = {k.replace(self.corner, 'min_ff_n40C_1v95'): v for k, v in self.metrics.items()}
        with self.assertRaisesRegex(RuntimeError, 'requested corner'):
            self.read()

    def test_missing_metric_rejected(self):
        del self.metrics['timing__hold_vio__count__corner:'+self.corner]
        with self.assertRaises(KeyError):
            self.read()

    def test_missing_or_incorrect_rc_overrides_rejected(self):
        rc = {'layers': {'met1': {'res': 0.001, 'cap': 0.0001}}, 'vias': {'via': {'res': 0.009}}}
        layer = '+ set_layer_rc -layer met1 -capacitance 0.0001 -corner '+self.corner+' -resistance 0.001\n'
        via = '+ set_layer_rc -corner '+self.corner+' -resistance 0.009 -via via\n'
        self.check_rc(layer+via, self.corner, rc)
        for log in (layer, via, (layer+via).replace('0.009', '0.000009'),
                    (layer+via).replace(self.corner, 'max_ss_100C_1v60')):
            with self.subTest(log=log), self.assertRaises(RuntimeError):
                self.check_rc(log, self.corner, rc)


if __name__ == '__main__':
    unittest.main()
