# Verification

Each block gets directed and random testbenches, mutation checks and a synthesis
pass. The core is also checked against reference models and a few formal proofs.
This is not full ISA compliance or timing signoff.

Needs Python 3, GNU Make, a C++ toolchain, Verilator and the pinned OSS CAD
Suite. `check-fast` also needs `riscv64-unknown-elf-gcc` and the Spike build
pinned in [`config/references.lock`](../config/references.lock).

```sh
export OSS_CAD_SUITE=/path/to/oss-cad-suite
make check-fast LOCKSTEP_SPIKE=/path/to/riscv-isa-sim/build/spike
make system-core-check
```

| Command | Covers |
| --- | --- |
| `make check-fast` | Schemas, reference models, Spike lockstep. Not the full core |
| `make system-core-check` | Integer, branch, CSR and trap core |
| `make memory-core-check` | Adds loads, stores, FENCE and FENCE.I |
| `make head-memory-check` | Memory controller alone |
| `make sail-fetched-differential-check` | Real programs compared with Sail event by event. A small corpus, not ACT4 |

Per-block gates are `make <block>-check`; see the [Makefile](../Makefile).
