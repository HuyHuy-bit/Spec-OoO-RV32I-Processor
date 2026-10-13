#!/usr/bin/env python3
"""Verify cache storage arrangements and measure native and physical implementations."""
import argparse
from collections import Counter
from fnmatch import fnmatchcase
import json
import hashlib
import math
from pathlib import Path
import random
import re
import os
import signal
import shutil
import subprocess
import time
import tarfile

from fetch_a1_timing import DEPS, verify
from run_a1_probe import ROOT, digest, verify_synthesis_suite
from run_a1_timing import analyze, sdc

TOP = "cache_storage_pair_probe"
OUT = ROOT / "out/storage_pilot"
SOURCES = ["rtl/memory/dense_memory_1rw_array.sv", "rtl/memory/dense_memory_1rw.sv",
           "synth/experiments/cache_storage_pair_probe.sv"]
BENCH = "verif/unit/cache_storage_pair_probe_tb.sv"
INPUTS = [*SOURCES, BENCH, "Makefile", "config/storage_pilot.json", "config/dense_memory.json",
          "config/a1_timing.lock", "config/synthesis.lock", "config/toolchain.lock",
          "tools/run_storage_pilot.py", "tools/run_a1_timing.py", "tools/run_a1_probe.py",
          "tools/fetch_a1_timing.py", "synth/a1_timing.tcl"]


def run(command, directory, name, timeout=900):
    start = time.monotonic()
    metrics = directory / (name + ".resources")
    process = subprocess.Popen(["/usr/bin/time", "-f", "%M", "-o", str(metrics), *map(str, command)],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True, env=dict(os.environ, CCACHE_DISABLE='1'))
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        (directory / (name + '.log')).write_text(output)
        raise RuntimeError(f'{name} timed out in {directory}') from None
    (directory / (name + ".log")).write_text(output)
    if process.returncode:
        raise RuntimeError(f"{name} failed in {directory}:\n{output[-3000:]}")
    return {"command": list(map(str, command)), "returncode": process.returncode,
            "elapsed_seconds": time.monotonic() - start, "peak_rss_kib": int(metrics.read_text().strip()),
            "log": str((directory / (name + '.log')).relative_to(ROOT))}


