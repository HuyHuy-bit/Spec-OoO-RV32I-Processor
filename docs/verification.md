# Verification

Verification uses shared runners, RTL testbenches, reference models, and focused
formal harnesses. RTL unit gates combine directed/random simulation, caller
assertions, mutation checks, and generic synthesis. These checks have specific
scopes; they do not establish complete ISA compliance or physical timing.

## Toolchain

The flow uses Python 3, GNU Make, a C++ toolchain, Verilator, and the pinned
OSS CAD Suite for Yosys/Slang synthesis. `check-fast` also requires
`riscv64-unknown-elf-gcc` and the Spike binary/source checkout pinned in
[`config/references.lock`](../config/references.lock); an arbitrary Spike build
will fail the runner's hash checks. Tool requirements are recorded in
[`config/toolchain.lock`](../config/toolchain.lock) and synthesis pins in
[`config/synthesis.lock`](../config/synthesis.lock).

## Running the checks

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
| `make memory-core-check` | Fetched loads/stores, FENCE ordering, FENCE.I self-modifying code, real retirement/traps, admission, cancellation and data-port ownership |
| `make head-memory-check` | Standalone memory controller with synthetic head/acceptance inputs and real transaction engines |
| `make sail-fetched-differential-check` | Real RV32 programs on the fetched memory core (`MEMORY_SERVICE=1`) compared event-by-event with the pinned Sail model: 3 seeds plus stall and reset schedules, comparator mutations and failure negatives. A bounded corpus, not ACT4 or full ISA acceptance |

The [Makefile](../Makefile) and [unit profiles](../tools/unit_profiles.py)
define the individual per-block gates (`make <block>-check`). Generated outputs
and full evidence archives stay local.

## Next steps

- Implement the producer behind the cached-store admission interface.
- Add the remaining memory ordering, load/store queue, forwarding, cache,
  cache-aware FENCE.I, and dynamic prediction functionality.
- Complete the selected ISA and integrated-core verification gates.
- Measure full-core timing and review the clock target before hardening.
- Run the LibreLane RTL-to-GDSII flow and report measured implementation results.
