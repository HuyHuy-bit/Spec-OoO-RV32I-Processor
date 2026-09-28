#!/usr/bin/env python3
"""Run bounded rename or load-handshake checks with shared formal tooling."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import time

from run_assert_portability import digest, require

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=Path.home()/'tools/oss-cad-suite-20260905/oss-cad-suite')
    parser.add_argument('--profile', choices=['rename_ownership', 'load_transaction_handshake'], default='rename_ownership')
    args = parser.parse_args()
    suite = args.suite.resolve()
    config_path = f'config/{args.profile}.json'
    config = json.loads((ROOT/config_path).read_text())
    load = args.profile == 'load_transaction_handshake'
    title = 'Load handshake' if load else 'Rename ownership'
    if not load:
        require((config['physical_registers'], config['architectural_registers'], config['maximum_inflight']) == (64, 32, 1), 'unsupported geometry')
    else:
        require(config['depth'] == 20 and config['top'] == args.profile, 'unsupported handshake bound or top')
    require(config['mode'] == 'bmc' and config['engine'] == ('smtbmc yices' if load else 'abc bmc3')
            and config['cover_engine'] == 'smtbmc yices', 'unsupported proof mode')
    mutations = config.get('mutations', [])
    if load:
        from unit_profiles import PROFILES
        mutations = []
        for name, assertions in config['unit_mutations'].items():
            source, old, new, count = PROFILES[config['unit_mutation_profile']]['mutations'][name]
            require(count == 1, 'unsupported mutation match count')
            mutations.append(dict(name=name, source=source, old=old, new=new, assertions=assertions))
    harness = (ROOT/config['sources'][-1]).read_text()
    require(set(re.findall(r'(\w+): cover', harness)) == set(config['required_covers']), 'cover contract differs')
    lock = json.loads((ROOT/'config/formal_tools.lock').read_text())
    synthesis = json.loads((ROOT/lock['synthesis_lock']).read_text())
    for name, value in {**synthesis['sha256'], **lock['sha256']}.items():
        require((suite/name).is_file() and digest(suite/name) == value, f'tool pin differs: {name}')
    require(digest(Path('/usr/bin/time')) == lock['time_sha256'], 'resource measurement tool differs')
    versions = {}
    for name, expected in {**lock['versions'], 'yosys': synthesis['yosys_version']}.items():
        versions[name] = subprocess.check_output([str(suite/'bin'/name), '--version'], text=True).splitlines()[0]
        require(versions[name] == expected, f'{name} version differs')
    paths = [*config['sources'], config_path, 'config/formal_tools.lock',
             lock['synthesis_lock'], 'tools/run_rename_ownership.py', 'tools/run_assert_portability.py',
             *config.get('extra_inputs', [])]
    inputs = {p: digest(ROOT/p) for p in paths}
    run_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    out = ROOT/'out'/args.profile/run_hash
    out.mkdir(parents=True, exist_ok=True)
    receipt = out/'receipt.json'
    receipt.unlink(missing_ok=True)
    retained = ROOT/f'evidence/receipts/{args.profile}.json'
    if load:
        retained.unlink(missing_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    env = dict(os.environ, PATH=str(suite/'bin')+os.pathsep+os.environ['PATH'])
    jobs = []
    variants = [dict(name='covers', mode='cover')]
    variants += [dict(m, mode='bmc') for m in mutations]
    variants.append(dict(name='baseline', mode='bmc'))
    for variant in variants:
        name = variant['name']
        directory = out/name
        directory.mkdir(exist_ok=True)
        sources = [ROOT/p for p in config['sources']]
        if 'old' in variant:
            index = config['sources'].index(variant['source']) if 'source' in variant else 2
            original = sources[index].read_text()
            require(original.count(variant['old']) == 1, f'mutation no longer matches: {name}')
            changed = directory/sources[index].name
            changed.write_text('`define SYNTHESIS\n'+original.replace(variant['old'], variant['new'])+'\n`undef SYNTHESIS\n')
            sources[index] = changed
        task = directory/'check.sby'
        engine = config['cover_engine'] if variant['mode'] == 'cover' else config['engine']
        task.write_text(f'[options]\nmode {variant["mode"]}\ndepth {config["depth"]}\n\n'
                        f'[engines]\n{engine}\n\n[script]\nplugin -i slang\n'
                        f'read_slang --no-synthesis-define --top {config["top"]} '+ ' '.join(p.name for p in sources)+'\n'
                        f'prep -top {config["top"]}\n\n[files]\n'+'\n'.join(str(p) for p in sources)+'\n')
        proof = directory/'proof'
        log = directory/'check.log'
        rss = directory/'memory.rss'
        command = ['/usr/bin/time', '-f', '%M', '-o', str(rss), str(suite/'bin/sby'), '-f', '-d', str(proof), str(task)]
        print(f'{title} {name}: running {variant["mode"]} depth={config["depth"]}', flush=True)
        before = time.monotonic()
        with log.open('w') as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = child.wait(timeout=config['timeout_seconds'])
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise RuntimeError(f'{name} interrupted or timed out; see {log}')
        output = log.read_text()
        status = (proof/'status').read_text().split()[0] if (proof/'status').is_file() else 'ERROR'
        mutant = 'old' in variant
        require(status == ('FAIL' if mutant else 'PASS') and ((code != 0) if mutant else (code == 0)), f'{name}: unexpected status {status}; see {log}')
        summary = (proof/status).read_text()
        assertions = []
        for filename, line in re.findall(r'failed assertion \S+ at (\w+\.sv):(\d+)\.', summary):
            source = next(p for p in sources if p.name == filename)
            label = re.search(r'(\w+):\s*assert\b', source.read_text().splitlines()[int(line)-1])
            assertions.append(label[1] if label else f'{filename}:{line}')
        traces = sorted(str(p.relative_to(ROOT)) for p in (proof/'engine_0').glob('trace*.vcd'))
        if mutant:
            require(traces, f'{name}: missing counterexample')
        engine_log = '\n'.join(p.read_text() for p in sorted((proof/'engine_0').glob('logfile*.txt')))
        top = re.escape(config['top'])
        if load:
            assertions += re.findall(r'Assert failed in '+top+r': (\w+)', engine_log)
            assertions = sorted(set(assertions))
        if mutant:
            require(any(expected in assertions for expected in variant['assertions']), f'{name}: wrong assertion failed: {assertions}')
        reached = re.findall(r'reached cover statement '+top+r'\.(\w+)', summary)
        if load:
            reached += re.findall(r'Reached cover statement in step \d+ at '+top+r': (\w+)', engine_log)
            reached = sorted(set(reached))
        if variant['mode'] == 'cover':
            require(set(reached) == set(config['required_covers']) and traces, 'incomplete required covers')
        if load and variant['mode'] == 'bmc' and not mutant:
            steps = [int(n) for n in re.findall(r'Checking assertions in step (\d+)', engine_log)]
            require(steps and max(steps) == config['depth']-1, 'incomplete bounded safety run')
        jobs.append(dict(name=name, mode=variant['mode'], engine=engine, depth=config['depth'], status=status,
                         expected_failure=mutant, command=command, duration_seconds=round(time.monotonic()-before, 6),
                         max_rss_kib=int(rss.read_text().splitlines()[-1]), log=str(log.relative_to(ROOT)),
                         assertions_failed=assertions, covers_reached=reached, traces=traces,
                         dut_assertions_enabled=not mutant))
        print(f'{title} {name}: PASS ({"fault detected" if mutant else status})', flush=True)
    require(all(digest(ROOT/p) == value for p, value in inputs.items()), 'inputs changed during run')
    artifacts = {str(p.relative_to(ROOT)): digest(p) for p in out.rglob('*') if p.is_file()}
    result = dict(schema=1, status='pass', profile=config['profile'], claim=config['claim'],
                  source_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  dirty_tree=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                  started_utc=started, finished_utc=datetime.now(timezone.utc).isoformat(), versions=versions,
                  inputs_sha256=inputs, artifacts_sha256=artifacts, assumptions=config['assumptions'],
                  environment=config['environment'], jobs=jobs, formal_readiness_accepted=False)
    receipt.write_text(json.dumps(result, indent=2)+'\n')
    if load:
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive = ROOT/f'evidence/{args.profile}/{run_id}.tar.gz'
        archive.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, 'w:gz') as bundle:
            for path, expected in sorted({**inputs, **artifacts}.items()):
                require(digest(ROOT/path) == expected, f'artifact changed before retention: {path}')
                bundle.add(ROOT/path, arcname=path, recursive=False)
            bundle.add(receipt, arcname='manifest.json', recursive=False)
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text(json.dumps(dict(schema=1, status='pass', profile=config['profile'],
            claim=config['claim'], archive=str(archive.relative_to(ROOT)), archive_sha256=digest(archive),
            manifest_sha256=digest(receipt), inputs_sha256=inputs), indent=2)+'\n')
    print(f'{title} smoke: PASS; receipt {receipt.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
