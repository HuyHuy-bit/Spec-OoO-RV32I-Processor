#!/usr/bin/env python3
"""Compare RTL architectural events with the pinned Sail or Spike model under stalls/reset."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model.iss.sail_log import parse_sail_log
from model.iss.spike_log import relocate, spike_sail_trace
from tools.act4_tools import SAIL
from tools.check_act4 import check_harness, check_sail
from tools.run_single_lane import OUT as CORE_OUT, build_core, check_trace, make_header
from tools.run_lockstep_smoke import verify_spike
from verif.core.differential_programs import program
from verif.core.programs import i, csr, constant
from verif.core.reference import unpack_event
from verif.lockstep.comparator import compare_traces, ComparisonError


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def packets(events):
    result = copy.deepcopy(events)
    for n,event in enumerate(result): event['order'] = n
    return [{'slots':[event,{}]} for event in result]


def compare_output(output, expected, boot):
    segments = []
    for line in output.splitlines():
        if line == 'RESET': segments.append([])
        elif line.startswith('EVENT '):
            if not segments: raise ValueError('event before reset')
            segments[-1].append(unpack_event(line.split()[3:]))
    if not segments or not segments[-1] or 'CORE PASS' not in output: raise ValueError('incomplete RTL run')
    total = 0
    for segment in segments:
        for n,event in enumerate(segment[:len(boot)]):
            if event['instruction'] != boot[n] or event['pc_before'] != 4*n:
                raise ValueError('RTL initialization sequence differs')
        if len(segment) > len(expected): raise ValueError('RTL exceeds reference trace')
        actual = segment[len(boot):]
        compare_traces(packets(expected[len(boot):len(segment)]),packets(actual))
        total += len(actual)
    if len(segments[-1]) != len(expected): raise ValueError('short RTL trace')
    return total


COUNTERS = set(range(0xb00,0xba0)) | set(range(0xc00,0xca0))


def fetched_coverage(events, output):
    dual = int(output.split('dual=')[1].split()[0])
    fences = sum(e['instruction'] & 0x707f == 0x0f for e in events if e.get('retired'))
    fence_i = sum(e['instruction'] & 0x707f == 0x100f for e in events if e.get('retired'))
    mret = sum(e['instruction'] == 0x30200073 for e in events if e.get('retired'))
    if not (dual and fences and fence_i >= 4 and mret):
        raise ValueError('required fetched-core witnesses were not reached')
    recycles = int(output.split('recycles=')[1].split()[0])
    return dict(dual_retire_cycles=dual, fences=fences, fence_i=fence_i, mret=mret, identity_recycles=recycles)


def build_fetched(directory=ROOT/'out/sail_fetched_differential/obj_dir', replace=None):
    """Build the MEMORY_SERVICE=1 fetched core with the program-mode driver; mutants run without RTL assertions."""
    config = json.loads((ROOT/'config/memory_core.json').read_text())
    make_header()
    flags = ['--Wall','--assert','--top-module','fetch_execution_core',
             *(f'-G{k}={v}' for k,v in config['parameters'].items()), ROOT/config['lint_config']]
    if replace: flags += ['-DSYNTHESIS','-Wno-UNUSEDSIGNAL','-Wno-UNUSEDPARAM']
    sources = [str(replace[1]) if replace and p == replace[0] else p for p in config['sources']]
    result = subprocess.run(['verilator','--cc','--exe','--build','--build-jobs','2',*map(str,flags),
        '--Mdir',str(directory),'-CFLAGS',f'-std=c++17 -Wall -Wextra -Werror -I{CORE_OUT}',
        *sources,'verif/core/fetched_core_tb.cpp','-o','fetched_core'],
        cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode: raise RuntimeError(result.stdout[-3000:])
    return directory/'fetched_core', config


def comparator_mutations(expected, actual):
    """Each corruption of the real fetched trace must be rejected."""
    def changed(name):
        trace = copy.deepcopy(actual)
        pick = lambda test: next(n for n,e in enumerate(trace) if test(e))
        if name == 'missing_event': del trace[len(trace)//2]
        elif name == 'extra_event': trace.insert(len(trace)//2, copy.deepcopy(trace[len(trace)//2]))
        elif name == 'reversed_lanes':
            n = pick(lambda e: e.get('retired') and e['rd_addr']); trace[n], trace[n+1] = trace[n+1], trace[n]
        elif name == 'gpr_value': trace[pick(lambda e: e['rd_addr'])]['rd_value'] ^= 1
        elif name == 'next_pc': trace[pick(lambda e: e.get('retired'))]['pc_after'] ^= 4
        elif name == 'trap_metadata': trace[pick(lambda e: e.get('trap'))]['trap_value'] ^= 1
        elif name == 'memory_mask': trace[pick(lambda e: e['mem_write_mask'])]['mem_write_mask'] ^= 1
        elif name == 'memory_data': trace[pick(lambda e: e['mem_read_mask'])]['mem_read_data'] ^= 0x100
        elif name == 'csr_effect': trace[pick(lambda e: e['csr_effects'])]['csr_effects'][0]['new_value'] ^= 1
        return trace
    caught = []
    for name in ('missing_event','extra_event','reversed_lanes','gpr_value','next_pc','trap_metadata',
                 'memory_mask','memory_data','csr_effect'):
        try: compare_traces(packets(expected), packets(changed(name)))
        except ComparisonError: caught.append(name)
        else: raise RuntimeError(f'comparator missed {name}')
    return caught


def coverage(events):
    traps = sorted({e['trap_cause'] for e in events if e.get('trap')})
    branches = sorted({(e['instruction'] >> 12) & 7 for e in events if e['instruction'] & 127 == 0x63})
    csr_forms = sorted({(e['instruction'] >> 12) & 7 for e in events if e['instruction'] & 127 == 0x73 and (e['instruction'] >> 12) & 7})
    loads = sorted({(e['instruction'] >> 12) & 7 for e in events if e.get('mem_read_mask')})
    stores = sorted({(e['instruction'] >> 12) & 7 for e in events if e.get('mem_write_mask')})
    if traps != [0,1,2,3,4,5,6,7,11] or branches != [0,1,4,5,6,7] or csr_forms != [1,2,3,5,6,7] or loads != [0,1,2,4,5] or stores != [0,1,2]:
        raise ValueError('required differential scenarios were not reached')
    return dict(trap_causes=traps,branch_forms=branches,csr_forms=csr_forms,load_forms=loads,store_forms=stores)


def napot(base, size):
    if size < 8 or size & (size-1) or base % size: raise ValueError('region is not a NAPOT block')
    return (base >> 2) | ((size >> 3)-1)


def spike_layout(platform, bias):
    """Spike memory map and a locked-PMP setup prefix reproducing the platform device PMAs."""
    regions = platform['memory']['regions']
    bram = next(r for r in regions if r['name'] == 'bram')
    devices = [r for r in regions if r is not bram]
    if any(r['execute'] for r in devices): raise ValueError('Spike PMP prefix assumes non-executable devices')
    start = min(int(r['base'],0) for r in devices)
    span = 1 << (max(int(r['base'],0)+int(r['size'],0) for r in devices)-start-1).bit_length()
    entries = [(napot(int(r['base'],0),int(r['size'],0)), 0x98 | r['write'] << 1 | r['read']) for r in devices]
    entries.append((napot(start,span),0x98))
    if len(entries) > 4: raise ValueError('Spike prefix uses one pmpcfg register')
    prefix = []
    for n,(address,_) in enumerate(entries): prefix += constant(5,address)+[csr(0,5,0x3b0+n,1)]
    prefix += constant(5,sum(cfg << 8*n for n,(_,cfg) in enumerate(entries)))+[csr(0,5,0x3a0,1)]
    prefix += constant(5,bias)+[i(0,5,0,0,0x67)]
    size = int(bram['size'],0)
    memory = f'-m0x{bias:x}:0x{size+0x1000:x},0x{start:x}:0x{max(span,0x1000):x}'
    return prefix, size, memory


def spike_reference(spike, settings, seed, repeat, out, run):
    """Run the relocated image on Spike and return platform-addressed events."""
    platform = json.loads((ROOT/'config/platform.yaml').read_text())
    bias = int(settings['spike_bram_base'],0)
    prefix, size, memory = spike_layout(platform, bias)
    image,_,end_pc = program(seed, True, bias, repeat)
    swaps = {int(k,0):int(v,0) for k,v in settings['spike_substitutions'].items()}
    blob = bytearray(image) + bytearray(size-len(image))
    substitutions = {}
    for n in range(0,len(image),4):
        word = int.from_bytes(blob[n:n+4],'little')
        if word in swaps:
            blob[n:n+4] = swaps[word].to_bytes(4,'little'); substitutions[bias+n] = word
    blob += b''.join(w.to_bytes(4,'little') for w in prefix)
    raw = out/f'{seed}_spike.bin'; raw.write_bytes(blob)
    assembly = out/f'{seed}_spike.S'
    assembly.write_text(f'.text\n.globl _start\n_start:\n.incbin "{raw.relative_to(ROOT)}"\n')
    elf = out/f'{seed}_spike.elf'
    run(['riscv64-unknown-elf-gcc','-march=rv32i','-mabi=ilp32','-nostdlib','-nostartfiles',
         f'-Wl,--no-relax,--build-id=none,-n,-Ttext=0x{bias:x},-e,0x{bias+size:x}',assembly,'-o',elf],f'compile_{seed}_spike.log')
    # Spike's step limit is consumed in scheduler quanta that traps cut short, so stop on the end loop instead.
    process = subprocess.Popen([str(spike),'--isa=rv32i_zicsr_zifencei','--priv=m','--pmpregions=4','--triggers=0',
        '--disable-dtb',f'--pc=0x{bias+size:x}',f'--instructions={settings["spike_step_limit"]}','-l','--log-commits',
        memory,str(elf)],cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    lines = []; ends = 0
    for line in process.stdout:
        lines.append(line)
        if line.startswith('core   0: 3 ') and int(line.split()[3],16) == end_pc:
            ends += 1
            if ends == 2: break
    process.kill(); process.wait()
    log = ''.join(lines); (out/f'spike_{seed}.log').write_text(log)
    if ends < 2: raise RuntimeError(f'Spike did not reach program end; see {out}/spike_{seed}.log')
    memory_bytes = {bias+n:b for n,b in enumerate(blob)}
    identity = {int(k,0):(int(v,0),int(next(c['reset'] for c in platform['csrs'] if int(c['address'],0) == int(k,0)),0))
                for k,v in settings['spike_identity_reads'].items()}
    mstatus = int(next(c['reset'] for c in platform['csrs'] if c['name'] == 'mstatus'),0)
    text = spike_sail_trace(log,memory_bytes,len(prefix),substitutions,identity,mstatus)
    (out/f'{seed}_spike.trace').write_text(text)
    events = relocate(parse_sail_log(text),bias,size)
    end = next(n for n,e in enumerate(events) if e['pc_before'] == end_pc-bias)
    return events[:end+1], out/f'spike_{seed}.log'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sail', type=Path, default=SAIL)
    parser.add_argument('--spike', type=Path, default=ROOT/'../riscv-isa-sim/build/spike')
    parser.add_argument('--reference', choices=('sail','spike'), default='sail')
    parser.add_argument('--dut', choices=('single_lane','fetched'), default='single_lane')
    args = parser.parse_args()
    fetched = args.dut == 'fetched'
    on_spike = args.reference == 'spike'
    if on_spike and not fetched: parser.error('the Spike differential runs on the fetched core')
    label = 'Spike' if on_spike else 'Sail'
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    lock,_ = check_harness()
    if on_spike:
        reference = args.spike.resolve(); verify_spike(reference)
        settings = json.loads((ROOT/'config/spike_differential.json').read_text())
    else:
        reference = sail = args.sail.resolve(); check_sail(sail,lock)
        settings = json.loads((ROOT/('config/fetched_differential.json' if fetched else 'config/sail_differential.json')).read_text())
        if digest(sail) != settings['sail_binary_sha256']: raise ValueError('Sail binary pin differs')
        if settings['sail_override']['platform']['instructions_per_tick'] <= settings.get('long_run',settings)['instruction_limit']+1:
            raise ValueError('Sail timer tick interval must exceed the run limit')
    config = json.loads((ROOT/('config/memory_core.json' if fetched else 'config/single_lane.json')).read_text())
    versions = {}
    toolchain = json.loads((ROOT/'config/toolchain.lock').read_text())
    for name,command in [('verilator','verilator'),('riscv-gcc','riscv64-unknown-elf-gcc')]:
        version = subprocess.check_output([command,'--version'],text=True).splitlines()[0]
        if version != next(t['expected_first_line'] for t in toolchain['tools'] if t['name']==name):
            raise ValueError(f'{name} pin differs')
        versions[name] = version
    paths = config['sources'] + ['config/sail_differential.json','config/single_lane.json','config/platform.yaml',
        'config/commit_event.yaml','config/memory_protocol.yaml','config/toolchain.lock','config/act4.lock',
        'model/iss/sail_log.py','tools/run_sail_differential.py','tools/run_single_lane.py','tools/check_act4.py',
        'verif/core/differential_programs.py','verif/core/programs.py','verif/core/reference.py','verif/core/single_lane_tb.cpp',
        'verif/lockstep/comparator.py','verif/lockstep/generated/commit_event.py','verif/protocol/generated/memory_protocol.py',
        'sw/link/platform.ld','tests/test_sail_log.py']
    if on_spike: paths += ['config/spike_differential.json','model/iss/spike_log.py','tools/run_lockstep_smoke.py','config/references.lock']
    if fetched:
        paths += ['config/fetched_differential.json','config/memory_core.json','config/rob_two_wide.json',config['lint_config'],
                  'verif/core/fetched_core_tb.cpp','verif/unit/packed_bits.hpp']
    paths += [str(p.relative_to(ROOT)) for p in sorted((ROOT/lock['harness']['directory']).iterdir()) if p.is_file()]
    inputs = {p:digest(ROOT/p) for p in sorted(set(paths))}
    fingerprint = {'inputs':inputs,f'{args.reference}_sha256':digest(reference),'versions':versions}
    run_hash = hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()[:16]
    out = ROOT/('out/spike_fetched_differential' if on_spike else 'out/sail_fetched_differential' if fetched else 'out/sail_differential')/run_hash
    out.mkdir(parents=True,exist_ok=True)
    receipt = out/'receipt.json'; receipt.unlink(missing_ok=True)
    if not on_spike: override = out/'override.json'; override.write_text(json.dumps(settings['sail_override'])+'\n')
    def run(command,log,failure=None):
        result = subprocess.run([str(c) for c in command],cwd=ROOT,text=True,stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,timeout=600)
        (out/log).write_text(result.stdout)
        if failure:
            if not result.returncode or failure not in result.stdout:
                raise RuntimeError(f'expected failure was not reported; see {out/log}')
        elif result.returncode: raise RuntimeError(f'command failed; see {out/log}\n{result.stdout[-1500:]}')
        return result.stdout
    binary = build_fetched()[0] if fetched else build_core(config)
    results = []; references = {}
    rob = json.loads((ROOT/'config/rob_two_wide.json').read_text())
    generations = rob['entries'] << (rob['identity_bits'] - rob['entries'].bit_length() + 1)
    runs = [(seed, 1, settings.get('instruction_limit')) for seed in settings['seeds']]
    if fetched: runs.append((settings['long_run']['seed'], settings['long_run']['repeat'], settings['long_run'].get('instruction_limit')))
    for seed,repeat,limit in runs:
        image,boot,end_pc = program(seed, fetched, 0, repeat)
        words = [int.from_bytes(image[n:n+4],'little') for n in range(0,len(image),4)]
        if any(w & 127 == 0x73 and (w >> 12) & 3 and w >> 20 in COUNTERS for w in words):
            raise ValueError('differential program reads a timing counter')
        raw = out/f'{seed}.bin'; raw.write_bytes(image)
        assembly = out/f'{seed}.S'
        assembly.write_text(f'.section .text.init\n.globl _start\n_start:\n.incbin "{raw.relative_to(ROOT)}"\n')
        elf = out/f'{seed}.elf'
        run(['riscv64-unknown-elf-gcc','-march=rv32i','-mabi=ilp32','-nostdlib','-nostartfiles',
             '-Wl,--no-relax,--build-id=none','-T','sw/link/platform.ld',assembly,'-o',elf],f'compile_{seed}.log')
        if on_spike:
            expected,trace = spike_reference(reference,settings,seed,repeat,out,run)
        else:
            trace = out/f'{seed}.trace'
            run([sail,'--config',ROOT/'verif/arch/act4/sail.json','--config-override',override,
                 '--trace-instr','--trace-reg','--trace-mem','--trace-exception','--trace-step',
                 '--trace-output',trace,'--inst-limit',limit+1,elf],f'sail_{seed}.log')
            expected = parse_sail_log(trace.read_text())
            if len(expected) != limit: raise ValueError('Sail trace is incomplete')
            end = next((n for n,e in enumerate(expected) if e['pc_before'] == end_pc),None)
            if end is None: raise ValueError('Sail did not reach program end')
            expected = expected[:end+1]
        if [(e['pc_before'],e['instruction']) for e in expected[:len(boot)]] != [(4*n,ins) for n,ins in enumerate(boot)]:
            raise ValueError(f'{label} initialization sequence differs')
        covered = coverage(expected[len(boot):])
        if repeat > 1:
            # Retirements provide a lower bound on ROB allocations.
            if len(expected) <= generations + rob['entries']: raise ValueError('long run does not wrap ROB generations')
            covered['rob_generation_wrap'] = len(expected) // generations
        references[seed] = (raw,image,boot,expected)
        modes = ['normal'] + (settings['extra_modes'] if seed == 42 else [])
        for mode in modes:
            log = f'core_{seed}_{mode}.log'
            output = run([binary,raw,seed,len(expected),mode,'+verilator+rand+reset+2',f'+verilator+seed+{seed}'],log)
            if not fetched: check_trace(output,image,mode)
            count = compare_output(output,expected,boot)
            witnesses = fetched_coverage(expected,output) if fetched else {}
            if repeat > 1 and not witnesses['identity_recycles']: raise ValueError('long run did not recycle ROB identities')
            results.append(dict(seed=seed,mode=mode,compared_events=count,coverage={**covered,**witnesses},elf_sha256=digest(elf),
                                **{f'{args.reference}_trace_sha256':digest(trace)},rtl_log_sha256=digest(out/log),
                                bootstrap_events_per_reset=len(boot)))
            print(f'{label} differential {args.dut} seed={seed} {mode}: PASS ({count} events)',flush=True)
    raw,image,boot,expected = references[42]
    mutations = []
    if fetched:
        output = (out/'core_42_normal.log').read_text()
        actual = [unpack_event(line.split()[3:]) for line in output.splitlines() if line.startswith('EVENT ')]
        mutations = comparator_mutations(expected, actual)
        print(f'{label} comparator mutations: PASS ({len(mutations)} detected)',flush=True)
        negatives = []
        for mode,marker in (('fatal','platform fatal'),('hold_drain','final memory transaction did not drain')):
            run([binary,raw,42,len(expected),mode],f'negative_{mode}.log',failure=marker)
            negatives.append(mode)
        try: compare_output(run([binary,raw,42,len(expected)-1,'normal'],'negative_short.log'),expected,boot)
        except ValueError: negatives.append('short_trace')
        else: raise RuntimeError('short fetched trace was accepted')
        print(f'{label} fetched negatives: PASS ({", ".join(negatives)})',flush=True)
        mutations += negatives
    for name,file,old,new in ([
        ('fence_i_no_redirect','rtl/core/head_system_controller.sv','decoded.op inside {OP_MRET, OP_FENCE_I};','decoded.op == OP_MRET;'),
        ('fence_i_keeps_line','rtl/frontend/fetch_two_wide.sv','        end else state_q <= EMPTY;',
         '        end else if (redirect_pc_i[31:5] != pc_q[31:5]) state_q <= EMPTY;'),
    ] if fetched else [
        ('load_sign','rtl/core/single_lane_core.sv',"{{24{selected_word[7]}}, selected_word[7:0]}","{24'd0, selected_word[7:0]}"),
        ('trap_value','rtl/execute/execute_single.sv','trap_value_o = address_o;','trap_value_o = address_o + 4;'),
    ]):
        text = (ROOT/file).read_text()
        if text.count(old) != 1: raise ValueError('RTL mutation no longer matches')
        directory = out/name; directory.mkdir(exist_ok=True)
        changed = directory/Path(file).name; changed.write_text(text.replace(old,new))
        if fetched: mutant = build_fetched(directory/'obj_dir',(file,changed))[0]
        else:
            sources = [changed if p == file else p for p in config['sources']]
            run(['verilator','--cc','--exe','--build','--build-jobs','2','--Wall','-Wno-UNUSEDPARAM','-Wno-UNUSEDSIGNAL',
                 '--assert','--top-module',config['top'],'--Mdir',directory/'obj_dir','-CFLAGS',f'-std=c++17 -I{CORE_OUT}',
                 *sources,ROOT/'verif/core/single_lane_tb.cpp','-o','core_check'],name+'_build.log')
            mutant = directory/'obj_dir/core_check'
        output = run([mutant,raw,42,len(expected),'normal'],name+'.log')
        try: compare_output(output,expected,boot)
        except ComparisonError: mutations.append(name)
        else: raise RuntimeError(f'{label} comparison missed {name} RTL mutation')
        print(f'{label} RTL mutation {name}: PASS (detected)',flush=True)
    if any(digest(ROOT/p) != h for p,h in inputs.items()): raise ValueError('inputs changed during differential run')
    receipt.write_text(json.dumps({**fingerprint,'schema':1,'profile':settings['profile'],'runs':results,
        'core_binary_sha256':digest(binary),**({} if on_spike else {'sail_override':settings['sail_override']}),
        'mutations_detected':mutations,'limitations':settings['limitations'],
        ('two_wide_core_accepted' if fetched else 'architectural_slice_accepted'):False},indent=2)+'\n')
    print(f'Receipt: {receipt.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
