#!/usr/bin/env python3
"""Normalize the pinned Spike commit log for the lockstep smoke program."""

from __future__ import annotations

import re


COMMIT = re.compile(
    r"^core\s+\d+:\s+([0-3])\s+(0x[0-9a-f]+)\s+\((0x[0-9a-f]+)\)(.*)$"
)
DESTINATION = re.compile(r"\bx(\d+)\s+(0x[0-9a-f]+)")


class SpikeLogError(ValueError):
    pass


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def _branch_offset(instruction: int) -> int:
    immediate = (
        ((instruction >> 31) & 1) << 12
        | ((instruction >> 7) & 1) << 11
        | ((instruction >> 25) & 0x3f) << 5
        | ((instruction >> 8) & 0xf) << 1
    )
    return _sign_extend(immediate, 13)


def _decode_sources(instruction: int) -> tuple[int, int]:
    opcode = instruction & 0x7f
    if opcode == 0x13:
        return (instruction >> 15) & 0x1f, 0
    if opcode in (0x33, 0x63):
        return (instruction >> 15) & 0x1f, (instruction >> 20) & 0x1f
    raise SpikeLogError(f"smoke adapter does not support opcode 0x{opcode:02x}")


def _next_pc(pc: int, instruction: int, rs1_value: int, rs2_value: int) -> int:
    opcode = instruction & 0x7f
    if opcode != 0x63:
        return pc + 4
    funct3 = (instruction >> 12) & 7
    if funct3 != 0:
        raise SpikeLogError("smoke adapter supports BEQ only")
    return pc + (_branch_offset(instruction) if rs1_value == rs2_value else 4)


def parse_spike_log(output: str, address_bias: int, region_size: int) -> list[dict]:
    registers = [0] * 32
    events = []
    for line in output.splitlines():
        match = COMMIT.match(line)
        if match is None:
            continue
        privilege, pc_text, instruction_text, tail = match.groups()
        pc = int(pc_text, 16)
        if not address_bias <= pc < address_bias + region_size:
            continue
        instruction = int(instruction_text, 16)
        rs1_addr, rs2_addr = _decode_sources(instruction)
        rs1_value = registers[rs1_addr]
        rs2_value = registers[rs2_addr]
        destination = DESTINATION.search(tail)
        rd_addr = 0
        rd_value = 0
        rd_mask = 0
        if destination is not None:
            rd_addr = int(destination.group(1))
            rd_value = int(destination.group(2), 16)
            encoded_rd = (instruction >> 7) & 0x1f
            if rd_addr != encoded_rd or rd_addr == 0:
                raise SpikeLogError("Spike destination disagrees with the instruction encoding")
            rd_mask = 0xffffffff
        event = {
            "valid": 1,
            "order": len(events),
            "instruction": instruction,
            "privilege": int(privilege),
            "pc_before": pc - address_bias,
            "pc_after": _next_pc(pc, instruction, rs1_value, rs2_value) - address_bias,
            "rs1_addr": rs1_addr,
            "rs1_value": rs1_value,
            "rs2_addr": rs2_addr,
            "rs2_value": rs2_value,
            "rd_addr": rd_addr,
            "rd_value": rd_value,
            "rd_write_mask": rd_mask,
            "retired": 1,
        }
        events.append(event)
        if rd_addr:
            registers[rd_addr] = rd_value
    return [{"slots": [*events[index:index + 2], *({} for _ in range(2 - len(events[index:index + 2])))]}
            for index in range(0, len(events), 2)]


SPIKE_LINE = re.compile(r"^core\s+0: (?:(3) )?0x([0-9a-f]+) \(0x([0-9a-f]+)\)(.*)$")
EXCEPTION = re.compile(r"^core\s+0: exception (\w+), epc 0x([0-9a-f]+)$")
TVAL = re.compile(r"^core\s+0:\s+tval 0x([0-9a-f]+)$")
EFFECT = re.compile(r"\b(x\d+|c\d+_\w+|mem)\s+0x([0-9a-f]+)(?:\s+0x([0-9a-f]+))?")
CAUSES = {"trap_instruction_address_misaligned": 0, "trap_instruction_access_fault": 1,
          "trap_illegal_instruction": 2, "trap_breakpoint": 3, "trap_load_address_misaligned": 4,
          "trap_load_access_fault": 5, "trap_store_address_misaligned": 6,
          "trap_store_access_fault": 7, "trap_machine_ecall": 11}


