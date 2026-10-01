#!/usr/bin/env python3
"""Generated memory protocol types and single-outstanding monitor."""

from __future__ import annotations

from dataclasses import dataclass


SOURCE_SHA256 = '64b3225a7572fb45f680f157bcea878e038ed935be1148dacf8fd0c44e4e4b94'
LINE_BYTES = 32
REQUEST_FIELD_WIDTHS = {'transaction_id': 4, 'write': 1, 'uncached': 1, 'address': 32, 'uncached_size': 2, 'line_write_data': 256, 'line_write_mask': 32, 'uncached_write_data': 32, 'uncached_write_strobe': 4}
RESPONSE_FIELD_WIDTHS = {'transaction_id': 4, 'status': 2, 'line_read_data': 256, 'uncached_read_data': 32}
RESPONSE_STATUS = {'ok': 0, 'access_fault': 1, 'protocol_error': 2}
UNCACHED_SIZE = {'byte': 0, 'halfword': 1, 'word': 2}
PORTS = {'instruction': {'read': True, 'write': False, 'cached_line': True, 'uncached': False}, 'data': {'read': True, 'write': True, 'cached_line': True, 'uncached': True}}


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryRequest:
    transaction_id: int = 0
    write: int = 0
    uncached: int = 0
    address: int = 0
    uncached_size: int = 0
    line_write_data: int = 0
    line_write_mask: int = 0
    uncached_write_data: int = 0
    uncached_write_strobe: int = 0


@dataclass(frozen=True)
class MemoryResponse:
    transaction_id: int = 0
    status: int = 0
    line_read_data: int = 0
    uncached_read_data: int = 0


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
        raise ProtocolError(f"{context} payload must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise ProtocolError(f"{context} has unknown fields: {sorted(unknown)}")


def _bounded(value: object, width: int, context: str) -> int:
    if type(value) not in (int, bool):
        raise ProtocolError(f"{context} must be an integer")
    result = int(value)
    if result < 0 or result >= 1 << width:
        raise ProtocolError(f"{context} exceeds {width} bits")
    return result


def request_from_dict(data: dict) -> MemoryRequest:
    _known(data, set(REQUEST_FIELD_WIDTHS), "request")
    values = {name: _bounded(data.get(name, 0), width, f"request {name}")
               for name, width in REQUEST_FIELD_WIDTHS.items()}
    return MemoryRequest(**values)


def response_from_dict(data: dict) -> MemoryResponse:
    _known(data, set(RESPONSE_FIELD_WIDTHS), "response")
    values = {name: _bounded(data.get(name, 0), width, f"response {name}")
               for name, width in RESPONSE_FIELD_WIDTHS.items()}
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
        raise ProtocolError(f"unknown memory port: {port}")
    for name, width in REQUEST_FIELD_WIDTHS.items():
        _bounded(getattr(request, name), width, f"request {name}")
    capabilities = PORTS[port]
    if request.write and not capabilities["write"]:
        raise ProtocolError(f"{port} port does not permit writes")
    if request.uncached and not capabilities["uncached"]:
        raise ProtocolError(f"{port} port does not permit uncached requests")

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
        _bounded(getattr(response, name), width, f"response {name}")
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
            raise ProtocolError(f"unknown memory port: {port}")
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