class Storage:
    def __init__(self, capacity, ways, chunk, dirty):
        self.ways, self.chunk, self.dirty = ways, chunk, dirty
        self.way_bytes = capacity // ways
        self.sets = self.way_bytes // 32
        self.data = [bytearray(self.way_bytes) for _ in range(ways)]
        self.tags = [[0] * self.sets for _ in range(ways)]
        self.reset()

    def reset(self):
        self.valid = [[False] * self.sets for _ in range(self.ways)]
        self.dirt = [[False] * self.sets for _ in range(self.ways)]
        self.lru = [0] * self.sets

    def cycle(self, cmd, reset):
        en, write, touch, way, addr, data, mask, valid, dirty = cmd
        index = (addr // 32) % self.sets
        offset = (addr % self.way_bytes) // self.chunk * self.chunk
        tag = addr // self.way_bytes
        result = (0, 0, 0, 0, 0, 0)
        if reset:
            self.reset()
            return result
        if en and not write:
            hits = [w for w in range(self.ways) if self.valid[w][index] and self.tags[w][index] == tag]
            invalid = [w for w in range(self.ways) if not self.valid[w][index]]
            selected = (hits or invalid or [self.lru[index]])[0]
            present = self.valid[selected][index]
            value = int.from_bytes(self.data[selected][offset:offset+self.chunk], "little") if present else 0
            victim = (self.tags[selected][index] * self.sets + index) * 32 if present else 0
            result = (1, int(bool(hits)), int(present and self.dirt[selected][index]), selected, victim, value)
        if en and write:
            self.tags[way][index] = tag
            self.valid[way][index] = bool(valid)
            self.dirt[way][index] = bool(dirty and self.dirty)
            for b in range(self.chunk):
                if mask >> b & 1:
                    self.data[way][offset+b] = data >> (8*b) & 255
        if touch and self.ways == 2:
            self.lru[index] = 1 - way
        return result


def vectors(candidate, seed, random_cycles):
    rng = random.Random(seed)
    ways, chunk = candidate['ways'], 4 * candidate['bank_words']
    models = [Storage(candidate[k], ways, chunk, c == 1) for c, k in enumerate(('i_bytes', 'd_bytes'))]
    idle = (0,) * 9
    yield True, [idle, idle]
    for way in range(ways):
        for row in range(max(m.way_bytes for m in models) // chunk):
            commands = []
            for c, m in enumerate(models):
                address = (way+1) * m.way_bytes + row * chunk
                commands.append((1, 1, 1, way, address, rng.getrandbits(chunk*8), (1 << chunk)-1, 1, c)
                                if row * chunk < m.way_bytes else idle)
            yield False, commands
    for way in range(ways):
        for row in range(max(m.way_bytes for m in models) // chunk):
            yield False, [(1, 0, 0, 0, (way+1)*m.way_bytes + row*chunk, 0, 0, 0, 0)
                          if row*chunk < m.way_bytes else idle for m in models]
    yield True, [idle, idle]
    for write in (0, 1, 0):
        yield False, [(1, write, 0, 0, m.way_bytes, 0, 0, 1, 1) for m in models]
    for cycle in range(random_cycles):
        commands = []
        for m in models:
            commands.append((int(rng.random() < .85), rng.randrange(2), rng.randrange(2), rng.randrange(ways),
                             rng.randrange(1, 5)*m.way_bytes + rng.randrange(m.way_bytes),
                             rng.getrandbits(chunk*8), rng.choice([0, (1 << chunk)-1, rng.getrandbits(chunk)]),
                             int(rng.random() < .8), rng.randrange(2)))
        yield cycle in (30, 1000), commands


def simulation(candidate, config, directory, sources=SOURCES):
    params = {k.upper(): candidate[k] for k in ('i_bytes', 'd_bytes', 'ways', 'bank_words')}
    command = ['verilator', '--binary', '--timing', '--assert', '--Wall', '-Wno-UNUSEDSIGNAL', '-Wno-DECLFILENAME',
               '--build-jobs', '2', '--top-module', TOP+'_tb', '--Mdir', directory/'obj',
               *(f'-G{k}={v}' for k, v in params.items()), *sources, BENCH]
    commands = [run(command, directory, 'build')]
    results = []
    for seed in config['seeds']:
        chunk = candidate['bank_words'] * 4
        widths = [1, 1, 1, 1, 32, chunk*8, chunk, 1, 1]
        models = [Storage(candidate[k], candidate['ways'], chunk, c == 1)
                  for c, k in enumerate(('i_bytes', 'd_bytes'))]
        expected, rows = [], []
        for reset, pair in vectors(candidate, seed, config['random_cycles']):
            packed = [pair[0][i] | pair[1][i] << width
                      for i, width in enumerate(widths)]
            rows.append(' '.join(f'{v:x}' for v in [int(reset), *packed]))
            values = [m.cycle(cmd, reset) for m, cmd in zip(models, pair)]
            expected.append([values[0][i] | values[1][i] << width
                             for i, width in enumerate([1, 1, 1, 1, 32, chunk*8])])
        (directory/'input.txt').write_text('\n'.join(rows)+'\n')
        commands.append(run([directory/'obj'/('V'+TOP+'_tb'), f'+input={directory}/input.txt',
                             f'+output={directory}/output.txt'], directory, f'seed_{seed}'))
        actual = [[int(v, 16) for v in line.split()] for line in (directory/'output.txt').read_text().splitlines()]
        if len(actual) != len(expected):
            raise RuntimeError('storage replay did not complete')
        for cycle, (got, want) in enumerate(zip(actual, expected)):
            if got != want:
                raise RuntimeError(f'STORAGE_MISMATCH {candidate["name"]} seed {seed} cycle {cycle}: {got} != {want}')
        results.append({'seed': seed, 'cycles': len(expected)})
    return {'status': 'pass', 'runs': results, 'commands': commands}


def storage_bits(c):
    result = {}
    for key in ('i_bytes', 'd_bytes'):
        sets = c[key] // (32*c['ways'])
        tag = 27 - (sets.bit_length()-1)
        controls = sets * (c['ways'] * (2 if key == 'd_bytes' else 1) + (c['ways'] == 2))
        result[key[0]] = {'payload_bits': c[key]*8, 'tag_bits': sets*c['ways']*tag, 'control_bits': controls,
                         'data_leaf_depth': max(16, sets*8//c['bank_words']), 'tag_leaf_depth': max(16, sets),
                         'data_leaf_width': 128 if c['bank_words'] == 8 else 32,
                         'data_leaves': c['ways']*(2 if c['bank_words'] == 8 else c['bank_words']),
                         'tag_leaf_width': tag, 'tag_leaves': c['ways']}
    return result


def synthesize(candidate, config, directory, yosys, lock):
    c = config['timing']
    liberty = (DEPS / lock['library']['path']).relative_to(ROOT)
    dont_use = ' '.join(f'-dont_use {x}' for x in c['dont_use'])
    (directory/'abc.constr').write_text(f'set_driving_cell {c["abc_driver"]}\nset_load {c["abc_load_ff"]}\n')
    params = ' '.join(f'-G {k.upper()}={candidate[k]}' for k in ('i_bytes', 'd_bytes', 'ways', 'bank_words'))
    script = (f'read_slang --top {TOP} {params} -D SYNTHESIS '+ ' '.join(SOURCES) + '\n'
              f'synth -top {TOP} -flatten -noabc\ndfflibmap -liberty {liberty} {dont_use}\n'
              f'abc -liberty {liberty} {dont_use} -constr {directory}/abc.constr -D {c["period_ns"]*1000}\n'
              f'clean -purge\nread_liberty -lib {liberty}\ncheck -assert\nstat -liberty {liberty}\n'
              f'write_json {directory}/mapped.json\nwrite_verilog -noattr -noexpr -simple-lhs {directory}/mapped.v\n')
    (directory/'synth.ys').write_text(script)
    command = run([yosys, '-Q', '-T', '-m', 'slang', '-s', directory/'synth.ys'], directory, 'synthesis')
    netlist = json.loads((directory/'mapped.json').read_text())
    counts = Counter(cell['type'] for cell in netlist['modules'][TOP]['cells'].values())
    if any(not cell.startswith('sky130_fd_sc_hd__') or '__dl' in cell
           or any(fnmatchcase(cell, x) for x in c['dont_use']) for cell in counts):
        raise RuntimeError('unmapped, latch or excluded storage cell')
    area = re.search(r'Chip area for (?:top )?module.*?:\s*([\d.]+)', (directory/'synthesis.log').read_text())
    if not area:
        raise RuntimeError('missing mapped area')
    bits = sum(n for cell, n in counts.items() if cell.startswith(('sky130_fd_sc_hd__df', 'sky130_fd_sc_hd__edf')))
    ports = netlist['modules'][TOP]['ports']
    constants = tuple(name + (f'[{i}]' if len(port['bits']) > 1 else '')
                      for name, port in ports.items() if port['direction'] == 'output'
                      for i, bit in enumerate(port['bits']) if bit in ('0', '1'))
    timing = analyze(str(DEPS/lock['opensta']['executable']), liberty,
                     (directory/'mapped.v').relative_to(ROOT), TOP, directory/'timing', sdc(c, True), constants)
    if candidate['name'] == 'small_direct_parallel':
        missing = '\n'.join(line for line in sdc(c, True).splitlines() if not line.startswith('set_output_delay'))+'\n'
        try:
            analyze(str(DEPS/lock['opensta']['executable']), liberty, (directory/'mapped.v').relative_to(ROOT),
                    TOP, directory/'missing_output_delay', missing, constants)
        except RuntimeError as error:
            if 'incomplete timing constraints' not in str(error):
                raise
        else:
            raise RuntimeError('missing-output-delay control was accepted')
        timing['missing_output_delay_rejected'] = True
    return {'status': 'pass', 'cell_area_um2': float(area[1]), 'sequential_bits': bits,
            'cell_counts': dict(counts), 'timing': timing, 'command': command}


def mutations(config):
    candidate = next(c for c in config['candidates'] if c['name'] == 'balanced_interleaved')
    changes = {
        'mask_ignored': (0, 'if (wmask_i[lane])', "if (1'b1)"),
        'row_alias': (0, 'array_q[addr_i]', 'array_q[addr_i >> 1]'),
        'invalid_reset': (2, 'valid_q[s] <= 0;', "valid_q[s] <= '1;"),
        'replacement_reversed': (2, 'lru_q[index] <= !way_i;', 'lru_q[index] <= way_i;'),
        'tag_ignored': (2, 'tags[w] == read_tag_q', "1'b1"),
        'dirty_inverted': (2, 'HAS_DIRTY && dirty_i', 'HAS_DIRTY && !dirty_i'),
    }
    results = {}
    for name, (index, old, new) in changes.items():
        directory = OUT/'mutations'/name
        directory.mkdir(parents=True, exist_ok=True)
        source = (ROOT/SOURCES[index]).read_text()
        if source.count(old) != (2 if name == 'row_alias' else 1):
            raise RuntimeError('mutation anchor changed')
        changed = directory/Path(SOURCES[index]).name
        changed.write_text(source.replace(old, new))
        sources = [str(changed) if i == index else s for i, s in enumerate(SOURCES)]
        try:
            simulation(candidate, {**config, 'seeds': [1]}, directory, sources)
        except RuntimeError as error:
            if not str(error).startswith('STORAGE_MISMATCH '):
                raise
            results[name] = str(error)
        else:
            raise RuntimeError('undetected mutation: '+name)
        print(name+': mutation detected', flush=True)
    return results


def physical_preflight(flow, pdk_store):
    if (flow['schema'] != 1 or not re.fullmatch(r'ghcr.io/librelane/librelane@sha256:[0-9a-f]{64}', flow['image'])
            or flow['pdk'] != 'sky130A' or flow['standard_cell_library'] != 'sky130_fd_sc_hd'):
        raise ValueError('Unsupported physical toolchain')
    pdk_root = pdk_store/'ciel/sky130/versions'/flow['pdk_revision']
    if not (pdk_root/flow['pdk']/'libs.tech/openlane/config.tcl').exists():
        raise RuntimeError('Pinned PDK is missing from '+str(pdk_root))
    image = flow['image']
    if subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise RuntimeError('Docker is unavailable. Start Docker Desktop and enable this distribution in WSL Integration.')
    subprocess.run(['docker', 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
    for command, expected in [(['librelane', '--bare-version'], flow['librelane']),
                              (['openroad', '-version'], flow['openroad_commit'])]:
        actual = subprocess.check_output(['docker', 'run', '--rm', image, *command], text=True).strip()
        if actual != expected:
            raise RuntimeError('Physical tool version differs: '+actual)
    return pdk_root


def timing_measurements(stage, corner):
    coverage = (stage/'checks.rpt').read_text().split('check_setup -verbose', 1)
    if len(coverage) != 2 or re.sub(r'[=\s]', '', coverage[1].split('\n', 1)[1]):
        raise RuntimeError('Physical timing coverage is incomplete')
    fresh = json.loads((stage/'or_metrics_out.json').read_text())
    reported = {k.split('__corner:', 1)[1] for k in fresh if '__corner:' in k}
    if reported != {corner}:
        raise RuntimeError('Fresh timing metrics do not match the requested corner')
    violations = ['design__max_slew_violation__count', 'design__max_cap_violation__count',
                  'design__max_fanout_violation__count', 'timing__setup_vio__count', 'timing__hold_vio__count']
    slacks = ['timing__setup__ws', 'timing__hold__ws']
    values = {k: fresh[k+'__corner:'+corner] for k in violations + slacks}
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) < 1e20 for v in values.values()):
        raise RuntimeError('Timing metrics contain invalid or missing-path values')
    passed = all(values[k] == 0 for k in violations) and all(values[k] >= 0 for k in slacks)
    return {'fresh_timing_metrics': fresh, 'timing_corner': corner,
            'reported_checks_passed': passed, 'timing_coverage_checked': True}


def physical_measurements(run_directory):
    states = sorted(run_directory.glob('*/state_out.json'), key=lambda p: int(p.parent.name.split('-')[0]))
    if not states or not states[-1].parent.name.endswith('openroad-stamidpnr-3'):
        raise RuntimeError('Physical flow did not reach final timing analysis')
    if len(states) < 2 or not states[-2].parent.name.endswith('openroad-repairantennas-1'):
        raise RuntimeError('Final antenna repair/check must precede timing analysis')
    if json.loads(states[-2].read_text())['odb'] != json.loads(states[-1].read_text())['odb']:
        raise RuntimeError('Antenna and timing checks use different physical databases')
    metrics = json.loads(states[-1].read_text())['metrics']
    timing = timing_measurements(states[-1].parent, 'nom_tt_025C_1v80')
    timing['reported_checks_passed'] &= metrics['route__antenna_violation__count'] == 0
    return {'metrics': metrics, **timing, 'final_state': str(states[-1].relative_to(ROOT))}


def physical(candidate, config, directory, flow, pdk_root):
    image = flow['image']
    constraints = '\n'.join(line for line in sdc(config['timing'], True).splitlines()
                            if not line.startswith('set_wire_load'))+'\n'
    (directory/'constraints.sdc').write_text(constraints)
    params = [f'{k.upper()}={candidate[k]}' for k in ('i_bytes', 'd_bytes', 'ways', 'bank_words')]
    settings = {**flow['flow'], 'DESIGN_NAME': TOP, 'VERILOG_FILES': [str(ROOT/s) for s in SOURCES],
                'SYNTH_PARAMETERS': params, 'CLOCK_PORT': 'clk_i', 'CLOCK_PERIOD': config['timing']['period_ns'],
                'PNR_SDC_FILE': str(directory/'constraints.sdc'),
                'SIGNOFF_SDC_FILE': str(directory/'constraints.sdc'),
                'PDK': flow['pdk'], 'STD_CELL_LIBRARY': flow['standard_cell_library']}
    (directory/'config.json').write_text(json.dumps(settings, indent=2)+'\n')
    container_name = 'storage-'+hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    command = ['docker', 'run', '--rm', '--name', container_name,
               '--user', f'{os.getuid()}:{os.getgid()}', '--cpus', str(flow['jobs']),
               '--memory', f"{flow['memory_gib']}g", '--memory-swap', f"{flow['memory_with_swap_gib']}g",
               '-v', f'{ROOT}:{ROOT}', '-v', f'{pdk_root}:/pdk:ro', '-w', str(ROOT),
               '-e', f'PILOT_MEMORY_FILE={directory}/memory_peak_bytes', image,
               'sh', '-c', 'librelane "$@"; result=$?; cat /sys/fs/cgroup/memory.peak > "$PILOT_MEMORY_FILE"; exit "$result"',
               'storage-pilot', str(directory/'config.json'), '--manual-pdk', '--pdk-root', '/pdk',
               '--run-tag', 'physical', '--to', flow['stop_after'], '--jobs', str(flow['jobs']), '--condensed']
    try:
        executed = run(command, directory, 'physical', timeout=3600)
    finally:
        subprocess.run(['docker', 'rm', '-f', container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'status': 'completed', 'scope': 'Nominal TT global-route estimates; no detailed routing, extraction, DRC or LVS.',
            'toolchain': flow, 'command': executed,
            **physical_measurements(directory/'runs/physical'),
            'container_peak_memory_bytes': int((directory/'memory_peak_bytes').read_text()),
            'geometry_approved': False}


def verify_corner_rc(log, corner, expected):
    applied = {'layers': {}, 'vias': {}}
    for line in log.splitlines():
        fields = line.split()
        if fields[:2] != ['+', 'set_layer_rc']:
            continue
        args = dict(zip(fields[2::2], fields[3::2]))
        if args.get('-corner') != corner:
            raise RuntimeError('RC override applied to the wrong corner')
        if '-layer' in args:
            applied['layers'][args['-layer']] = {'res': float(args['-resistance']), 'cap': float(args['-capacitance'])}
        elif '-via' in args:
            applied['vias'][args['-via']] = {'res': float(args['-resistance'])}
    for kind in ('layers', 'vias'):
        if set(applied[kind]) != set(expected[kind]):
            raise RuntimeError('Missing '+kind+' RC overrides')
        for name, values in expected[kind].items():
            if any(not math.isclose(applied[kind][name][k], v, rel_tol=1e-9, abs_tol=1e-15) for k, v in values.items()):
                raise RuntimeError('Applied RC differs from technology export')


def routed_corner_study(source, flow, pdk_root, selected):
    prior = json.loads(source.read_text())
    candidates = [c for c in prior['candidates'] if not selected or c['candidate']['name'] in selected]
    if not candidates or (selected and set(selected) != {c['candidate']['name'] for c in candidates}):
        raise ValueError('Requested candidates are missing from the physical summary')
    corners = flow['analysis_corners']
    supported = {r+'_'+p for r in ('nom', 'min', 'max')
                 for p in ('tt_025C_1v80', 'ss_100C_1v60', 'ff_n40C_1v95')}
    if set(corners) != supported or len(corners) != len(supported):
        raise ValueError('Expected all nine storage timing corners exactly once')
    hashes = {p: digest(ROOT/p) for p in [*INPUTS, 'config/storage_physical.json', 'tests/test_unit_runner.py']}
    source_hash = digest(source)
    run_id = hashlib.sha256(json.dumps([hashes, source_hash, selected], sort_keys=True).encode()).hexdigest()[:16]
    directory = OUT/('corners-'+run_id)
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source, directory/'source_summary.json')
    docker = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
              '--cpus', str(flow['jobs']), '--memory', f"{flow['memory_gib']}g",
              '--memory-swap', f"{flow['memory_with_swap_gib']}g", '-v', f'{ROOT}:{ROOT}',
              '-v', f'{pdk_root}:/pdk:ro', '-w', str(ROOT), flow['image']]
    def execute(command, work, label):
        container = 'storage-sta-'+hashlib.sha256(str(work/label).encode()).hexdigest()[:16]
        try:
            return run(docker[:3]+['--name', container]+docker[3:]+command, work, label)
        finally:
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rc = {}
    collateral = {}
    for interconnect in ('nom', 'min', 'max'):
        tech = f"{flow['pdk']}/libs.ref/{flow['standard_cell_library']}/techlef/{flow['standard_cell_library']}__{interconnect}.tlef"
        collateral[tech] = digest(pdk_root/tech)
        script = directory/(interconnect+'_rc.tcl')
        script.write_text('read_lef {/pdk/'+tech+'}\n'+'''set_cmd_units -time ns -capacitance pF -resistance kOhm -distance um
foreach layer [[ord::get_db_tech] getLayers] {
    if {[$layer getType] eq "ROUTING"} {
        lassign [est::dblayer_wire_rc $layer] r c
        puts "PILOT_LAYER [$layer getName] [expr {$r * [sta::unit_scale distance] / [sta::unit_scale resistance]}] [expr {$c * [sta::unit_scale distance] / [sta::unit_scale capacitance]}]"
    } elseif {[$layer getType] eq "CUT"} {
        puts "PILOT_VIA [$layer getName] [expr {[$layer getResistance] / [sta::unit_scale resistance]}]"
    }
}
''')
        execute(['openroad', '-exit', str(script)], directory, interconnect+'_rc')
        layers, vias = {}, {}
        for line in (directory/(interconnect+'_rc.log')).read_text().splitlines():
            fields = line.split()
            if fields[:1] == ['PILOT_LAYER']:
                layers[fields[1]] = dict(res=float(fields[2]), cap=float(fields[3]))
            elif fields[:1] == ['PILOT_VIA']:
                vias[fields[1]] = dict(res=float(fields[2]))
        if set(layers) != {'li1', 'met1', 'met2', 'met3', 'met4', 'met5'} or len(vias) != 6:
            raise RuntimeError('Incomplete technology RC export')
        rc[interconnect] = {'layers': layers, 'vias': vias}
    (directory/'rc.json').write_text(json.dumps(rc, indent=2)+'\n')
    results = []
    for candidate in candidates:
        name = candidate['candidate']['name']
        if not re.fullmatch('[a-z][a-z0-9_]*', name):
            raise ValueError('Invalid candidate name')
        old = candidate['physical']
        for key in ('image', 'pdk_revision', 'pdk', 'standard_cell_library', 'openroad_commit'):
            if old['toolchain'][key] != flow[key]:
                raise RuntimeError('Saved physical toolchain differs: '+key)
        for p in [*SOURCES, 'config/storage_pilot.json', 'config/dense_memory.json']:
            if candidate['inputs_sha256'][p] != hashes[p]:
                raise RuntimeError('Saved physical design differs from current inputs: '+p)
        state_path = ROOT/old['final_state']
        state = json.loads(state_path.read_text())
        retained = [state_path, Path(state['odb']), Path(state['sdc']), state_path.parent/'config.json']
        for p in retained:
            if digest(p) != candidate['artifacts_sha256'][str(p.relative_to(ROOT))]:
                raise RuntimeError('Saved physical artifact hash mismatch: '+str(p))
        settings = json.loads((state_path.parent/'config.json').read_text())
        target = directory/name
        target.mkdir()
        snapshot = {}
        for kind in ('odb', 'sdc'):
            dest = target/('input.'+kind)
            shutil.copy2(state[kind], dest)
            snapshot[kind] = str(dest)
        snapshot['metrics'] = {}
        initial = target/'state_in.json'
        initial.write_text(json.dumps(snapshot, indent=2)+'\n')
        registers = state['metrics']['design__instance__count__class:sequential_cell']
        audit = target/'audit.tcl'
        audit.write_text('''if {[llength [all_clocks]] != 1} {error "unexpected clock count"}
set clock [get_clocks core]
if {[get_property $clock is_virtual] || ![get_property $clock is_propagated]} {error "clock is not propagated"}
if {![grt::have_routes]} {error "missing global routes"}
set count [llength [all_registers -clock core]]
'''+f'if {{$count != {registers}}} {{error "unexpected register count"}}\n'+
                         f'if {{abs([get_property $clock period] - {settings["CLOCK_PERIOD"]}) > 0.000001}} {{error "clock period differs"}}\n'+
                         'write_metric_int "pilot__clocked_registers__corner:$corner_name" $count\n')
        for corner in corners:
            cell_pvt = corner.split('_', 1)[1]
            liberty = f"{flow['pdk']}/libs.ref/{flow['standard_cell_library']}/lib/{flow['standard_cell_library']}__{cell_pvt}.lib"
            collateral[liberty] = digest(pdk_root/liberty)
            interconnect = corner.split('_', 1)[0]
            work = target/corner
            work.mkdir()
            config = {**settings, 'meta': {'version': 2, 'flow': ['OpenROAD.STAMidPNR']},
                      'DESIGN_DIR': str(work),
                      'DEFAULT_CORNER': corner, 'PNR_CORNERS': [corner], 'STA_CORNERS': [corner],
                      'LAYERS_RC': {corner: rc[interconnect]['layers']},
                      'VIAS_R': {'*': rc[interconnect]['vias']}, 'SET_RC_VERBOSE': True,
                      'STA_EXTRA_CORNER_TCL_FILE': str(audit),
                      'PNR_SDC_FILE': snapshot['sdc']}
            config.pop('SIGNOFF_SDC_FILE', None)
            config_path = work/'config.json'
            config_path.write_text(json.dumps(config, indent=2)+'\n')
            command = execute(['librelane', str(config_path), '--manual-pdk', '--pdk-root', '/pdk',
                                 '--run-tag', 'timing', '--with-initial-state', str(initial),
                                 '--jobs', str(flow['jobs']), '--condensed'], work, 'timing')
            stage, = (work/'runs/timing').glob('*/state_out.json')
            verify_corner_rc((stage.parent/'openroad-stamidpnr.log').read_text(), corner, rc[interconnect])
            final = json.loads(stage.read_text())
            if any(final[k] != snapshot[k] for k in ('odb', 'sdc')):
                raise RuntimeError('Timing analysis changed its physical inputs')
            measured = timing_measurements(stage.parent, corner)
            if measured['fresh_timing_metrics'].get('pilot__clocked_registers__corner:'+corner) != registers:
                raise RuntimeError('Physical clock audit is missing')
            result = {'candidate': name, **measured, 'command': command}
            (work/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
            results.append(result)
            print(name+' '+corner+': '+('PASS' if measured['reported_checks_passed'] else 'VIOLATIONS'), flush=True)
        if any(digest(Path(snapshot[k])) != candidate['artifacts_sha256'][str(Path(state[k]).relative_to(ROOT))]
               for k in ('odb', 'sdc')):
            raise RuntimeError('Physical inputs changed during timing analysis')
    if digest(source) != source_hash or any(digest(ROOT/p) != h for p, h in hashes.items()):
        raise RuntimeError('Study inputs changed during analysis')
    if any(digest(pdk_root/p) != h for p, h in collateral.items()):
        raise RuntimeError('PDK collateral changed during analysis')
    passed = all(r['reported_checks_passed'] for r in results)
    summary = {'run_id': run_id, 'inputs_sha256': hashes, 'source_summary_sha256': source_hash,
               'pdk_files_sha256': collateral, 'toolchain': flow, 'corners': corners, 'results': results,
               'reported_checks_passed': passed, 'geometry_approved': False,
               'scope': 'Fresh per-corner global-route parasitic estimates on unchanged routed storage shells; no extracted signoff.'}
    summary['artifacts_sha256'] = {str(p.relative_to(ROOT)): digest(p) for p in directory.rglob('*') if p.is_file()}
    (directory/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    archive = ROOT/'evidence/storage_pilot'/('corners-'+run_id+'.tar.gz')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
        for path in [*(ROOT/p for p in hashes), *(p for p in directory.rglob('*') if p.is_file())]:
            tar.add(path, arcname=str(path.relative_to(ROOT)))
    archive.with_suffix('.json').write_text(json.dumps({'archive_sha256': digest(archive),
        'summary_sha256': digest(directory/'summary.json'), 'reported_checks_passed': passed}, indent=2)+'\n')
    print('Retained '+str(archive.relative_to(ROOT)), flush=True)
    if not passed:
        raise SystemExit('Timing study completed with violations; see retained per-corner reports')


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path.home()/'tools/oss-cad-suite-20260905/oss-cad-suite')
    parser.add_argument('--candidate', action='append')
    parser.add_argument('--synth', action='store_true')
    parser.add_argument('--mutations', action='store_true')
    parser.add_argument('--physical', action='store_true')
    parser.add_argument('--physical-sta-from', type=Path, help='Analyze all corners of a retained physical summary')
    parser.add_argument('--pdk-store', type=Path, default=Path.home()/'.ciel')
    args = parser.parse_args()
    if args.physical_sta_from:
        if args.physical or args.synth or args.mutations:
            parser.error('--physical-sta-from cannot be combined with other flow modes')
        flow = json.loads((ROOT/'config/storage_physical.json').read_text())
        pdk_root = physical_preflight(flow, args.pdk_store.resolve())
        routed_corner_study(args.physical_sta_from.resolve(), flow, pdk_root, args.candidate)
        return
    if args.physical and (args.synth or args.mutations):
        parser.error('--physical is a separate flow; do not combine it with --synth or --mutations')
    config = json.loads((ROOT/'config/storage_pilot.json').read_text())
    flow = json.loads((ROOT/'config/storage_physical.json').read_text()) if args.physical else None
    if args.physical:
        INPUTS.append('config/storage_physical.json')
        if not args.candidate:
            args.candidate = flow['candidates']
    if config['schema'] != 1 or config['line_bytes'] != 32 or not config['seeds'] or config['random_cycles'] < 2000:
        raise ValueError('unsupported storage pilot contract')
    for c in config['candidates']:
        if (not re.fullmatch('[a-z][a-z0-9_]*', c['name']) or c['ways'] not in (1, 2)
                or c['bank_words'] not in (1, 2, 8) or any(c[k] not in (512, 1024, 4096) for k in ('i_bytes', 'd_bytes'))):
            raise ValueError('unsupported candidate')
    selected = [c for c in config['candidates'] if not args.candidate or c['name'] in args.candidate]
    if args.candidate and set(args.candidate) - {c['name'] for c in selected}:
        parser.error('unknown candidate')
    pdk_root = physical_preflight(flow, args.pdk_store.resolve()) if args.physical else None
    lock = verify() if args.synth else None
    yosys = verify_synthesis_suite(args.suite.resolve()) if args.synth else None
    hashes = {p: digest(ROOT/p) for p in INPUTS}
    run_id = hashlib.sha256(json.dumps({'inputs': hashes, 'candidates': selected,
        'synth': args.synth, 'mutations': args.mutations, 'physical': args.physical}, sort_keys=True).encode()).hexdigest()[:16]
    OUT = OUT/run_id
    version = subprocess.check_output(['verilator', '--version'], text=True).strip()
    tools = json.loads((ROOT/'config/toolchain.lock').read_text())['tools']
    if version != next(t['expected_first_line'] for t in tools if t['name'] == 'verilator'):
        raise RuntimeError('Verilator version differs')
    if args.synth and subprocess.check_output([DEPS/lock['opensta']['executable'], '-version'], text=True).strip() != lock['opensta']['version']:
        raise RuntimeError('OpenSTA version differs')
    for candidate in selected:
        directory = OUT/candidate['name']
        directory.mkdir(parents=True, exist_ok=True)
        receipt = directory/'receipt.json'
        receipt.unlink(missing_ok=True)
        result = {'candidate': candidate, 'inputs_sha256': hashes, 'scope': config['scope'],
                  'storage': storage_bits(candidate), 'line_transfer_beats': 8//candidate['bank_words'],
                  'verilator': version, 'physical': {'status': 'not_run', 'reason': 'Native pilot performs no placement or routing'},
                  'geometry_approved': False}
        result['simulation'] = simulation(candidate, config, directory)
        print(candidate['name']+': simulation PASS', flush=True)
        if args.synth:
            result['library'] = lock['library']
            result['mapping'] = synthesize(candidate, config, directory, yosys, lock)
            print(candidate['name']+': mapping/STA PASS', flush=True)
        if args.physical:
            result['physical'] = physical(candidate, config, directory, flow, pdk_root)
            print(candidate['name']+': physical flow completed', flush=True)
        if any(digest(ROOT/p) != h for p, h in hashes.items()):
            raise RuntimeError('source changed during pilot')
        result['artifacts_sha256'] = {str(p.relative_to(ROOT)): digest(p) for p in directory.rglob('*')
                                     if p.is_file() and 'obj' not in p.parts}
        receipt.write_text(json.dumps(result, indent=2)+'\n')
    if args.mutations:
        receipt = OUT/'mutations.json'
        receipt.unlink(missing_ok=True)
        results = mutations(config)
        if any(digest(ROOT/p) != h for p, h in hashes.items()):
            raise RuntimeError('source changed during controls')
        receipt.write_text(json.dumps({'inputs_sha256': hashes, 'detected': results}, indent=2)+'\n')
    receipts = [OUT/c['name']/'receipt.json' for c in selected]
    (OUT/'summary.json').write_text(json.dumps({'run_id': run_id, 'inputs_sha256': hashes,
        'candidates': [json.loads(p.read_text()) for p in receipts],
        'mutations': json.loads((OUT/'mutations.json').read_text()) if args.mutations else None,
        'physical_status': 'completed' if args.physical else 'not_run', 'geometry_approved': False}, indent=2)+'\n')
    if any(digest(ROOT/p) != h for p, h in hashes.items()):
        raise RuntimeError('source changed before retention')
    archive = ROOT/'evidence/storage_pilot'/f'{run_id}.tar.gz'
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'w:gz') as tar:
        for path in [*(ROOT/p for p in INPUTS), *(p for p in OUT.rglob('*') if p.is_file() and 'obj' not in p.parts)]:
            tar.add(path, arcname=str(path.relative_to(ROOT)))
    archive.with_suffix('.json').write_text(json.dumps({'archive_sha256': digest(archive),
        'inputs_sha256': hashes, 'physical_status': 'completed' if args.physical else 'not_run'}, indent=2)+'\n')
    print('Retained '+str(archive.relative_to(ROOT)), flush=True)


if __name__ == '__main__':
    main()
