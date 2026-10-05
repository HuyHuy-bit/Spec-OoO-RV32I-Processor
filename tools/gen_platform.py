#!/usr/bin/env python3
"""Validate the platform source and render its consumed views."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = {
    "rtl/generated/platform_pkg.sv": "sv",
    "sw/include/platform.h": "c",
    "sw/link/platform.ld": "ld",
    "model/generated/platform.json": "json",
}


def integer(value: str | int) -> int:
    return value if isinstance(value, int) else int(value, 0)


def identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", value).upper()


def validate(config: dict) -> None:
    if config["schema"] != 1 or config["xlen"] != 32:
        raise ValueError("platform schema and XLEN must be 1 and 32")
    isa = config["isa"]
    if isa["base"] != "RV32I" or isa["extensions"] != ["Zicsr", "Zifencei"]:
        raise ValueError("ISA must be RV32I_Zicsr_Zifencei")
    if isa["privilege_modes"] != ["M"] or isa["ialign_bits"] != 32:
        raise ValueError("only M-mode with IALIGN=32 is supported")
    if isa["endianness"] != "little" or isa["interrupts_enabled"]:
        raise ValueError("baseline must be little-endian with interrupts disabled")

    line_bytes = integer(config["memory"]["line_bytes"])
    if line_bytes <= 0 or line_bytes & (line_bytes - 1):
        raise ValueError("cache-line size must be a positive power of two")
    regions = sorted(config["memory"]["regions"], key=lambda item: integer(item["base"]))
    names = set()
    previous_end = 0
    for region in regions:
        base = integer(region["base"])
        size = integer(region["size"])
        if region["name"] in names or size <= 0 or base + size > 1 << 32:
            raise ValueError("memory region name, size, or range is invalid")
        if names and base < previous_end:
            raise ValueError("memory regions overlap")
        if region["cacheable"] and (base % line_bytes or size % line_bytes):
            raise ValueError("cacheable region must end on cache-line boundaries")
        if not region["cacheable"] and (region["execute"] or region["idempotent"]):
            raise ValueError("MMIO must be non-executable and non-idempotent")
        names.add(region["name"])
        previous_end = base + size

    for field in ("pc", "mtvec"):
        address = integer(config["reset"][field])
        if address % 4 or not any(
            integer(region["base"]) <= address < integer(region["base"]) + integer(region["size"])
            and region["execute"] for region in regions
        ):
            raise ValueError(f"reset {field} must be aligned executable memory")

    csr_names = set()
    csr_addresses = set()
    for csr in config["csrs"]:
        address = integer(csr["address"])
        reset = integer(csr["reset"])
        write_mask = integer(csr["write_mask"])
        fixed_mask = integer(csr["fixed_mask"])
        fixed_value = integer(csr["fixed_value"])
        if csr["name"] in csr_names or address in csr_addresses or address >= 1 << 12:
            raise ValueError("CSR name or address is invalid or duplicated")
        if csr["access"] not in ("ro", "rw") or (csr["access"] == "ro" and write_mask):
            raise ValueError("CSR access and write mask disagree")
        if write_mask & fixed_mask or fixed_value & ~fixed_mask:
            raise ValueError("CSR writable and fixed fields disagree")
        if reset != ((reset & write_mask) | fixed_value):
            raise ValueError("CSR reset violates its WARL mask")
        csr_names.add(csr["name"])
        csr_addresses.add(address)

    causes = [integer(trap["cause"]) for trap in config["synchronous_traps"]]
    if len(causes) != len(set(causes)) or any(cause >= 1 << 31 for cause in causes):
        raise ValueError("trap causes must be unique synchronous codes")


def render_sv(config: dict, digest: str) -> str:
    lines = [
        "`default_nettype none",
        "package platform_pkg;",
        f'  localparam string PLATFORM_SHA256 = "{digest}";',
        "  localparam int unsigned XLEN = 32;",
        f"  localparam logic [31:0] RESET_PC = 32'h{integer(config['reset']['pc']):08x};",
        f"  localparam logic [31:0] RESET_MTVEC = 32'h{integer(config['reset']['mtvec']):08x};",
        f"  localparam int unsigned LINE_BYTES = {integer(config['memory']['line_bytes'])};",
    ]
    for region in config["memory"]["regions"]:
        name = identifier(region["name"])
        lines.extend([
            f"  localparam logic [31:0] {name}_BASE = 32'h{integer(region['base']):08x};",
            f"  localparam logic [31:0] {name}_SIZE = 32'h{integer(region['size']):08x};",
            f"  localparam bit {name}_READ = {int(region['read'])};",
            f"  localparam bit {name}_WRITE = {int(region['write'])};",
            f"  localparam bit {name}_EXECUTE = {int(region['execute'])};",
            f"  localparam bit {name}_CACHEABLE = {int(region['cacheable'])};",
            f"  localparam bit {name}_IDEMPOTENT = {int(region['idempotent'])};",
        ])
    for csr in config["csrs"]:
        name = identifier(csr["name"])
        lines.extend([
            f"  localparam logic [11:0] CSR_{name} = 12'h{integer(csr['address']):03x};",
            f"  localparam logic [31:0] CSR_{name}_RESET = 32'h{integer(csr['reset']):08x};",
            f"  localparam logic [31:0] CSR_{name}_WRITE_MASK = 32'h{integer(csr['write_mask']):08x};",
            f"  localparam logic [31:0] CSR_{name}_FIXED_MASK = 32'h{integer(csr['fixed_mask']):08x};",
            f"  localparam logic [31:0] CSR_{name}_FIXED_VALUE = 32'h{integer(csr['fixed_value']):08x};",
        ])
    # Index-ordered CSR tables; entry i is config["csrs"][i] (packed arrays list MSB first).
    names = [identifier(csr["name"]) for csr in reversed(config["csrs"])]
    lines.append(f"  localparam int CSR_COUNT = {len(names)};")
    for table, width, suffix in [("ADDRESSES", 12, ""), ("RESETS", 32, "_RESET"), ("WRITE_MASKS", 32, "_WRITE_MASK"),
                                 ("FIXED_MASKS", 32, "_FIXED_MASK"), ("FIXED_VALUES", 32, "_FIXED_VALUE")]:
        values = ", ".join(f"CSR_{name}{suffix}" for name in names)
        lines.append(f"  localparam logic [CSR_COUNT-1:0][{width - 1}:0] CSR_{table} = {{{values}}};")
    read_only = "".join("1" if csr["access"] == "ro" else "0" for csr in reversed(config["csrs"]))
    lines.append(f"  localparam logic [CSR_COUNT-1:0] CSR_READ_ONLY = {len(names)}'b{read_only};")
    lines.extend(f"  localparam int CSR_INDEX_{identifier(csr['name'])} = {index};"
                 for index, csr in enumerate(config["csrs"]))
    for trap in config["synchronous_traps"]:
        lines.append(
            f"  localparam logic [31:0] CAUSE_{identifier(trap['name'])} = 32'd{integer(trap['cause'])};"
        )
    lines.extend(["endpackage", "`default_nettype wire", ""])
    return "\n".join(lines)


def render_c(config: dict, digest: str) -> str:
    lines = [
        "#ifndef SPEC_OOO_PLATFORM_H",
        "#define SPEC_OOO_PLATFORM_H",
        f'#define PLATFORM_SHA256 "{digest}"',
        f"#define PLATFORM_RESET_PC 0x{integer(config['reset']['pc']):08x}u",
        f"#define PLATFORM_RESET_MTVEC 0x{integer(config['reset']['mtvec']):08x}u",
    ]
    for region in config["memory"]["regions"]:
        name = identifier(region["name"])
        lines.extend([
            f"#define PLATFORM_{name}_BASE 0x{integer(region['base']):08x}u",
            f"#define PLATFORM_{name}_SIZE 0x{integer(region['size']):08x}u",
        ])
    for csr in config["csrs"]:
        name = identifier(csr["name"])
        lines.extend([
            f"#define PLATFORM_CSR_{name} 0x{integer(csr['address']):03x}u",
            f"#define PLATFORM_CSR_{name}_RESET 0x{integer(csr['reset']):08x}u",
            f"#define PLATFORM_CSR_{name}_WRITE_MASK 0x{integer(csr['write_mask']):08x}u",
            f"#define PLATFORM_CSR_{name}_FIXED_MASK 0x{integer(csr['fixed_mask']):08x}u",
            f"#define PLATFORM_CSR_{name}_FIXED_VALUE 0x{integer(csr['fixed_value']):08x}u",
        ])
    lines.extend(["#endif", ""])
    return "\n".join(lines)


def render_ld(config: dict, digest: str) -> str:
    ram = next(region for region in config["memory"]["regions"] if region["name"] == "bram")
    return "\n".join([
        f"/* platform-sha256: {digest} */",
        "ENTRY(_start)",
        "MEMORY",
        "{",
        f"  ram (rwx) : ORIGIN = 0x{integer(ram['base']):08x}, LENGTH = 0x{integer(ram['size']):08x}",
        "}",
        "SECTIONS",
        "{",
        "  . = ORIGIN(ram);",
        "  .text : { KEEP(*(.text.init)) *(.text .text.*) } > ram",
        "  .rodata : { *(.rodata .rodata.*) } > ram",
        "  .data : { *(.data .data.*) } > ram",
        "  .bss : { *(.bss .bss.* COMMON) } > ram",
        "  . = ALIGN(16);",
        "  _end = .;",
        "  _stack_top = ORIGIN(ram) + LENGTH(ram);",
        "}",
        "",
    ])


def render(config: dict, digest: str) -> dict[str, str]:
    model = {"source_sha256": digest, **config}
    return {
        "rtl/generated/platform_pkg.sv": render_sv(config, digest),
        "sw/include/platform.h": render_c(config, digest),
        "sw/link/platform.ld": render_ld(config, digest),
        "model/generated/platform.json": json.dumps(model, indent=2) + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = args.input.read_bytes()
    config = json.loads(source)
    validate(config)
    outputs = render(config, hashlib.sha256(source).hexdigest())
    failed = False
    for relative, content in outputs.items():
        path = ROOT / relative
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            print(f"stale generated file: {relative}")
            failed = True
    if not failed:
        print("platform views: PASS")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
