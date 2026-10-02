#!/usr/bin/env python3
"""Cycle occupancy model for LQ / D$ miss-slot sizing (C0 planning input, not RTL)."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
import itertools
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
FAULTS = (None, "overissue", "early_lq_release", "lose_response")


class ModelError(Exception):
    pass


@dataclass
class Op:
    kind: str                 # alu | load | store
    dep: int | None = None    # producer whose writeback the load address waits on
    miss: bool = False
    dispatch_cycle: int | None = None
    launched: bool = False
    done: bool = False
    retire_cycle: int | None = None


@dataclass(frozen=True)
class Machine:
    lq: int = 8
    sq: int = 8
    rob: int = 32
    mshr: int = 1
    credits: int = 1
    blocking: bool | None = None   # None: blocking cache exactly when mshr == 1
    hit_latency: int = 2
    miss_latency: int = 20
    completion_buffer: int = 2
    width: int = 2


def workload(seed, instructions, load_density, store_density=0.0, miss_rate=0.5, chains=0):
    """chains=K: each load's address depends on the load K loads earlier; 0 = independent."""
    rng = random.Random(seed)
    ops, loads = [], []
    for index in range(instructions):
        draw = rng.random()
        if draw < load_density:
            dep = loads[-chains] if chains and len(loads) >= chains else None
            ops.append(Op("load", dep, rng.random() < miss_rate))
            loads.append(index)
        elif draw < load_density + store_density:
            ops.append(Op("store"))
        else:
            ops.append(Op("alu"))
    return ops


def simulate(machine, ops, warmup=0, max_cycles=None, fault=None):
    """Per cycle, in order: accept responses, writeback, retire, AGU launch, dispatch.

    Returns window statistics over cycles that start with at least `warmup` ops retired.
    Raises ModelError on an invariant violation or when the bounded drain times out.
    """
    if fault not in FAULTS:
        raise ValueError(fault)
    m = machine
    blocking = m.mshr == 1 if m.blocking is None else m.blocking
    n = len(ops)
    limit = max_cycles or n * (m.miss_latency + m.hit_latency + 4) + 100
    head = tail = lq = sq = misses = cycle = 0
    pending = []   # (ready_cycle, op index, is_miss): launched, response not yet accepted
    cbuf = []      # accepted load results waiting for a writeback slot
    lost = False
    stats = Counter()
    peak = 0
    while head < n:
        if cycle >= limit:
            raise ModelError(f"timeout at cycle {cycle}: {head}/{n} retired")
        window = head >= warmup

        # Responses: oldest-ready first; a full completion buffer leaves the MSHR held.
        pending.sort()
        for item in [p for p in pending if p[0] <= cycle]:
            if len(cbuf) >= m.completion_buffer:
                stats["response_blocked"] += window
                break
            pending.remove(item)
            misses -= item[2]
            if fault == "lose_response" and item[2] and not lost:
                lost = True
                continue
            cbuf.append(item[1])

        # Writeback: two slots shared by ALU results and load results, oldest first.
        candidates = sorted(cbuf + [i for i in range(head, tail) if ops[i].kind == "alu"
                                    and not ops[i].done and ops[i].dispatch_cycle < cycle])
        stats["wb_congested"] += window and len(candidates) > m.width
        for i in candidates[:m.width]:
            ops[i].done = True
            if i in cbuf:
                cbuf.remove(i)
        stats["wb"] += window * min(len(candidates), m.width)

        # Retire in order; the LQ entry is released here, not at response.
        for _ in range(m.width):
            if head == tail or not ops[head].done:
                break
            op = ops[head]
            op.retire_cycle = cycle
            if op.kind == "load" and fault != "early_lq_release":
                lq -= 1
            sq -= op.kind == "store"
            head += 1
        stats["rob_head_load_blocked"] += (window and head < tail and ops[head].kind == "load"
                                           and not ops[head].done)

        # One AGU: oldest launchable memory op. Stores are always address-ready, so every
        # older store launches before a younger load (no load passes an unknown store
        # address). A ready miss that finds no slot holds the AGU this cycle (replay).
        for i in range(head, tail):
            op = ops[i]
            if op.kind == "alu" or op.launched:
                continue
            if op.kind == "store":
                op.launched = op.done = True
                stats["agu"] += window
                break
            if op.dep is not None and not ops[op.dep].done:
                continue
            if blocking and misses:
                stats["blocking_stall"] += window
                break
            if op.miss and fault != "overissue":
                if misses >= m.mshr:
                    stats["mshr_full"] += window
                    break
                if misses >= m.credits:
                    stats["credit_full"] += window
                    break
            op.launched = True
            latency = m.miss_latency if op.miss else m.hit_latency
            pending.append((cycle + latency, i, op.miss))
            misses += op.miss
            if fault == "early_lq_release":
                lq -= 1
            stats["agu"] += window
            break

        # Dispatch up to two in order, reserving ROB and LQ/SQ entries.
        for _ in range(m.width):
            if tail == n or tail - head >= m.rob:
                break
            op = ops[tail]
            if op.kind == "load" and lq >= m.lq:
                stats["lq_full"] += window
                break
            if op.kind == "store" and sq >= m.sq:
                stats["sq_full"] += window
                break
            lq += op.kind == "load"
            sq += op.kind == "store"
            op.dispatch_cycle = cycle
            tail += 1

        check(m, ops, head, tail, lq, sq, misses, pending, cbuf)
        peak = max(peak, misses) if window else peak
        stats["miss_cycles"] += window * misses
        stats["cycles"] += window
        cycle += 1

    if any(op.retire_cycle is None for op in ops):
        raise ModelError("op left unretired")
    cycles = stats["cycles"] or 1
    result = {"cycles": stats["cycles"], "total_cycles": cycle,
              "ipc": round((n - warmup) / cycles, 4),
              "avg_outstanding_misses": round(stats["miss_cycles"] / cycles, 3),
              "max_outstanding_misses": peak}
    for key in ("agu", "wb"):
        result[f"{key}_per_cycle"] = round(stats[key] / cycles, 4)
    for key in ("lq_full", "sq_full", "mshr_full", "credit_full", "blocking_stall",
                "response_blocked", "rob_head_load_blocked", "wb_congested"):
        result[f"{key}_frac"] = round(stats[key] / cycles, 4)
    return result


