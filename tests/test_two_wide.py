import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.check_two_wide import evaluate

DIFF, ACT = 'sail-fetched-differential-check', 'act4-fetched-check'
CONTRACT = {
    'commands': ['rob-two-wide-check', DIFF, ACT],
    'children': {
        DIFF: {'config': 'diff.json', 'reference': 'sail', 'negatives': ['fatal', 'short_trace']},
        ACT: {'config': 'act.json', 'modes': ['normal', 'stall'], 'negatives': {'fail': 'ACT4 FAIL tohost=3'}},
    },
    'requirements': [
        {'id': 'dual_lane_flow', 'text': 'dual', 'units': {'rob_two_wide': ['dual_retire']}},
        {'id': 'identity_wraparound', 'text': 'wrap', 'differentials': {DIFF: ['rob_generation_wrap']}},
        {'id': 'rename_ownership_proof', 'text': 'formal', 'formal': 'proof'},
        {'id': 'selected_act4', 'text': 'act4', 'differentials': {ACT: []}},
    ],
    'gaps': [],
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TwoWideCheckerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.write('rtl.sv', 'module m; endmodule\n')
        rtl = {'rtl.sv': self.hash('rtl.sv')}
        self.write('evidence/receipts/rob_two_wide.json', dict(
            status='pass', mutations_detected=['m'], inputs_sha256=rtl, simulations=[{'dual_retire': 0}, {'dual_retire': 3}]))

        self.write('diff.json', dict(profile='diff', seeds=[1, 42], extra_modes=['stall'], long_run={'seed': 7},
                                     limitations=['l'], sail_binary_sha256='s'))
        runs, lines = [], []
        for seed, mode in ((1, 'normal'), (42, 'normal'), (42, 'stall'), (7, 'normal')):
            for name in (f'd/{seed}.elf', f'd/{seed}.trace', f'd/core_{seed}_{mode}.log'):
                self.write(name, name)
            runs.append(dict(seed=seed, mode=mode, compared_events=5, coverage={'rob_generation_wrap': int(seed == 7)},
                             elf_sha256=self.hash(f'd/{seed}.elf'), sail_trace_sha256=self.hash(f'd/{seed}.trace'),
                             rtl_log_sha256=self.hash(f'd/core_{seed}_{mode}.log')))
            lines.append(f'Sail differential fetched seed={seed} {mode}: PASS (5 events)')
        self.write('d/receipt.json', dict(schema=1, profile='diff', inputs={**rtl, 'diff.json': self.hash('diff.json')},
                                          sail_sha256='s', runs=runs, mutations_detected=['fatal', 'short_trace'],
                                          limitations=['l'], two_wide_core_accepted=False))
        self.write('diff.log', '\n'.join(lines + ['Receipt: d/receipt.json']))

        self.write('act.json', dict(profile='act', seeds=[1, 42], instruction_limit=100, selected_tests=['I/add.S', 'I/lw.S'],
                                    deferred_tests=[{'test': 'I/beq.S', 'reason': 'size'}], unexpected_skips_allowed=0))
        runs, lines = [], []
        for test in ('add', 'lw'):
            self.write(f'a/work/spec_ooo_rv32i/elfs/I/{test}.elf', test)
            for mode in ('normal', 'stall'):
                for seed in (1, 42):
                    self.write(f'a/{test}_{mode}_{seed}.log', 'ACT4 PASS events=9 cycles=20\n')
                    runs.append(dict(test=f'I/{test}.elf', mode=mode, seed=seed, events=9, cycles=20,
                                     elf_sha256=self.hash(f'a/work/spec_ooo_rv32i/elfs/I/{test}.elf')))
            lines.append(f'ACT4 {test}: PASS (2 modes x 2 seeds)')
        self.write('a/fail.log', 'ACT4 FAIL tohost=3\n')
        self.write('a/receipt.json', dict(schema=1, profile='act', inputs={**rtl, 'act.json': self.hash('act.json')},
                                          upstream={'I/add.S': 'x', 'I/lw.S': 'y'}, modes=['normal', 'stall'], runs=runs,
                                          selected_tests=2, deferred_tests=[{'test': 'I/beq.S', 'reason': 'size'}],
                                          unexpected_skips=0, negative_checks=['fail'], two_wide_core_accepted=False))
        self.write('act.log', '\n'.join(lines + ['Receipt: a/receipt.json']))

        self.write('config/proof.json', dict(profile='proof', mode='prove', required_covers=['c1', 'c2'],
                                             mutations=[{'name': 'mut', 'assertions': ['owner']}]))
        self.jobs = [dict(name='covers', mode='cover', status='PASS', expected_failure=False, covers_reached=['c1', 'c2']),
                     dict(name='mut', mode='bmc', status='FAIL', expected_failure=True, assertions_failed=['owner'], traces=['t']),
                     dict(name='baseline', mode='prove', status='PASS', expected_failure=False)]
        self.archive(self.jobs)

        self.write('rob.log', 'ok')
        self.steps = {'rob-two-wide-check': self.step('rob.log'),
                      DIFF: self.step('diff.log', 'd/receipt.json'), ACT: self.step('act.log', 'a/receipt.json')}

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, value):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def hash(self, name):
        return sha((self.root/name).read_bytes())

    def step(self, log, receipt=None):
        return dict(returncode=0, log=log, log_sha256=self.hash(log),
                    **(dict(receipt=receipt, receipt_sha256=self.hash(receipt)) if receipt else {}))

    def archive(self, jobs):
        manifest = json.dumps({'jobs': jobs}).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            info = tarfile.TarInfo('manifest.json'); info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
        (self.root/'proof.tar.gz').write_bytes(buffer.getvalue())
        inputs = {'config/proof.json': self.hash('config/proof.json')}
        self.write('evidence/receipts/proof.json', dict(status='pass', profile='proof', inputs_sha256=inputs, archive='proof.tar.gz',
                                                         archive_sha256=self.hash('proof.tar.gz'), manifest_sha256=sha(manifest)))

    def evaluate(self, steps=None, contract=CONTRACT):
        return evaluate(contract, steps or self.steps, self.root)

    def rejects(self, message, path, change):
        """Rehash altered receipts to exercise content checks."""
        original = (self.root/path).read_text()
        data = json.loads(original); change(data); self.write(path, data)
        steps = copy.deepcopy(self.steps)
        for step in steps.values():
            if step.get('receipt') == path:
                step['receipt_sha256'] = self.hash(path)
        with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
            self.evaluate(steps)
        self.write(path, original)

    def test_complete_evidence_promotes_and_blocking_gap_does_not(self):
        status, rows, blocking = self.evaluate()
        self.assertEqual((status, [r['id'] for r in rows], blocking), ('promoted', ['dual_lane_flow', 'identity_wraparound', 'rename_ownership_proof', 'selected_act4'], []))
        contract = dict(CONTRACT, gaps=[{'id': 'G1', 'blocking': True}, {'id': 'G2', 'blocking': False}])
        self.assertEqual(self.evaluate(contract=contract)[::2], ('not_promoted', ['G1']))

    def test_rejects_missing_failed_and_unmutated_evidence(self):
        missing = dict(self.steps); del missing['rob-two-wide-check']
        failed = copy.deepcopy(self.steps); failed['rob-two-wide-check']['returncode'] = 2
        for steps, message in ((missing, 'missing or changed command'), (failed, 'failed command')):
            with self.assertRaisesRegex(ValueError, message): self.evaluate(steps)
        self.write('evidence/receipts/rob_two_wide.json', dict(status='pass', mutations_detected=[], inputs_sha256={}, simulations=[]))
        with self.assertRaisesRegex(ValueError, 'did not pass'): self.evaluate()

    def test_rejects_stale_child_input(self):
        self.write('rtl.sv', 'module changed; endmodule\n')
        with self.assertRaisesRegex(ValueError, 'stale rob_two_wide input'): self.evaluate()

    def test_rejects_changed_receipt_or_log(self):
        for path in ('a/receipt.json', 'act.log', 'rob.log'):
            original = (self.root/path).read_text()
            self.write(path, original + ' ')
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'changed or missing'): self.evaluate()
            self.write(path, original)

    def test_rejects_incomplete_act4_evidence(self):
        self.rejects('run matrix', 'a/receipt.json', lambda d: d.update(runs=d['runs'][:1]))
        self.rejects('run matrix', 'a/receipt.json', lambda d: d['runs'].__setitem__(1, d['runs'][0]))
        self.rejects('unexpected', 'a/receipt.json', lambda d: d.update(unexpected_skips=38))
        self.rejects('negative checks', 'a/receipt.json', lambda d: d.update(negative_checks=[]))
        self.rejects('negative checks', 'a/receipt.json', lambda d: d.update(negative_checks=['fail', 'fail']))
        self.rejects('deferral', 'a/receipt.json', lambda d: d.update(deferred_tests=[]))
        self.rejects('self-promote', 'a/receipt.json', lambda d: d.update(two_wide_core_accepted=True))
        self.write('a/fail.log', 'passed\n')
        with self.assertRaisesRegex(ValueError, 'negative outcome'): self.evaluate()

    def test_rejects_incomplete_differential_evidence(self):
        self.rejects('run matrix', 'd/receipt.json', lambda d: d.update(runs=d['runs'][:-1]))
        self.rejects('run matrix', 'd/receipt.json', lambda d: d['runs'].append(d['runs'][0]))
        self.rejects('negative checks', 'd/receipt.json', lambda d: d.update(mutations_detected=['fatal']))
        self.rejects('scope', 'd/receipt.json', lambda d: d.update(limitations=[]))
        self.rejects('missing witness', 'd/receipt.json', lambda d: d['runs'][-1]['coverage'].update(rob_generation_wrap=0))
        self.rejects('does not bind', 'd/receipt.json', lambda d: d['inputs'].pop('diff.json'))
        self.write('d/core_42_stall.log', 'different')
        with self.assertRaisesRegex(ValueError, 'artifact mismatch'): self.evaluate()

    def test_rejects_missing_formal_proof_cover_or_mutation(self):
        for index, change, message in ((2, dict(status='FAIL'), 'formal proof'),
                                       (0, dict(covers_reached=['c1']), 'formal cover'),
                                       (1, dict(assertions_failed=['other']), 'formal mutation')):
            jobs = copy.deepcopy(self.jobs); jobs[index].update(change); self.archive(jobs)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message): self.evaluate()
        self.archive(self.jobs + [self.jobs[0]])
        with self.assertRaisesRegex(ValueError, 'duplicate formal job'): self.evaluate()


if __name__ == '__main__':
    unittest.main()
