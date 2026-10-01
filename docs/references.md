# References

Read while designing the core. No code from them is used.

- [RISC-V ISA manuals](https://docs.riscv.org/): semantics, CSRs, traps
- [BOOM](https://docs.boom-core.org/en/latest/): renaming and branch recovery
- [RSD](https://github.com/rsd-devel/rsd), [FROST](https://github.com/twosigma/frost),
  [Coreblocks](https://github.com/kuznia-rdzeni/coreblocks),
  [CVA6](https://docs.openhwgroup.org/projects/cva6-user-manual/): other open cores
- [Complexity-Effective Superscalar Processors](https://www.cs.cmu.edu/afs/cs/academic/class/15740-f19/www/papers/isca97-palacharla-complexity.pdf)
  (Palacharla, Jouppi, Smith, 1997): timing cost of rename, wakeup and bypass
- [Memory Dependence Prediction Using Store Sets](https://people.csail.mit.edu/emer/media/papers/1998.06.isca.storesets.pdf)
  (Chrysos, Emer, 1998): for a later load/store queue
- [Spike](https://github.com/riscv-software-src/riscv-isa-sim),
  [Sail RISC-V](https://github.com/riscv/sail-riscv),
  [riscv-arch-test](https://github.com/riscv/riscv-arch-test): reference models and tests
- [Verilator](https://verilator.org/guide/latest/),
  [Yosys](https://yosyshq.readthedocs.io/projects/yosys/en/stable/),
  [SymbiYosys](https://yosyshq.readthedocs.io/projects/sby/en/stable/): simulation, synthesis, formal
- [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) and
  [SKY130 cells](https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd): early timing probes
- [LibreLane](https://librelane.readthedocs.io/en/stable/): the planned RTL-to-GDSII flow