def check(m, ops, head, tail, lq, sq, misses, pending, cbuf):
    live = ops[head:tail]
    if tail - head > m.rob:
        raise ModelError("ROB over capacity")
    if lq != sum(op.kind == "load" for op in live) or lq > m.lq:
        raise ModelError(f"LQ count {lq} disagrees with live loads or exceeds {m.lq}")
    if sq != sum(op.kind == "store" for op in live) or sq > m.sq:
        raise ModelError(f"SQ count {sq} disagrees with live stores or exceeds {m.sq}")
    if misses != sum(p[2] for p in pending) or misses > min(m.mshr, m.credits):
        raise ModelError(f"{misses} live misses exceed MSHR {m.mshr} / credits {m.credits}")
    if len(cbuf) > m.completion_buffer:
        raise ModelError("completion buffer over capacity")


def sweep(config):
    base = Machine(**config["machine"])
    points = []
    axes = config["sweep"]
    for name, spec in config["workloads"].items():
        for values in itertools.product(*axes.values()):
            machine = replace(base, **dict(zip(axes, values)))
            ops = workload(config["seed"], config["instructions"], **spec)
            points.append({"workload": name, **dict(zip(axes, values)),
                           **simulate(machine, ops, config["warmup"])})
    return points


def summary(points):
    rows = ["workload           lat cred mshr  ipc8   ipc16  gain%  lq8full  miss8  miss16"]
    key = lambda p: (p["workload"], p["miss_latency"], p["credits"], p["mshr"])
    by = {(key(p), p["lq"]): p for p in points}
    for k in sorted({key(p) for p in points}):
        a, b = by[(k, 8)], by[(k, 16)]
        gain = 100 * (b["ipc"] / a["ipc"] - 1)
        rows.append(f"{k[0]:<18} {k[1]:>3} {k[2]:>4} {k[3]:>4} {a['ipc']:6.3f} {b['ipc']:6.3f}"
                    f" {gain:6.1f} {a['lq_full_frac']:8.3f} {a['avg_outstanding_misses']:6.2f}"
                    f" {b['avg_outstanding_misses']:7.2f}")
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/occupancy_sweep.json")
    parser.add_argument("--out", type=Path, default=ROOT / "out/occupancy/results.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    points = sweep(config)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"config": config, "points": points}, indent=1) + "\n")
    print(summary(points))
    print(f"{len(points)} points -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())
