#!/usr/bin/env python3
"""Check two-wide core acceptance against current verification evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from check_architectural_slice import require, sha, verify as verify_slice

CONTRACT = 'config/two_wide_acceptance.json'
RECEIPT = 'evidence/receipts/two_wide.json'


def fresh(root, inputs, name):
    require(inputs, f'{name} receipt has no input hashes')
    for path, value in inputs.items():
        require((root/path).is_file() and sha((root/path).read_bytes()) == value, f'stale {name} input: {path}')


def bound(root, step, target, key):
    """Read an artifact only if its hash matches the parent receipt."""
    path = step.get(key)
    require(path, f'{target} reported no {key}')
    data = (root/path).read_bytes() if (root/path).is_file() else None
    require(data is not None and sha(data) == step.get(f'{key}_sha256'), f'changed or missing {target} {key}: {path}')
    return data.decode()


def unique(keys, expected, message):
    require(len(keys) == len(set(keys)) and set(keys) == expected, message)


def formal(root, name):
    """Require passing induction, all covers and detected mutations."""
    receipt = json.loads((root/f'evidence/receipts/{name}.json').read_text())
    config = json.loads((root/f'config/{name}.json').read_text())
    require(receipt['status'] == 'pass' and receipt['profile'] == config['profile'], 'formal gate did not pass')
    fresh(root, receipt['inputs_sha256'], name)
    require(f'config/{name}.json' in receipt['inputs_sha256'], f'{name} receipt does not bind its config')
    require(sha((root/receipt['archive']).read_bytes()) == receipt['archive_sha256'], 'formal archive mismatch')
    with tarfile.open(root/receipt['archive'], 'r:gz') as tar:
        raw = tar.extractfile('manifest.json').read()
    require(sha(raw) == receipt['manifest_sha256'], 'formal manifest mismatch')
    jobs = {}
    for job in json.loads(raw)['jobs']:
        require(job['name'] not in jobs, f'duplicate formal job: {job["name"]}')
        jobs[job['name']] = job
    require(any(j['mode'] == config['mode'] == 'prove' and j['status'] == 'PASS' and not j['expected_failure']
                for j in jobs.values()), 'missing passing formal proof')
    require(any(j['mode'] == 'cover' and j['status'] == 'PASS' and set(j['covers_reached']) == set(config['required_covers'])
                for j in jobs.values()), 'missing required formal cover')
    for mutation in config['mutations']:
        job = jobs.get(mutation['name'], {})
        require(job.get('status') == 'FAIL' and job.get('expected_failure') and job.get('traces')
                and set(job.get('assertions_failed', ())) & set(mutation['assertions']), f'undetected formal mutation: {mutation["name"]}')


def child(root, target, spec, step, log):
    """Validate differential or ACT4 results against their configuration."""
    receipt = json.loads(bound(root, step, target, 'receipt'))
    require(f'Receipt: {step["receipt"]}' in log, f'{target} log does not name its receipt')
    config = json.loads((root/spec['config']).read_text())
    require(receipt['schema'] == 1 and receipt['profile'] == config['profile'], f'{target} profile mismatch')
    fresh(root, receipt['inputs'], target)
    require(spec['config'] in receipt['inputs'], f'{target} receipt does not bind {spec["config"]}')
    require(not any(v for k, v in receipt.items() if k.endswith('_accepted')), f'{target} cannot self-promote')
    prefix = root/Path(step['receipt']).parent
    runs = receipt['runs']
    if 'selected_tests' in config:
        selected = config['selected_tests']
        require(len(selected) == len(set(selected)) == receipt['selected_tests'], f'{target} selection mismatch')
        require(receipt['unexpected_skips'] == config['unexpected_skips_allowed'] == 0, f'unexpected {target} skip')
        require(receipt['deferred_tests'] == config['deferred_tests'], f'unaccounted {target} deferral')
        require(set(receipt['upstream']) == set(selected), f'missing {target} source hashes')
        require(receipt['modes'] == spec['modes'], f'{target} mode mismatch')
        unique([(r['test'], r['mode'], r['seed']) for r in runs],
               {(str(Path(t).with_suffix('.elf')), m, s) for t in selected for m in spec['modes'] for s in config['seeds']},
               f'incomplete or duplicate {target} run matrix')
        for r in runs:
            require(0 < r['events'] <= config['instruction_limit'] and r['cycles'] > 0, f'invalid {target} budget')
            run_log = (prefix/f"{Path(r['test']).stem}_{r['mode']}_{r['seed']}.log").read_text()
            require(f"ACT4 PASS events={r['events']} cycles={r['cycles']}" in run_log, f'{target} completion mismatch')
            require(sha((prefix/'work/spec_ooo_rv32i/elfs'/r['test']).read_bytes()) == r['elf_sha256'], f'{target} ELF mismatch')
        for test in selected:
            require(f'ACT4 {Path(test).stem}: PASS' in log, f'{target} log lacks {test}')
        unique(receipt['negative_checks'], set(spec['negatives']), f'missing {target} negative checks')
        for name, error in spec['negatives'].items():
            require(error in (prefix/f'{name}.log').read_text(), f'missing {target} negative outcome: {name}')
    else:
        reference = spec['reference']
        require(receipt['limitations'] == config['limitations'], f'{target} scope mismatch')
        if reference == 'sail':
            require(receipt['sail_sha256'] == config['sail_binary_sha256'], f'{target} Sail pin mismatch')
        expected = {(s, 'normal') for s in config['seeds']} | {(42, m) for m in config['extra_modes']}
        if 'long_run' in config:
            expected.add((config['long_run']['seed'], 'normal'))
        unique([(r['seed'], r['mode']) for r in runs], expected, f'incomplete or duplicate {target} run matrix')
        trace = '{}.trace' if reference == 'sail' else 'spike_{}.log'
        for r in runs:
            require(r['compared_events'] > 0, f'empty {target} comparison')
            for path, field in ((f"{r['seed']}.elf", 'elf_sha256'), (trace.format(r['seed']), f'{reference}_trace_sha256'),
                                (f"core_{r['seed']}_{r['mode']}.log", 'rtl_log_sha256')):
                require(sha((prefix/path).read_bytes()) == r[field], f'{target} artifact mismatch: {path}')
            require(f" seed={r['seed']} {r['mode']}: PASS ({r['compared_events']} events)" in log, f'{target} log lacks seed {r["seed"]} {r["mode"]}')
        unique(receipt['mutations_detected'], set(spec['negatives']), f'missing {target} negative checks')
    return receipt


def evaluate(contract, steps, root=ROOT):
    """Evaluate requirements from command results and bound artifacts."""
    require(list(steps) == contract['commands'], 'missing or changed command')
    logs = {}
    for target, step in steps.items():
        require(step['returncode'] == 0, f'failed command: {target}')
        logs[target] = bound(root, step, target, 'log')
    rows = []
    for requirement in contract['requirements']:
        evidence = []
        if requirement.get('architectural_slice'):
            verify_slice(root/'evidence/receipts/architectural_slice.json', root)
            evidence.append('evidence/receipts/architectural_slice.json')
        for unit, counters in requirement.get('units', {}).items():
            path = f'evidence/receipts/{unit}.json'
            receipt = json.loads((root/path).read_text())
            require(receipt['status'] == 'pass' and receipt['mutations_detected'], f'{unit} did not pass with mutations')
            fresh(root, receipt['inputs_sha256'], unit)
            for counter in counters:
                require(sum(run.get(counter, 0) for run in receipt['simulations']) > 0, f'unhit cover: {unit}.{counter}')
            evidence.append(path)
        if 'formal' in requirement:
            formal(root, requirement['formal'])
            evidence.append(f"evidence/receipts/{requirement['formal']}.json")
        for target, witnesses in requirement.get('differentials', {}).items():
            require(target in contract['children'], f'{target} has no child contract')
            receipt = child(root, target, contract['children'][target], steps[target], logs[target])
            for witness in witnesses:
                require(any(run['coverage'].get(witness) for run in receipt['runs']), f'missing witness: {target}.{witness}')
            evidence.append(steps[target]['receipt'])
        rows.append(dict(id=requirement['id'], text=requirement['text'], evidence=evidence))
    blocking = [gap['id'] for gap in contract['gaps'] if gap['blocking']]
    return ('not_promoted' if blocking else 'promoted'), rows, blocking


def run(contract):
    work = ROOT/'out/two_wide'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work.mkdir(parents=True)
    env = dict(os.environ, CCACHE_DISABLE='1', MAKEFLAGS='', MFLAGS='')
    steps = {}
    for index, target in enumerate(contract['commands']):
        print(f'Two-wide core [{index+1}/{len(contract["commands"])}]: make {target}', flush=True)
        log = work/f'{index:02d}_{target}.log'
        before = time.monotonic()
        with log.open('w') as stream:
            result = subprocess.run(['make', '--no-print-directory', target], cwd=ROOT, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=contract['command_timeout_seconds'])
        receipts = [line.removeprefix('Receipt: ') for line in log.read_text().splitlines() if line.startswith('Receipt: ')]
        steps[target] = dict(returncode=result.returncode, log=str(log.relative_to(ROOT)), log_sha256=sha(log.read_bytes()),
                             duration_seconds=round(time.monotonic()-before, 3))
        if receipts:
            steps[target].update(receipt=receipts[-1], receipt_sha256=sha((ROOT/receipts[-1]).read_bytes()))
        print(f'  {"PASS" if result.returncode == 0 else "FAIL"} ({steps[target]["duration_seconds"]:.0f}s)', flush=True)
        require(result.returncode == 0, f'command failed; see {log}')
    return steps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true', help='re-evaluate the retained receipt against the current tree')
    args = parser.parse_args()
    contract = json.loads((ROOT/CONTRACT).read_text())
    if args.verify:
        receipt = json.loads((ROOT/RECEIPT).read_text())
        require(receipt['contract_sha256'] == sha((ROOT/CONTRACT).read_bytes()), 'contract changed since the run')
        status, rows, blocking = evaluate(contract, receipt['steps'])
        require((status, rows) == (receipt['status'], receipt['requirements']), 'retained receipt disagrees with evidence')
    else:
        started = datetime.now(timezone.utc).isoformat()
        steps = run(contract)
        status, rows, blocking = evaluate(contract, steps)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--', '.', ':!evidence'], cwd=ROOT))
        (ROOT/RECEIPT).write_text(json.dumps(dict(
            schema=1, gate='Two-wide core', status=status, profile=contract['profile'], claim_class=contract['claim_class'],
            contract_sha256=sha((ROOT/CONTRACT).read_bytes()), source_revision=revision, dirty_tree=dirty,
            started_utc=started, finished_utc=datetime.now(timezone.utc).isoformat(), steps=steps,
            requirements=rows, gaps=contract['gaps'], limitations=contract['limitations']), indent=2)+'\n')
    print(f'Two-wide core: {status.upper().replace("_", " ")}' + (f' (blocking gaps: {", ".join(blocking)})' if blocking else ''))


if __name__ == '__main__':
    main()
