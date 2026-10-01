# Speculative Out-of-Order RV32I Processor
Hi,

This is my two-wide speculative RISC-V processor in SystemVerilog, developed from RTL
through verification toward a LibreLane GDSII implementation.

The current core connects instruction fetch, register renaming, out-of-order
integer execution, branch recovery, in-order retirement, and machine-mode
system/trap handling. An opt-in memory profile connects conservative head-only
loads and stores through the real PRF, ROB, and CSR trap path.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Instruction set](#instruction-set)
- [Execution](#execution)
- [Simulation and verification](#simulation-and-verification)
- [Next steps](#next-steps)
- [Repository layout](#repository-layout)
- [References](#references)
- [Author](#author)

## Overview

An out-of-order processor can execute ready instructions while earlier ones wait
for their operands. It must still preserve the behavior of a sequential program:
register dependencies, branch outcomes, exceptions, and architectural updates
must remain correct even when work finishes in a different order.

This project explores how those requirements fit together in hardware:

1. **Scheduling:** rename architectural registers to physical registers, track
   dependencies, and select ready instructions for two execution ports.
2. **Speculation:** execute beyond unresolved branches and recover register
   mappings and younger work when the predicted path is wrong.
3. **Precise state:** retire instructions in program order and coordinate system
   operations, traps, and memory side effects at the ROB head.
4. **Physical implementation:** verify the design, measure its timing, and take
   it through a direct RTL-to-GDSII flow using LibreLane.

The design is still in progress. The diagrams and specifications below describe
implemented RTL; remaining integration and physical work are listed separately.

## Architecture

### Core

![Connected RTL overview](docs/diagrams/rtl-overview.svg)

The overview shows the default `MEMORY_SERVICE=0` profile.
Solid arrows carry payload/interface traffic; dashed arrows show control; gray
blocks contain clocked state. See the [architecture guide](docs/architecture.md)
for exact module links, configuration, and diagram conventions, or open the
[editable draw.io source](docs/diagrams/core.drawio).

The current top is [`fetch_execution_core`](rtl/core/fetch_execution_core.sv).
Its external interfaces include separate instruction/data memory channels,
retirement/trap traces, and readiness/recovery controls. Enable
`MEMORY_SERVICE=1` with `TRAP_SERVICE=1` for the
[`memory_core` profile](config/memory_core.json); data outputs are inactive
in the default memory-disabled profile.

| Resource | Current configuration |
| --- | --- |
| Fetch | Up to 2 instructions; one held 32-byte line; one outstanding request |
| Rename / dispatch | Up to 2 instructions in an accepted prefix |
| Issue queue | 16 entries, feeding 2 execution ports |
| Physical register file | 64 tags, 4 read ports, 2 write ports; p0 is fixed at zero |
| Reorder buffer | 32 entries; up to 2 in-order retirements per cycle |
| Branch recovery | 8 rename checkpoints; sequential/not-taken prediction |
| Execution port 0 | Integer ALU and branch/jump resolution |
| Execution port 1 | Integer ALU |

These are hardware capacities, not measured IPC or frequency results.

**Frontend.** Fetch holds one instruction line and supplies consecutive
instructions to decode. It does not prefetch the next line or provide an
instruction cache. Execution port 0 resolves control transfers and redirects
fetch when the sequential prediction is wrong.

**Rename and scheduling.** Rename maps architectural registers to physical
registers and allocates ROB entries. Ordinary operations enter the issue queue,
where operand readiness determines when they can launch. Accepted results write
the PRF and wake dependent instructions.

**Retirement and recovery.** The ROB keeps architectural updates in program
order. Rename checkpoints support recovery from incorrect branch predictions.
CSR operations, MRET, and WFI use a separate serialized head path: they bypass
the issue queue and block younger allocation until retirement or recovery.
Trap entry updates the shared machine CSR bank and redirects fetch.

### Memory subsystem

![Head memory controller](docs/diagrams/head-memory-controller.svg)

The [`head_memory_controller`](rtl/core/head_memory_controller.sv) combines
address preparation, transaction ownership, and retirement/trap event handling.
It connects to the fetched backend when `MEMORY_SERVICE=1`, sharing the head
dispatcher and PRF reads with system operations.
Two-headed arrows bundle request, response, and handshake directions.

- **Preparation:** capture operands and the effective address, check alignment
  and physical memory attributes, and retain the descriptor.
- **Loads:** execute conservatively at the ROB head and hold the result or fault.
- **Cacheable stores:** require an external admission guarantee, commit on
  accepted retirement, then drain the write to memory.
- **MMIO stores:** retain transaction ownership and the result until the
  corresponding retirement or trap acceptance.
- **Data-port arbitration:** share one external port among the transaction
  engines and route responses to their owner.

Here, “cacheable” is an address attribute; the path does not contain a cache.
A real store-admission producer, a load/store queue, and store-to-load
forwarding remain pending. External flush must wait for `flush_ready_o` while
MMIO ownership is irrevocable; accepted cached stores continue draining.

## Instruction set

The [platform profile](config/platform.yaml) targets **RV32I + Zicsr + Zifencei**,
with 32-bit instructions, little-endian data, and machine-mode execution.
Interrupts are disabled. This target is not a claim of full ISA acceptance.

| Instruction family | Current fetched-core behavior |
| --- | --- |
| Integer arithmetic, logical, shift and comparison operations; LUI/AUIPC | Connected through the integer execution paths |
| Conditional branches; JAL/JALR | Connected through execution port 0 and branch recovery |
| CSR register/immediate forms | Serialized access to the implemented CSR set; illegal accesses trap |
| MRET | Serialized trap return with an accepted redirect |
| WFI | Serialized immediate-resume hint |
| ECALL, EBREAK, illegal instructions and fetch faults | Precise trap path |
| Loads and stores | Head-only execution with `MEMORY_SERVICE=1`; block dispatch when disabled |
| FENCE and FENCE.I | Unsupported in the fetched core and block dispatch |

An unsupported operation in the admitted prefix blocks allocation of that whole
prefix, including an older ALU instruction paired with it. This is not a
temporary operand stall or an automatic illegal-instruction trap; a reset or
redirect must discard the unsupported instruction to move past it. The
[system-core contract](config/system_core.json) records the supported behavior
and verification scope.

## Execution

For an ordinary integer instruction, the functional flow is:

1. **Fetch and decode:** obtain instruction bits and PC, classify the operation,
   and retain any fault information.
2. **Rename and allocate:** map sources to physical registers, allocate a
   destination when needed, and reserve ROB/issue-queue space.
3. **Wait and issue:** track source readiness and select eligible work for an
   available execution port.
4. **Execute and complete:** compute the result; on accepted completion, update
   the PRF and make dependent operands ready.
5. **Retire:** accept completed instructions from the ROB head in program order.

These steps describe ownership and data flow, not fixed clock-cycle stages.
Multiple instructions may occupy different parts of the flow simultaneously.

A mispredicted branch discards younger work and restores the appropriate rename
state. A fault waits until it reaches the ROB head before accepted trap entry
updates architectural trap state. CSR/MRET/WFI instead use the serialized head
controller and commit their successful effects atomically at retirement.
External flush takes redirect priority over system redirects, which take
priority over branch recovery.

## Simulation and verification

Verification uses shared runners, RTL testbenches, reference models, and focused
formal harnesses. RTL unit gates combine directed/random simulation, caller
assertions, mutation checks, and generic synthesis. These checks have specific
scopes; they do not establish complete ISA compliance or physical timing.

### Running the checks

The existing flow uses Python 3, GNU Make, a C++ toolchain, Verilator, and the
pinned OSS CAD Suite for Yosys/Slang synthesis. `check-fast` also requires
`riscv64-unknown-elf-gcc` and the Spike binary/source checkout pinned in
[`config/references.lock`](config/references.lock); an arbitrary Spike build will
fail the runner's hash checks. Tool requirements are recorded in
[`config/toolchain.lock`](config/toolchain.lock) and synthesis pins in
[`config/synthesis.lock`](config/synthesis.lock).

From the repository root, set `LOCKSTEP_SPIKE` to that checkout's `build/spike`
for the reference checks. Set `OSS_CAD_SUITE` for the RTL unit gates:

```sh
make check-fast LOCKSTEP_SPIKE=/path/to/riscv-isa-sim/build/spike
export OSS_CAD_SUITE=/path/to/oss-cad-suite
make system-core-check
make memory-core-check
make head-memory-check
```

| Command | Scope |
| --- | --- |
| `make check-fast` | Foundation, schema, model and reference checks; not the complete two-wide core regression |
| `make system-core-check` | Connected fetch/integer/control/system core, including traps and redirects |
| `make memory-core-check` | Fetched load/store programs, real retirement/traps, admission, cancellation and data-port ownership |
| `make head-memory-check` | Standalone memory controller with synthetic head/acceptance inputs and real transaction engines |

The [Makefile](Makefile) and [unit profiles](tools/unit_profiles.py) define the
individual gates. Generated outputs and full evidence archives stay local.

## Next steps

- Implement the producer behind the cached-store admission interface.
- Add the remaining memory ordering, load/store queue, forwarding, cache,
  fence, and dynamic prediction functionality.
- Complete the selected ISA and integrated-core verification gates.
- Measure full-core timing and review the clock target before hardening.
- Run the LibreLane RTL-to-GDSII flow and report measured implementation results.

The project follows the direct ASIC flow. Full-core frequency, physical closure, and GDSII results remain pending.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`rtl/`](rtl/) | SystemVerilog modules and generated packages |
| [`config/`](config/) | Platform definition, interface contracts, verification profiles and tool pins |
| [`verif/`](verif/) | RTL benches, reference models and formal harnesses |
| [`tools/`](tools/) | Shared generation and verification runners |
| [`docs/`](docs/) | Architecture documentation and editable diagrams |

## References

### Architecture and design

- [RISC-V Unprivileged ISA](https://docs.riscv.org/reference/isa/unpriv/unpriv-index.html)
  and [Privileged Architecture](https://docs.riscv.org/reference/isa/priv/priv-index.html):
  instruction semantics, machine CSRs, and precise traps.
- [BOOM](https://docs.boom-core.org/en/latest/): register renaming, branch recovery,
  allocation lists, and register-file/bypass trade-offs.
- [RSD](https://github.com/rsd-devel/rsd): an RV32 SystemVerilog out-of-order
  comparison design.
- [FROST](https://github.com/twosigma/frost): completion holding and layered
  verification; [Coreblocks](https://github.com/kuznia-rdzeni/coreblocks): block
  testing and full-core integration.
- [CVA6](https://docs.openhwgroup.org/projects/cva6-user-manual/03_cva6_design/issue_stage.html):
  issue handshakes and transaction-ID tracking.

These processor projects were studied as design comparisons; their RTL is not
incorporated into this core.

### Research papers

- [*Complexity-Effective Superscalar Processors*](https://www.cs.cmu.edu/afs/cs/academic/class/15740-f19/www/papers/isca97-palacharla-complexity.pdf),
  Palacharla, Jouppi and Smith, ISCA 1997: timing costs of rename, wakeup/select,
  and bypass logic; motivation for measuring the clock/complexity trade-off.
- [*Memory Dependence Prediction Using Store Sets*](https://people.csail.mit.edu/emer/media/papers/1998.06.isca.storesets.pdf),
  Chrysos and Emer, ISCA 1998: reference for the optional store-set study.
  Store-set prediction is not implemented.

### Verification and implementation tools

- [Spike](https://github.com/riscv-software-src/riscv-isa-sim),
  [Sail RISC-V](https://github.com/riscv/sail-riscv), and
  [RISC-V Architectural Tests](https://github.com/riscv/riscv-arch-test):
  reference-model comparisons and selected architectural tests on the earlier
  serialized core.
- [RVFI](https://github.com/YosysHQ/riscv-formal/blob/main/docs/source/rvfi.rst):
  reference for the architectural event format, with explicit project deviations;
  this is not a full-core riscv-formal acceptance claim.
- [Verilator](https://verilator.org/guide/latest/),
  [Yosys](https://yosyshq.readthedocs.io/projects/yosys/en/stable/),
  [Yosys Slang frontend](https://github.com/povik/yosys-slang), and
  [SymbiYosys](https://yosyshq.readthedocs.io/projects/sby/en/stable/):
  RTL simulation, synthesis, and focused formal checks.
- [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) and
  [SKY130 standard-cell data](https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd):
  early timing-feasibility probes, not full-core timing closure.
- [LibreLane](https://librelane.readthedocs.io/en/stable/): the planned
  RTL-to-GDSII implementation flow.

## Author

**Nguyen Ngoc Huy**

[LinkedIn](https://www.linkedin.com/in/nguyen-ngoc-huy-119027380/) ·
[Email](mailto:huynguyenngoccbg@gmail.com) ·
[Portfolio](https://portfolio-h-huy.vercel.app/)
