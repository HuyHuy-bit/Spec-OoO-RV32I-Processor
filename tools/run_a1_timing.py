#!/usr/bin/env python3
"""Map the A1 probes to SKY130 cells and measure their constrained timing."""

import argparse
from collections import Counter
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re

from fetch_a1_timing import DEPS, verify
from run_a1_probe import ROOT, SOURCES, TOP, build, digest, run, simulate, verify_synthesis_suite


OUT = ROOT / "out/a1_timing"


def sdc(config: dict, clock_port: bool) -> str:
    c = config
    if c["schema"] != 1 or c["exceptions"] or c["period_ns"] <= 0:
        raise ValueError("unsupported timing intent")
    lines = [
        f'create_clock -name core -period {c["period_ns"]}' + (' [get_ports clk_i]' if clock_port else ''),
        f'set_clock_latency {c["clock_latency_ns"]} [get_clocks core]',
        f'set_clock_uncertainty -setup {c["setup_uncertainty_ns"]} [get_clocks core]',
        f'set_clock_uncertainty -hold {c["hold_uncertainty_ns"]} [get_clocks core]',
        'set data_inputs [all_inputs]',
    ]
    if clock_port:
        lines += [f'set_clock_transition {c["clock_transition_ns"]} [get_clocks core]',
                  'set data_inputs {}',
                  'foreach port [all_inputs] { if {[get_full_name $port] ne "clk_i"} { lappend data_inputs $port } }']
    for direction, ports in (("input", "$data_inputs"), ("output", "[all_outputs]")):
        for bound in ("min", "max"):
            lines += [f'set_{direction}_delay -{bound} {c[f"{direction}_delay_{bound}_ns"]} -clock core {ports}']
    lines += [
        f'set_input_transition {c["input_transition_ns"]} $data_inputs',
        f'set_load {c["output_load_pf"]} [all_outputs]',
        f'set_max_transition {c["max_transition_ns"]} [current_design]',
        f'set_max_capacitance {c["max_capacitance_pf"]} [current_design]',
        f'set_max_fanout {c["max_fanout"]} [current_design]',
        f'set_wire_load_mode {c["wire_load_mode"]}',
        f'set_wire_load_model -name {c["wire_load_model"]}',
    ]
    return "\n".join(lines) + "\n"


def analyze(sta: str, liberty: Path, netlist: Path, top: str, directory: Path, constraints: str,
            constant_outputs: tuple[str, ...] = ()) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory.relative_to(ROOT)
    (directory / "constraints.sdc").write_text(constraints)
    setup = (f'set liberty {liberty}\nset netlist {netlist}\nset top {top}\n'
             f'set constraints {prefix}/constraints.sdc\nset reports {prefix}\n'
             'if {[catch {source synth/a1_timing.tcl} reason]} { puts stderr $reason; exit 1 }\nexit 0\n')
    (directory / "run.tcl").write_text(setup)
    log = run([sta, "-no_init", "-exit", str(directory / "run.tcl")], directory / "sta.log")
    if "A1 STA COMPLETE" not in log or re.search(r"(?m)^(Error|Warning):", log):
        raise RuntimeError(f"STA did not finish cleanly: {log[-2000:]}")
    coverage = (directory / "coverage.rpt").read_text().strip()
    constant_endpoints = []
    if coverage:
        match = re.fullmatch(r'Warning: There are (\d+) unconstrained endpoints\.\n((?:  \S+\n?)+)', coverage)
        if match:
            constant_endpoints = match[2].split()
        if (not match or int(match[1]) != len(constant_endpoints)
                or not set(constant_endpoints) <= set(constant_outputs)):
            raise RuntimeError(f"incomplete timing constraints in {top}: {coverage}")
    slack = re.search(r'worst slack\s+(-?[\d.]+)', (directory / "worst_slack.rpt").read_text())
    tns = re.search(r'tns\s+(-?[\d.]+)', (directory / "tns.rpt").read_text())
    hold = re.findall(r'(-?[\d.]+)\s+slack \(', (directory / "hold.rpt").read_text())
    if not slack or not tns or not hold:
        raise RuntimeError("missing timing metrics")
    setup_report = (directory / "setup.rpt").read_text()
    start = re.search(r"(?m)^Startpoint: (.+)$", setup_report)
    end = re.search(r"(?m)^Endpoint: (.+)$", setup_report)
    if not start or not end:
        raise RuntimeError("missing critical path endpoints")
    def count(name: str) -> int:
        return len(re.findall(r"(?m)^\S.*?\s+-\d+\.\d+(?: \(VIOLATED\))?\s*$",
                              (directory / name).read_text()))
    return {"setup_worst_slack_ns": float(slack[1]), "setup_tns_ns": float(tns[1]),
            "critical_setup_startpoint": start[1], "critical_setup_endpoint": end[1],
            "hold_worst_slack_ns": min(map(float, hold)), "unconstrained_endpoints": len(constant_endpoints),
            "unconstrained_nonconstant_endpoints": 0, "constant_output_endpoints": constant_endpoints,
            "setup_hold_violation_count": count("setup_hold_violations.rpt"),
            "slew_violation_count": count("slew_violations.rpt"),
            "setup_hold_met": float(slack[1]) >= 0 and min(map(float, hold)) >= 0}


