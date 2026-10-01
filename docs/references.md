# References

## Architecture and design

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

## Research papers

- [*Complexity-Effective Superscalar Processors*](https://www.cs.cmu.edu/afs/cs/academic/class/15740-f19/www/papers/isca97-palacharla-complexity.pdf),
  Palacharla, Jouppi and Smith, ISCA 1997: timing costs of rename, wakeup/select,
  and bypass logic; motivation for measuring the clock/complexity trade-off.
- [*Memory Dependence Prediction Using Store Sets*](https://people.csail.mit.edu/emer/media/papers/1998.06.isca.storesets.pdf),
  Chrysos and Emer, ISCA 1998: reference for the optional store-set study.
  Store-set prediction is not implemented.

## Verification and implementation tools

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
