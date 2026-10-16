"""Retry idempotent endstop RPCs inside a single bounded time budget."""

import time


def _transport_phase(exc):
    # ONVIF wraps requests errors, retaining the original exception as its context.
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import ConnectTimeout, Timeout

    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ConnectTimeout, RequestsConnectionError, ConnectionError)):
            return "connect"
        if isinstance(exc, (Timeout, TimeoutError)):
            return "read"
        exc = exc.__cause__ or exc.__context__
    return None


def call(ptz, operation, *, budget=0.8, clock=time.monotonic):
    """Call a position read or Stop, retrying transient connection/read failures.

    These operations are idempotent. Do not wrap relative movement or a toggle.
    The camera may close each HTTP connection: a short connect timeout permits a
    fresh connection attempt without waiting out the entire old RPC timeout.
    All attempts share a deadline shorter than the old 0.5s connect + 0.5s read
    timeouts combined; failure is still propagated to the guard's latch.
    Only the observer thread owns this transport. Restore its timeout afterwards.
    """
    deadline = clock() + budget
    transport = ptz.zeep_client.transport
    original = transport.operation_timeout
    phase = None
    try:
        for attempt in range(4):
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("PTZ RPC deadline exceeded")
            connect = min(0.1, remaining / 3)
            if attempt == 3 and phase == "connect":
                connect = remaining - min(0.05, remaining / 4)
            read = min(0.25, remaining - connect) if attempt < 3 else remaining - connect
            transport.operation_timeout = (connect, read)
            try:
                result = operation()
            except Exception as exc:
                phase = _transport_phase(exc)
                if attempt == 3 or phase is None:
                    raise
            else:
                if clock() > deadline:
                    raise TimeoutError("PTZ RPC deadline exceeded")
                return result
    finally:
        transport.operation_timeout = original
