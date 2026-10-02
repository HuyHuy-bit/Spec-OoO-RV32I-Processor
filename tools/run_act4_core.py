#!/usr/bin/env python3
"""Build selected ACT4 self-checking ELFs with Sail and execute them on the selected RTL core."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.act4_tools import CHECKOUT, SAIL, environment
from tools.check_act4 import check_harness, check_upstream, check_sail
from tools.run_single_lane import build_core
from tools.run_sail_differential import build_fetched
from verif.arch.elf_image import load_elf
from verif.core.programs import constant, i, store, branch


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Memory map hardcoded by the bench drivers (both cores share it).
BENCH_MAP = [('bram', 0, 0x10000), ('uart', 0x10000000, 0x10), ('gpio', 0x10001000, 0x20)]
DUTS = {
    'single': dict(core='config/single_lane.json', selection='config/act4_single_lane.json',
                   driver='verif/core/single_lane_tb.cpp', out='out/act4_single_lane', modes=['act4']),
    'fetched': dict(core='config/memory_core.json', selection='config/act4_fetched.json',
                    driver='verif/core/fetched_core_tb.cpp', out='out/act4_fetched', modes=['normal', 'stall']),
}


def check_selection(selection, checkout):
    """Frozen selection: unique existing tests, and every covered pinned test is selected or deferred with a reason."""
    tests = selection['selected_tests']
    deferred = {d['test']: d['reason'] for d in selection['deferred_tests']}
    if not tests or len(tests) != len(set(tests)) or selection['unexpected_skips_allowed'] != 0 \
            or set(tests) & set(deferred) or not all(deferred.values()):
        raise ValueError('invalid ACT4 selection')
    for name in [*tests, *deferred]:
        if Path(name).is_absolute() or '..' in Path(name).parts or not (checkout/'tests'/name).is_file():
            raise ValueError(f'invalid selected ACT4 source: {name}')
    for directory in selection.get('complete_directories', []):
        pinned = {str(p.relative_to(checkout/'tests')) for p in (checkout/'tests'/directory).glob('*.S')}
        if not pinned or pinned - set(tests) - set(deferred):
            raise ValueError(f'unexpected ACT4 skips in {directory}: {sorted(pinned - set(tests) - set(deferred))}')
    return tests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dut', choices=tuple(DUTS), default='single')
    parser.add_argument('--checkout', type=Path, default=CHECKOUT)
    parser.add_argument('--sail', type=Path, default=SAIL)
    args = parser.parse_args()
    dut = DUTS[args.dut]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    checkout, sail = args.checkout.resolve(), args.sail.resolve()
    lock, suite = check_harness()
    check_upstream(checkout, lock, suite)
    check_sail(sail, lock)
    env = environment(checkout, sail)
    versions = {}
    for tool in ('ruby', 'uv', 'riscv64-unknown-elf-gcc', 'verilator'):
        command = shutil.which(tool, path=env['PATH'])
        if not command: raise RuntimeError(f'{tool} missing; run make act4-tools')
        versions[tool] = subprocess.check_output([command, '--version'], env=env, text=True).splitlines()[0]
    if not versions['ruby'].startswith('ruby '+lock['tools']['ruby']+' '): raise RuntimeError('Ruby pin mismatch')
    if not versions['uv'].startswith('uv '+lock['tools']['uv']+' '): raise RuntimeError('uv pin mismatch')
    if versions['riscv64-unknown-elf-gcc'] != 'riscv64-unknown-elf-gcc () 15.1.0': raise RuntimeError('GCC pin mismatch')
    toolchain = json.loads((ROOT/'config/toolchain.lock').read_text())
    if versions['verilator'] != next(t['expected_first_line'] for t in toolchain['tools'] if t['name'] == 'verilator'):
        raise RuntimeError('Verilator pin mismatch')
    subprocess.run(['bundle', 'check'], env=env, cwd=checkout, check=True, capture_output=True)
    selection = json.loads((ROOT/dut['selection']).read_text())
    tests = check_selection(selection, checkout)
    platform = json.loads((ROOT/'config/platform.yaml').read_text())
    if [(r['name'], int(r['base'], 16), int(r['size'], 16)) for r in platform['memory']['regions']] != BENCH_MAP:
        raise RuntimeError('bench memory map differs from config/platform.yaml')
    subprocess.run([sys.executable, 'tools/gen_platform.py', '--input', 'config/platform.yaml', '--check'],
                   cwd=ROOT, check=True, capture_output=True)
    config = json.loads((ROOT/dut['core']).read_text())
    paths = config['sources'] + [dut['core'], dut['selection'],
        'config/act4.lock', 'config/act4_tools.lock', 'config/references.lock', 'config/platform.yaml',
        'config/memory_protocol.yaml', 'config/commit_event.yaml', 'config/toolchain.lock',
        'tools/run_act4_core.py', 'tools/run_single_lane.py', 'tools/act4_tools.py', 'tools/check_act4.py',
        'tools/gen_platform.py', dut['driver'], 'verif/arch/elf_image.py']
    if args.dut == 'fetched':
        paths += [config['lint_config'], 'tools/run_sail_differential.py', 'verif/unit/packed_bits.hpp']
    paths += [str(p.relative_to(ROOT)) for p in sorted((ROOT/lock['harness']['directory']).iterdir()) if p.is_file()]
    inputs = {p: digest(ROOT/p) for p in paths}
    upstream = {name: digest(checkout/'tests'/name) for name in tests}
    fingerprint = {'dut': args.dut, 'inputs': inputs, 'upstream': upstream, 'suite_revision': suite['revision'],
                   'versions': versions, 'sail_sha256': digest(sail)}
    run_hash = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    out = ROOT/dut['out']/run_hash[:16]
    out.mkdir(parents=True, exist_ok=True)
    receipt = out/'receipt.json'
    receipt.unlink(missing_ok=True)
    selected = out/'selected'
    selected.mkdir(exist_ok=True)
    for name in ['env', *tests]:
        link = selected/name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink(): link.symlink_to(checkout/'tests'/name)
        if link.resolve() != (checkout/'tests'/name).resolve(): raise ValueError('selected source link mismatch')
    def run(command, log, expected_error=None, run_env=None, cwd=ROOT):
        result = subprocess.run([str(c) for c in command], env=run_env, cwd=cwd, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        (out/log).write_text(result.stdout)
        if expected_error:
            if result.returncode == 0 or expected_error not in result.stdout:
                raise RuntimeError(f'negative check did not detect {expected_error}; see {out/log}')
        elif result.returncode:
            raise RuntimeError(f'command failed; see {out/log}\n{result.stdout[-4000:]}')
        return result.stdout
    print(f'ACT4 {args.dut} build: {len(tests)} selected tests, Sail {lock["tools"]["sail_riscv"]}', flush=True)
    run(['bundle', 'exec', 'uv', 'run', '--frozen', '--no-sync', 'act', ROOT/lock['harness']['directory']/lock['harness']['config'],
         '--test-dir', selected, '--workdir', out/'work', '--jobs', '2'],
        'build.log', run_env=env, cwd=checkout)
    elfdir = out/'work/spec_ooo_rv32i/elfs'
    expected = {str(Path(t).with_suffix('.elf')) for t in tests}
    actual = {str(p.relative_to(elfdir)) for p in elfdir.rglob('*.elf')}
    if actual != expected: raise RuntimeError(f'ACT4 output selection mismatch: missing={expected-actual}, extra={actual-expected}')
    binary = build_fetched(out/'obj_dir')[0] if args.dut == 'fetched' else build_core(config)
    modes = dut['modes']
    def driver(image, seed, limit, tohost, mode=modes[0]):
        return [binary, image, seed, limit, mode, tohost]
    results = []
    first = None
    for name in sorted(expected):
        elf = elfdir/name
        image, symbols = load_elf(elf.read_bytes())
        imagefile = out/(Path(name).stem+'.bin')
        imagefile.write_bytes(image)
        if first is None: first = (image, symbols)
        for mode in modes:
            for seed in selection['seeds']:
                log = f'{Path(name).stem}_{mode}_{seed}.log'
                output = run([*driver(imagefile, seed, selection['instruction_limit'], symbols['tohost'], mode),
                              '+verilator+rand+reset+2', f'+verilator+seed+{seed}'], log)
                match = re.search(r'^ACT4 PASS events=(\d+) cycles=(\d+)( dual=\d+)?$', output, re.MULTILINE)
                if not match: raise RuntimeError(f'ACT4 did not complete: {name} {mode} seed {seed}')
                results.append({'test': name, 'mode': mode, 'seed': seed, 'elf_sha256': digest(elf),
                                'events': int(match[1]), 'cycles': int(match[2])})
        print(f'ACT4 {Path(name).stem}: PASS ({len(modes)} modes x {len(selection["seeds"])} seeds)', flush=True)
    image, symbols = first
    corrupted = bytearray(image)
    corrupted[symbols['rvtest_sig_begin']+4] ^= 1
    bad = out/'corrupt_signature.bin'; bad.write_bytes(corrupted)
    run(driver(bad, 42, selection['instruction_limit'], symbols['tohost']),
        'corrupt_signature.log', expected_error='ACT4 FAIL tohost=3')
    import struct
    negatives = [
        ('fail', constant(1, 0x8000)+[i(2,0,3), store(1,2,0,2), branch(0,0,0)], 'ACT4 FAIL tohost=3', modes[0]),
        ('timeout', [branch(0,0,0)], 'ACT4 instruction limit without tohost completion', modes[0]),
    ]
    if args.dut == 'fetched':
        negatives.append(('undrained_tohost', constant(1, 0x8000)+[i(2,0,1), store(1,2,0,2), branch(0,0,0)],
                          'ACT4 tohost store did not drain', 'hold_drain'))
    for name, words, error, mode in negatives:
        path = out/f'{name}.bin'
        path.write_bytes(struct.pack('<'+'I'*len(words), *words))
        run(driver(path, 42, 50, 0x8000, mode), name+'.log', expected_error=error)
    if any(digest(ROOT/p) != h for p, h in inputs.items()): raise RuntimeError('inputs changed during ACT4 run')
    checks = ['corrupt_signature', *(n[0] for n in negatives)]
    acceptance = {k: v for k, v in selection.items() if k.endswith('_accepted')}
    receipt.write_text(json.dumps({**fingerprint, 'schema': 1, 'profile': selection['profile'], 'modes': modes,
        'runs': results, 'selected_tests': len(tests), 'deferred_tests': selection['deferred_tests'],
        'unexpected_skips': 0, 'negative_checks': checks, **acceptance}, indent=2)+'\n')
    print(f'ACT4 {args.dut} core: PASS ({len(tests)} tests, {len(results)} runs, zero unexpected skips, '
          f'{len(checks)} negative checks)', flush=True)
    print(f'Receipt: {receipt.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