def spike_sail_trace(output: str, memory: dict, skip: int, substitutions: dict,
                     identity_reads: dict, mstatus: int) -> str:
    """Rewrite a pinned Spike -l --log-commits log into the Sail step format parse_sail_log reads.

    Spike omits load data for x0 destinations, read-only CSR values and trap CSR writes, so this
    adapter supplies them: loads from a byte memory updated by Spike's own stores, CSR reads from the
    destination value, and trap mstatus from the platform MIE/MPIE rule. Everything else is Spike's.
    """
    records = []
    pending = None
    for line in output.splitlines():
        if match := SPIKE_LINE.match(line):
            committed, pc, ins, tail = match.groups()
            pc, ins = int(pc, 16), int(ins, 16)
            if not committed:
                if pending is not None:
                    raise SpikeLogError(f"instruction at 0x{pending[0]:x} has no commit or exception")
                pending = (pc, ins)
                continue
            # Spike prints no fetch record when an instruction branches to itself.
            repeat = pending is None and records and (records[-1]["pc"], records[-1]["ins"]) == (pc, ins)
            if pending != (pc, ins) and not repeat:
                raise SpikeLogError(f"commit at 0x{pc:x} without its fetch record")
            records.append({"pc": pc, "ins": ins, "effects": EFFECT.findall(tail)})
            pending = None
        elif match := EXCEPTION.match(line):
            name, epc = match.group(1), int(match.group(2), 16)
            if name not in CAUSES:
                raise SpikeLogError(f"unsupported Spike exception {name}")
            if pending is None:
                if CAUSES[name] != 1:
                    raise SpikeLogError("unframed Spike exception is not an instruction access fault")
                records.append({"pc": epc, "ins": None, "cause": 1, "tval": 0})
            elif pending[0] != epc:
                raise SpikeLogError("Spike exception epc differs from the trapping instruction")
            else:
                records.append({"pc": epc, "ins": pending[1], "cause": CAUSES[name], "tval": 0})
            pending = None
        elif match := TVAL.match(line):
            if not records or "cause" not in records[-1]:
                raise SpikeLogError("tval without a Spike exception")
            records[-1]["tval"] = int(match.group(1), 16)
    if len(records) <= skip:
        raise SpikeLogError("Spike log ends inside the setup prefix")
    blocks = []
    for order, record in enumerate(records[skip:]):
        pc, ins = record["pc"], record["ins"]
        lines = []
        if ins is not None:
            ins = substitutions.get(pc, ins)
            lines.append(f"[{order}] [M]: 0x{pc:08X} (0x{ins:08X}) spike")
        if "cause" in record:
            mstatus = (mstatus & ~0x88) | ((mstatus & 8) << 4)
            lines += [f"trapping from M to M to handle {record['cause']}",
                      f"CSR mstatus (0x300) <- 0x{mstatus:08X}",
                      f"CSR mcause (0x342) <- 0x{record['cause']:08X}",
                      f"CSR mtval (0x343) <- 0x{record['tval']:08X}",
                      f"CSR mepc (0x341) <- 0x{pc:08X}"]
            blocks.append("\n".join(lines))
            continue
        op, f3, rd = ins & 0x7f, (ins >> 12) & 7, (ins >> 7) & 31
        csr_address = ins >> 20 if op == 0x73 and f3 & 3 else None
        written = set()
        destination = loaded = None
        for name, first, second in record["effects"]:
            value = int(first, 16)
            if name.startswith("x"):
                destination = (int(name[1:]), value)
            elif name.startswith("c"):
                address = int(name[1:].split("_")[0])
                if address == 0x310 and csr_address != 0x310:
                    if value:
                        raise SpikeLogError("Spike reported a non-zero mstatush side effect")
                    continue
                if address == 0x300:
                    mstatus = value
                written.add(address)
                lines.append(f"CSR spike (0x{address:03X}) <- 0x{value:08X}")
            elif op == 0x23:
                size = 1 << (f3 & 3)
                data = int(second, 16)
                for n in range(size):
                    memory[value + n] = (data >> (8 * n)) & 0xff
                lines.append(f"mem[W,0x{value:08X}] <- 0x{data:0{2 * size}X}")
            elif op == 0x03:
                size = 1 << (f3 & 3)
                data = sum(memory.get(value + n, 0) << (8 * n) for n in range(size))
                lines.append(f"mem[R,0x{value:08X}] -> 0x{data:0{2 * size}X}")
                loaded = data if f3 & 4 or size == 4 else _sign_extend(data, 8 * size) & 0xffffffff
            else:
                raise SpikeLogError(f"unexpected Spike memory effect at 0x{pc:x}")
        if op == 0x03 and rd and (destination is None or destination[1] != loaded):
            raise SpikeLogError(f"Spike load at 0x{pc:x} disagrees with its own stores")
        if destination is not None:
            number, value = destination
            if csr_address is not None and csr_address not in written:
                if identity_reads.get(csr_address, (None,))[0] == value:
                    value = identity_reads[csr_address][1]
                lines.append(f"CSR spike (0x{csr_address:03X}) -> 0x{value:08X}")
            lines.append(f"x{number} <- 0x{value:08X}")
        if ins == 0x30200073:
            lines.append("ret-ing from M to M")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\n"


def relocate(events: list[dict], bias: int, size: int) -> list[dict]:
    """Map Spike's relocated BRAM addresses back to platform addresses, including LUI immediates."""
    move = lambda value: value - bias if bias <= value < bias + size else value
    for event in events:
        for field in ("pc_before", "pc_after", "rs1_value", "rs2_value", "rd_value", "mem_address",
                      "mem_read_data", "mem_write_data", "trap_value"):
            if field in event:
                event[field] = move(event[field])
        for effect in event["csr_effects"]:
            effect["old_value"] = move(effect["old_value"])
            effect["new_value"] = move(effect["new_value"])
        if event["instruction"] & 0x7f == 0x37:
            event["instruction"] = move(event["instruction"] & 0xfffff000) | (event["instruction"] & 0xfff)
    return events
