# Architecture

The diagrams describe the current working-tree implementation. They show
functional RTL hierarchy and major interfaces, not clock-cycle stage boundaries
or a post-synthesis netlist. The connected-core view uses
`FRONTEND_FAULTS=1`, `TRAP_SERVICE=1`, and `MEMORY_SERVICE=0`, matching the
[`system_core` profile](../config/system_core.json).

## Connected core

![Connected RTL](diagrams/rtl-overview.svg)

The outer boundary is [`fetch_execution_core`](../rtl/core/fetch_execution_core.sv).
It has an instruction-memory channel, accepted retirement/trap traces, and
external readiness/recovery controls. The additional data-memory and admission
ports are inactive in this diagram's memory-disabled profile. Here loads, stores,
`FENCE`, and `FENCE.I` are reported on `unsupported_o`. When
included in the admitted prefix, they block allocation of that whole prefix:
an ALU in lane 0 paired with an unsupported load in lane 1 is held too. This
is not a temporary readiness stall. A reset or redirect must discard the
unsupported instruction to move past it; an older accepted branch recovery or
system redirect can do this, as can an external flush.
The environment controls shown at the top are top-level inputs, not an invented
hardware controller.

The fetch block holds one 32-byte line and offers up to two consecutive
instructions. It does not prefetch another line while consuming the current
one. Redirects discard stale buffered contents and drain stale offered/accepted
requests; they do not retract an offered request. Sequential PC+4 prediction
feeds the backend, with taken control transfers resolved by execution port 0.

Decode, rename and allocation form an accepted transaction into the ROB and,
for ordinary operations, the issue queue. Accepted writeback updates the PRF and
wakes matching queued operands. The register-read address mux selects IQ tags
or the serialized head owner's tags. Head-read ownership suppresses both IQ
launch ports. For the current CSR path, only the first operand is consumed and
the second source tag is p0.

CSR/MRET/WFI allocate a solo ROB entry and retain a `head_dispatch` descriptor;
they do not enter the IQ and block all younger allocation until accepted
retirement or recovery. A successful system result uses atomic serial
retirement rather than an ordinary completion port. Illegal CSR accesses first
produce a raw fault completion. The ROB's head fault is then serviced through
the shared CSR bank; accepted trap entry updates trap state and redirects fetch.
WFI retires as an immediate-resume hint.

Fetch/illegal/ECALL/EBREAK faults use resultless scheduling tokens, with original
fault metadata retained by ROB slot and substituted at completion. The diagram
summarizes those fault/prediction side tables rather than drawing every field.
Two-wide arrows mean **up to two** accepted operations: lane-zero CFI/fault
cutoffs, resource availability, and solo system instructions can reduce the
accepted prefix. Retirement width is not an IPC measurement.

| Drawn block | Source / contract |
| --- | --- |
| Fetch and frontend fault decode | [`fetch_two_wide.sv`](../rtl/frontend/fetch_two_wide.sv), [`frontend_fault_decode.sv`](../rtl/frontend/frontend_fault_decode.sv) |
| Decode, side tables, execution/system coordination | [`control_flow_backend.sv`](../rtl/backend/control_flow_backend.sv) |
| Issue queue, head-read mux and backend connection | [`issue_backend.sv`](../rtl/backend/issue_backend.sv), [`issue_backend.json`](../config/issue_backend.json) |
| Rename/checkpoints, ROB and PRF ownership | [`backend_two_wide.sv`](../rtl/backend/backend_two_wide.sv), [`rename_recovery.sv`](../rtl/backend/rename_recovery.sv) |
| Execution ports | [`control_flow_pipeline.sv`](../rtl/execute/control_flow_pipeline.sv), [`alu_pipeline.sv`](../rtl/execute/alu_pipeline.sv) |
| Serialized descriptor and system state | [`head_dispatch.sv`](../rtl/core/head_dispatch.sv), [`head_system_controller.sv`](../rtl/core/head_system_controller.sv), [`csr_two_wide.sv`](../rtl/core/csr_two_wide.sv) |

The PRF exposes 64 physical tags, with p0 hardwired to zero and 63 stored
register payloads. A ROB identity contains a five-bit slot and eight-bit
generation. Rename has eight branch checkpoints. The source files and consumed
configuration remain authoritative for exact interfaces.

## Head memory subsystem

![Head memory controller](diagrams/head-memory-controller.svg)

[`head_memory_controller`](../rtl/core/head_memory_controller.sv) is instantiated
inside the production backend with `MEMORY_SERVICE=1` and `TRAP_SERVICE=1`.
The [`memory_core` profile](../config/memory_core.json) exercises fetched loads
and stores through the shared head dispatcher, both PRF operands, real ROB
retirement and shared CSR trap handling. The external admission producer remains
outside this core. The [standalone test](../verif/unit/head_memory_controller_tb.cpp)
continues checking the controller boundary separately.

