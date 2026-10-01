#!/usr/bin/env python3
"""Validate the baseline memory protocol and render shared types."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import textwrap


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = (
    "rtl/generated/memory_protocol_pkg.sv",
    "verif/protocol/generated/memory_protocol.py",
)
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


def integer(value: str | int) -> int:
    return value if isinstance(value, int) else int(value, 0)


def validate(config: dict) -> None:
    if config["schema"] != 1 or config["name"] != "single_outstanding_memory_v0":
        raise ValueError("memory protocol identity must be single_outstanding_memory_v0 schema 1")
    if config["xlen"] != 32 or config["line_bytes"] != 32 or config["transaction_id_bits"] != 4:
        raise ValueError("baseline widths must be XLEN32, 32-byte lines, and 4-bit IDs")
    if (config["outstanding_per_port"] != 1 or config["minimum_response_latency_cycles"] != 1
            or config["same_cycle_turnover"] is not False):
        raise ValueError("baseline permits one outstanding request without same-cycle turnover")
    expected_ports = {
        "instruction": {"read": True, "write": False, "cached_line": True, "uncached": False},
        "data": {"read": True, "write": True, "cached_line": True, "uncached": True},
    }
    if config["ports"] != expected_ports:
        raise ValueError("memory port capabilities do not match the baseline")

    expected_request = {
        "transaction_id": 4, "write": 1, "uncached": 1, "address": 32,
        "uncached_size": 2, "line_write_data": 256, "line_write_mask": 32,
        "uncached_write_data": 32, "uncached_write_strobe": 4,
    }
    expected_response = {
        "transaction_id": 4, "status": 2, "line_read_data": 256,
        "uncached_read_data": 32,
    }
    for key, expected in (("request_fields", expected_request), ("response_fields", expected_response)):
        fields = config[key]
        actual = {field["name"]: field["width"] for field in fields}
        if len(actual) != len(fields) or actual != expected:
            raise ValueError(f"{key} names and widths do not match memory protocol v0")
        if any(not IDENTIFIER.fullmatch(field["name"]) for field in fields):
            raise ValueError(f"{key} contains an invalid identifier")
    if config["response_status"] != {"ok": 0, "access_fault": 1, "protocol_error": 2}:
        raise ValueError("response status values do not match memory protocol v0")
    expected_status_effects = {
        "ok": "Complete the live request normally.",
        "access_fault": "Complete the live request with an architectural access fault.",
        "protocol_error": "Raise the sticky platform-fatal path instead of an architectural completion.",
    }
    if config["response_status_effects"] != expected_status_effects:
        raise ValueError("response status effects do not match memory protocol v0")
    if config["uncached_size_encoding"] != {"byte": 0, "halfword": 1, "word": 2}:
        raise ValueError("uncached size values do not match memory protocol v0")

    expected_completion = {
        "data_order": "A write response handshake confirms the write reached the target data-order point.",
        "instruction_visibility": "An instruction read request accepted after a data write response handshake observes that write.",
    }
    if config["completion_points"] != expected_completion:
        raise ValueError("memory completion points do not match protocol v0")
    if config["request_hold_rule"] != "Once request valid is asserted, valid and payload remain stable through the request handshake.":
        raise ValueError("request hold rule does not match memory protocol v0")
    if config["response_hold_rule"] != "Once response valid is asserted, valid and payload remain stable through the response handshake.":
        raise ValueError("response hold rule does not match memory protocol v0")
    if config["id_rule"] != "An accepted request owns its transaction ID until its response handshake; the ID is not reused while owned.":
        raise ValueError("transaction ID rule does not match memory protocol v0")
    if config["reset_rule"] != "Reset clears channel ownership and the target suppresses every response belonging to pre-reset traffic.":
        raise ValueError("reset rule does not match memory protocol v0")
    if config["drop_rule"] != "An accepted cached read may be marked for discard but owns its slot and transaction ID until its response handshake.":
        raise ValueError("drop rule does not match memory protocol v0")


def validate_dependencies(config: dict) -> None:
    platform = json.loads((ROOT / "config/platform.yaml").read_text(encoding="utf-8"))
    if integer(platform["xlen"]) != config["xlen"]:
        raise ValueError("platform and memory protocol XLEN disagree")
    if integer(platform["memory"]["line_bytes"]) != config["line_bytes"]:
        raise ValueError("platform and memory protocol line size disagree")


def sv_field(field: dict) -> str:
    width = field["width"]
    kind = "logic" if width == 1 else f"logic [{width - 1}:0]"
    return f"    {kind} {field['name']};"


def render_sv(config: dict, digest: str) -> str:
    status_width = next(field["width"] for field in config["response_fields"] if field["name"] == "status")
    size_width = next(field["width"] for field in config["request_fields"] if field["name"] == "uncached_size")
    lines = [
        "`default_nettype none",
        "package memory_protocol_pkg;",
        f'  localparam string MEMORY_PROTOCOL_SHA256 = "{digest}";',
        f"  localparam int unsigned MEM_LINE_BYTES = {config['line_bytes']};",
        f"  localparam int unsigned MEM_TRANSACTION_ID_BITS = {config['transaction_id_bits']};",
        f"  typedef enum logic [{status_width - 1}:0] {{",
    ]
    statuses = list(config["response_status"].items())
    for index, (name, value) in enumerate(statuses):
        comma = "," if index + 1 < len(statuses) else ""
        lines.append(f"    MEM_STATUS_{name.upper()} = {status_width}'d{value}{comma}")
    lines.extend(["  } mem_response_status_e;", f"  typedef enum logic [{size_width - 1}:0] {{"])
    sizes = list(config["uncached_size_encoding"].items())
    for index, (name, value) in enumerate(sizes):
        comma = "," if index + 1 < len(sizes) else ""
        lines.append(f"    MEM_SIZE_{name.upper()} = {size_width}'d{value}{comma}")
    lines.extend(["  } mem_uncached_size_e;", "  typedef struct packed {"])
    for field in config["request_fields"]:
        if field["name"] == "uncached_size":
            lines.append("    mem_uncached_size_e uncached_size;")
        else:
            lines.append(sv_field(field))
    lines.extend(["  } mem_request_t;", "  typedef struct packed {"])
    for field in config["response_fields"]:
        if field["name"] == "status":
            lines.append("    mem_response_status_e status;")
        else:
            lines.append(sv_field(field))
    lines.extend([
        "  } mem_response_t;",
        "endpackage",
        "`default_nettype wire",
        "",
    ])
    return "\n".join(lines)


def render_python(config: dict, digest: str) -> str:
    request_widths = {field["name"]: field["width"] for field in config["request_fields"]}
    response_widths = {field["name"]: field["width"] for field in config["response_fields"]}
    request_members = "\n".join(f"    {name}: int = 0" for name in request_widths)
    response_members = "\n".join(f"    {name}: int = 0" for name in response_widths)
    body = f'''\
#!/usr/bin/env python3
"""Generated memory protocol types and single-outstanding monitor."""

from __future__ import annotations

from dataclasses import dataclass


SOURCE_SHA256 = {digest!r}
LINE_BYTES = {config['line_bytes']}
REQUEST_FIELD_WIDTHS = {request_widths!r}
RESPONSE_FIELD_WIDTHS = {response_widths!r}
RESPONSE_STATUS = {config['response_status']!r}
UNCACHED_SIZE = {config['uncached_size_encoding']!r}
PORTS = {config['ports']!r}


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryRequest:
{request_members}


@dataclass(frozen=True)
class MemoryResponse:
{response_members}


@dataclass(frozen=True)
class CycleResult:
    request_accepted: bool = False
    response_accepted: bool = False
    completion: bool = False
    killed: bool = False
    fatal: bool = False
    status: int | None = None


def _known(data: dict, allowed: set[str], context: str) -> None:
    if not isinstance(data, dict):
        raise ProtocolError(f"{{context}} payload must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise ProtocolError(f"{{context}} has unknown fields: {{sorted(unknown)}}")


def _bounded(value: object, width: int, context: str) -> int:
    if type(value) not in (int, bool):
        raise ProtocolError(f"{{context}} must be an integer")
    result = int(value)
    if result < 0 or result >= 1 << width:
        raise ProtocolError(f"{{context}} exceeds {{width}} bits")
    return result


def request_from_dict(data: dict) -> MemoryRequest:
    _known(data, set(REQUEST_FIELD_WIDTHS), "request")
    values = {{name: _bounded(data.get(name, 0), width, f"request {{name}}")
               for name, width in REQUEST_FIELD_WIDTHS.items()}}
    return MemoryRequest(**values)


def response_from_dict(data: dict) -> MemoryResponse:
    _known(data, set(RESPONSE_FIELD_WIDTHS), "response")
    values = {{name: _bounded(data.get(name, 0), width, f"response {{name}}")
               for name, width in RESPONSE_FIELD_WIDTHS.items()}}
    return MemoryResponse(**values)


def _byte_bits(mask: int, lanes: int) -> int:
    result = 0
    for lane in range(lanes):
        if mask & (1 << lane):
            result |= 0xff << (8 * lane)
    return result


def _uncached_strobe(request: MemoryRequest) -> int:
    if request.uncached_size not in UNCACHED_SIZE.values():
        raise ProtocolError("uncached access size must be byte, halfword, or word")
    size = 1 << request.uncached_size
    if request.address % size:
        raise ProtocolError("uncached access must be naturally aligned")
    lane = request.address & 3
    if lane + size > 4:
        raise ProtocolError("uncached access crosses a 32-bit word")
    return ((1 << size) - 1) << lane


def validate_request(port: str, request: MemoryRequest) -> None:
    if port not in PORTS:
        raise ProtocolError(f"unknown memory port: {{port}}")
    for name, width in REQUEST_FIELD_WIDTHS.items():
        _bounded(getattr(request, name), width, f"request {{name}}")
    capabilities = PORTS[port]
    if request.write and not capabilities["write"]:
        raise ProtocolError(f"{{port}} port does not permit writes")
    if request.uncached and not capabilities["uncached"]:
        raise ProtocolError(f"{{port}} port does not permit uncached requests")

    if request.uncached:
        if request.line_write_data or request.line_write_mask:
            raise ProtocolError("uncached request carries cached-line fields")
        strobe = _uncached_strobe(request)
        if request.write:
            if request.uncached_write_strobe != strobe:
                raise ProtocolError("uncached write strobe does not match address and size")
            if request.uncached_write_data & ~_byte_bits(strobe, 4):
                raise ProtocolError("uncached write data violates byte lanes")
        elif request.uncached_write_data or request.uncached_write_strobe:
            raise ProtocolError("uncached read carries write fields")
    else:
        if request.address % LINE_BYTES:
            raise ProtocolError("cached request address must be line aligned")
        if request.uncached_size or request.uncached_write_data or request.uncached_write_strobe:
            raise ProtocolError("cached request carries uncached fields")
        if request.write:
            if request.line_write_mask == 0:
                raise ProtocolError("cached write requires a byte mask")
            if request.line_write_data & ~_byte_bits(request.line_write_mask, LINE_BYTES):
                raise ProtocolError("cached write data violates byte lanes")
        elif request.line_write_data or request.line_write_mask:
            raise ProtocolError("cached read carries write fields")


def validate_response(request: MemoryRequest, response: MemoryResponse) -> None:
    for name, width in RESPONSE_FIELD_WIDTHS.items():
        _bounded(getattr(response, name), width, f"response {{name}}")
    if response.transaction_id != request.transaction_id:
        raise ProtocolError("response transaction ID does not own the live request")
    if response.status not in RESPONSE_STATUS.values():
        raise ProtocolError("response status is unknown")
    if response.status != RESPONSE_STATUS["ok"]:
        if response.line_read_data or response.uncached_read_data:
            raise ProtocolError("error response must carry zero data")
        return
    if request.write:
        if response.line_read_data or response.uncached_read_data:
            raise ProtocolError("write response must carry zero data")
    elif request.uncached:
        if response.line_read_data:
            raise ProtocolError("uncached response carries cached-line data")
        if response.uncached_read_data & ~_byte_bits(_uncached_strobe(request), 4):
            raise ProtocolError("uncached read data violates byte lanes")
    elif response.uncached_read_data:
        raise ProtocolError("cached response carries uncached data")


class SingleOutstandingPort:
    def __init__(self, port: str):
        if port not in PORTS:
            raise ProtocolError(f"unknown memory port: {{port}}")
        self.port = port
        self.outstanding: MemoryRequest | None = None
        self.drop_pending = False
        self._stalled_request: MemoryRequest | None = None
        self._stalled_response: MemoryResponse | None = None

    def step(self, *, reset: bool = False, request_valid: bool = False,
             request_ready: bool = False, request: MemoryRequest | dict | None = None,
             response_valid: bool = False, response_ready: bool = False,
             response: MemoryResponse | dict | None = None, drop: bool = False) -> CycleResult:
        req = request_from_dict(request) if isinstance(request, dict) else request
        rsp = response_from_dict(response) if isinstance(response, dict) else response
        if request_valid != (req is not None):
            raise ProtocolError("request valid and payload presence disagree")
        if response_valid != (rsp is not None):
            raise ProtocolError("response valid and payload presence disagree")
        if reset:
            if request_valid or response_valid or drop:
                raise ProtocolError("reset cycle must not carry protocol activity")
            self.outstanding = None
            self.drop_pending = False
            self._stalled_request = None
            self._stalled_response = None
            return CycleResult()

        if self._stalled_request is not None and (not request_valid or req != self._stalled_request):
            raise ProtocolError("request payload changed while stalled")
        if self._stalled_response is not None and (not response_valid or rsp != self._stalled_response):
            raise ProtocolError("response payload changed while stalled")
        if req is not None:
            validate_request(self.port, req)
        if self.outstanding is not None and request_ready:
            raise ProtocolError("single-outstanding port advertised request ready while occupied")
        if rsp is not None:
            if self.outstanding is None:
                raise ProtocolError("response has no live transaction owner")
            validate_response(self.outstanding, rsp)
        if drop:
            if self.outstanding is None or self.outstanding.write or self.outstanding.uncached:
                raise ProtocolError("only an accepted cached read may be dropped")
            self.drop_pending = True

        request_accepted = request_valid and request_ready
        response_accepted = response_valid and response_ready
        if request_accepted and self.outstanding is not None:
            raise ProtocolError("single-outstanding port accepted a second request")
        self._stalled_request = req if request_valid and not request_ready else None
        self._stalled_response = rsp if response_valid and not response_ready else None

        completion = False
        killed = False
        fatal = False
        status = None
        if response_accepted:
            killed = self.drop_pending
            status = rsp.status
            fatal = status == RESPONSE_STATUS["protocol_error"]
            completion = not killed and not fatal
            self.outstanding = None
            self.drop_pending = False
        if request_accepted:
            self.outstanding = req
        return CycleResult(request_accepted, response_accepted, completion, killed, fatal, status)
'''
    return textwrap.dedent(body)


def render(config: dict, digest: str) -> dict[str, str]:
    return {
        OUTPUTS[0]: render_sv(config, digest),
        OUTPUTS[1]: render_python(config, digest),
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
    validate_dependencies(config)
    outputs = render(config, hashlib.sha256(source).hexdigest())
    stale = False
    for relative, content in outputs.items():
        path = ROOT / relative
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            print(f"stale generated file: {relative}")
            stale = True
    if not stale:
        print("memory-protocol views: PASS")
    return int(stale)


if __name__ == "__main__":
    raise SystemExit(main())