def check_controls(sta: str, liberty: Path, config: dict) -> list[str]:
    top = "a1_select_probe"
    netlist = (OUT / top / "mapped.v").relative_to(ROOT)
    missing_output = "\n".join(line for line in sdc(config, False).splitlines()
                               if not line.startswith("set_output_delay")) + "\n"
    try:
        analyze(sta, liberty, netlist, top, OUT / "controls/missing_output_delay", missing_output)
    except RuntimeError as error:
        if "incomplete timing constraints" not in str(error):
            raise
    else:
        raise RuntimeError("missing-output-delay control was not rejected")
    tight = analyze(sta, liberty, netlist, top, OUT / "controls/tight_clock",
                    sdc({**config, "period_ns": 0.5}, False))
    if tight["setup_hold_met"] or tight["setup_worst_slack_ns"] >= 0 or tight["setup_hold_violation_count"] == 0:
        raise RuntimeError("tight-clock control failed to detect timing violations")
    slew = analyze(sta, liberty, netlist, top, OUT / "controls/tight_slew",
                   sdc({**config, "max_transition_ns": 0.001}, False))
    if slew["slew_violation_count"] == 0:
        raise RuntimeError("tight-slew control failed to detect transition violations")
    print("A1 timing controls: PASS (missing delay rejected; tight clock and slew violations detected)", flush=True)
    return ["missing_output_delay", "tight_clock", "tight_slew"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path,
                        default=Path.home() / "tools/oss-cad-suite-20260905/oss-cad-suite")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    receipt = OUT / "receipt.json"
    receipt.unlink(missing_ok=True)
    lock = verify()
    yosys = verify_synthesis_suite(args.suite.resolve())
    sta = str(DEPS / lock["opensta"]["executable"])
    if run([sta, "-version"], OUT / "sta_version.log").strip() != lock["opensta"]["version"]:
        raise RuntimeError("OpenSTA version mismatch")
    tools = json.loads((ROOT / "config/toolchain.lock").read_text())["tools"]
    expected = next(tool["expected_first_line"] for tool in tools if tool["name"] == "verilator")
    if run(["verilator", "--version"], OUT / "verilator_version.log").strip() != expected:
        raise RuntimeError("Verilator version mismatch")
    config = json.loads((ROOT / "config/a1_timing.json").read_text())
    dont_use = " ".join(f"-dont_use {pattern}" for pattern in config["dont_use"])
    liberty = (DEPS / lock["library"]["path"]).relative_to(ROOT)
    abc = OUT / "abc.constr"
    abc.write_text(f'set_driving_cell {config["abc_driver"]}\nset_load {config["abc_load_ff"]}\n')
    inputs = SOURCES + ["config/a1_timing.json", "config/a1_timing.lock", "config/synthesis.lock",
                       "config/toolchain.lock", "synth/a1_timing.tcl", "tools/run_a1_timing.py",
                       "tools/run_a1_probe.py", "tools/fetch_a1_timing.py", "verif/unit/a1_backend_probe_tb.cpp",
                       "config/prf_contract.json"]
    results = {"schema": 1, "inputs_sha256": {p: digest(ROOT / p) for p in inputs},
               "intent": config, "library": lock["library"], "opensta": lock["opensta"],
               "physical_implementation": False,
               "checks_not_reported_by_tool": ["max_capacitance", "max_fanout"],
               "tops": {}}
    for top, sources, stored_bits in (("prf_4r2w", SOURCES[:1], 2016),
                                      ("a1_select_probe", SOURCES[1:2], 0),
                                      (TOP, SOURCES, 2016)):
        directory = OUT / top
        directory.mkdir(parents=True, exist_ok=True)
        prefix = directory.relative_to(ROOT)
        script = (f'read_slang --top {top} -D SYNTHESIS {" ".join(sources)}; '
                  f'synth -top {top} -flatten -noabc; dfflibmap -liberty {liberty} {dont_use}; '
                  f'abc -liberty {liberty} {dont_use} -constr {abc.relative_to(ROOT)} -D {config["period_ns"] * 1000}; '
                  f'clean -purge; read_liberty -lib {liberty}; check -assert; '
                  f'stat -liberty {liberty}; write_json {prefix}/mapped.json; '
                  f'write_verilog -noattr -noexpr {prefix}/mapped.v')
        (directory / "synth.ys").write_text(script + "\n")
        log = run([yosys, "-Q", "-T", "-m", "slang", "-s", str(directory / "synth.ys")], directory / "synth.log")
        net = json.loads((directory / "mapped.json").read_text())
        counts = Counter(cell["type"] for cell in net["modules"][top]["cells"].values())
        if any(not cell.startswith("sky130_fd_sc_hd__") for cell in counts):
            raise RuntimeError(f"unmapped cell in {top}")
        if any(fnmatchcase(cell, pattern) for cell in counts for pattern in config["dont_use"]):
            raise RuntimeError(f"excluded technology cell in {top}")
        bits = sum(count for cell, count in counts.items()
                   if cell.startswith(("sky130_fd_sc_hd__df", "sky130_fd_sc_hd__edf")))
        if bits != stored_bits:
            raise RuntimeError(f"unexpected flop count: {bits}")
        area = re.search(r'Chip area for (?:top )?module.*?:\s*([\d.]+)', log)
        wire_load = re.search(r'ABC: WireLoad = "([^"]+)"', log)
        if not area or not wire_load:
            raise RuntimeError("missing mapped area or ABC wire-load model")
        timing = analyze(sta, liberty, prefix / "mapped.v", top, directory, sdc(config, top != "a1_select_probe"))
        results["tops"][top] = {"cell_counts": dict(sorted(counts.items())), "stored_bits": bits,
                                "abc_wire_load_model": wire_load[1],
                                "cell_area_um2": float(area[1]), **timing,
                                "netlist_sha256": digest(directory / "mapped.v")}
        print(f'A1 timing {top}: area={area[1]} um^2, setup slack={timing["setup_worst_slack_ns"]} ns, '
              f'hold slack={timing["hold_worst_slack_ns"]} ns', flush=True)
    results["controls_detected"] = check_controls(sta, liberty, config)
    for report in ("read_data", "wakeup_read"):
        text = (OUT / TOP / f"{report}.rpt").read_text()
        slacks = re.findall(r'(-?[\d.]+)\s+slack \(', text)
        if not slacks:
            raise RuntimeError(f"missing {report} path")
        results["tops"][TOP][f"{report}_worst_slack_ns"] = min(map(float, slacks))
    prefix = (OUT / TOP).relative_to(ROOT)
    model_script = (f'read_liberty -ignore_miss_func {liberty}; read_verilog {prefix}/mapped.v; '
                    f'hierarchy -check -top {TOP}; write_verilog -noattr {prefix}/simulation.v')
    run([yosys, "-Q", "-T", "-p", model_script], OUT / "models.log")
    executable = build([str(OUT / TOP / "simulation.v")], OUT / "mapped_sim", mapped=True)
    results["mapped_simulation"] = simulate(executable)
    reports = ["coverage.rpt", "setup.rpt", "hold.rpt", "paths_including_unconstrained.rpt",
               "setup_hold_violations.rpt", "slew_violations.rpt", "worst_slack.rpt", "tns.rpt",
               "constraints.sdc", "sta.log"]
    directories = [OUT / top for top in results["tops"]]
    directories += [OUT / "controls" / name for name in results["controls_detected"]]
    artifacts = [directory / name for directory in directories for name in reports]
    artifacts += [OUT / TOP / f"{name}.rpt" for name in ("read_data", "wakeup_read")]
    artifacts += [OUT / top / "mapped.v" for top in results["tops"]]
    artifacts += [OUT / "mapped_sim" / f"seed_{item['seed']}.log" for item in results["mapped_simulation"]]
    results["artifacts_sha256"] = {str(path.relative_to(ROOT)): digest(path) for path in artifacts}
    receipt.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