Loads/stores allocate alone, bypass IQ and block younger allocation. The matching
ROB head reserves PRF reads until both operands are ready, then captures once.
Successful loads write their destination at solo retirement. Memory retirement
increments the shared `minstret` counter. Raw fault completion retains memory
ownership; actual trap acceptance applies CSR effects and releases it. A younger
memory descriptor cannot block an older nonmemory fault.

`flush_ready_o` is low during irrevocable MMIO ownership. The caller must defer
external flush and keep retirement/trap acceptance available. Offered cached
reads and committed stores drain after legal cancellation. Either port's fatal
error freezes architectural progress until reset; instruction-port fatal does
not cancel granted memory traffic. Reset must also clear the external targets.
See the profile contract for admission, issue permission, drain and reset rules.

The preparation stage captures instruction, PC, operands, address and attributes
once at the matching ROB head. It retains that descriptor until accepted
retirement/trap entry or legal cancellation. The transaction path has one active
owner across load, committed-store and MMIO-store engines. Its arbiter serves
only these data engines; it is not shared with instruction fetch.

A cacheable store requires external admission before accepted solo retirement
commits its bytes. Its write drains afterward and blocks a new memory owner.
“Cacheable” describes the address class; no cache is implemented in this path.
Loads and MMIO stores retain their result until retirement. A fault can complete
into the ROB-facing interface once, but its result and MMIO irrevocability remain
held until actual trap acceptance. These loads execute conservatively at the
head; this subsystem does not yet provide speculative loads or an LSQ.

The [controller contract](../config/head_memory_controller.json) defines the
acceptance and recovery requirements. The
[transaction-path contract](../config/memory_transaction_path.json) and
[committed-store contract](../config/committed_store.json) define bus ownership
and admission guarantees. The diagram's head-ownership/event block groups
logic inside the controller; it is not another RTL module or metadata RAM.

## Instruction set

The [platform profile](../config/platform.yaml) targets **RV32I + Zicsr + Zifencei**,
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
| FENCE | Serialized full ordering with `MEMORY_SERVICE=1`; waits for older data completion, including store write responses; blocks dispatch when disabled |
| FENCE.I | With `MEMORY_SERVICE=1`: same barrier as FENCE, then refetches PC+4, discarding the fetch line buffer and older instruction requests; no caches exist yet. Blocks dispatch when disabled |

The [system-core contract](../config/system_core.json) records the supported
behavior and verification scope.

## Execution flow

For an ordinary integer instruction:

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

## Diagram conventions and editing

- Solid arrows show payload or interface traffic; dashed arrows show control.
  The memory detail uses two-headed channel arrows for bundled request,
  response and handshake directions.
- Gray blocks contain clocked state. White logic blocks are combinational;
  white outlined containers identify module hierarchy. Position is not latency.
- Common clock/reset distribution and most ready/accept return wires are
  omitted. Use the linked interfaces for exact handshake and acceptance rules.
- No predictor, cache, LSQ, FPGA or future connection is drawn as implemented.

[`core.drawio`](diagrams/core.drawio) is the editable source for both pages.
The SVGs are rendered with draw.io Desktop and use native SVG text. Edit the
source, then export page 1 to `rtl-overview.svg` and page 2 to
`head-memory-controller.svg`, using a light background. From the repository root,
an installed `drawio` command can export them with:

```sh
drawio --disable-update --export --format svg --theme light --embed-svg-fonts false --border 24 --page-index 1 --output docs/diagrams/rtl-overview.svg docs/diagrams/core.drawio
drawio --disable-update --export --format svg --theme light --embed-svg-fonts false --border 24 --page-index 2 --output docs/diagrams/head-memory-controller.svg docs/diagrams/core.drawio
```

The hierarchy/interface approach is informed by the primary
[BOOM overview and detail diagrams](https://docs.boom-core.org/en/latest/sections/intro-overview/boom-pipeline.html),
[Ibex source diagram](https://github.com/lowRISC/ibex/blob/master/doc/03_reference/images/blockdiagram.svg),
and [CV32E40P source diagram](https://github.com/openhwgroup/cv32e40p/blob/master/docs/images/blockdiagram.svg).
These projects use different visual styles; monochrome and square corners are
choices for this repository, not a universal RTL standard. This project's
figures are original and do not embed their artwork. The editable-source/SVG
workflow follows [draw.io's export guidance](https://www.drawio.com/docs/manual/export/export-to-svg/).
