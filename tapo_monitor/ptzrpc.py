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


def diagnose(exc):
    """Fixed category for a failed RPC: never the exception text, which can carry
    the camera's address or a session token.

    One of 'connect timeout', 'read timeout', 'authentication failure',
    'connection refused' or 'other'.
    """
    from requests.exceptions import ConnectTimeout, Timeout

    from .camera import is_authentication_error

    chain, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    if any(isinstance(e, ConnectTimeout) for e in chain):
        return "connect timeout"
    if any(isinstance(e, (Timeout, TimeoutError)) for e in chain):
        return "read timeout"
    if any(isinstance(e, ConnectionRefusedError) for e in chain):
        return "connection refused"
    if is_authentication_error(exc) or any(
            "not authorized" in str(e).lower() or "unauthorized" in str(e).lower()
            for e in chain):
        return "authentication failure"
    return "other"


class AdaptiveBudget:
    """Time budget for an idempotent Stop that stretches after a failed Stop.

    Base budget until a Stop fails; then ``escalated`` (never above ``CAP``) until
    ``restore_after`` consecutive successes or ``quiet_s`` seconds without a failure.
    The failure itself was made with the base budget. Only Stop may use this: a
    position read must stay within the guard's stale_after, a late Stop ACK is valid.
    """

    CAP = 2.0

    def __init__(self, base=0.8, escalated=1.6, restore_after=3, quiet_s=30.0,
                 clock=time.monotonic):
        self.base = min(float(base), self.CAP)
        self.escalated = min(max(float(escalated), self.base), self.CAP)
        self.restore_after, self.quiet_s, self.clock = restore_after, quiet_s, clock
        self._active = False
        self._successes = 0
        self._last_failure = None

    def _expire(self):
        if self._active and self.clock() - self._last_failure >= self.quiet_s:
            self._active, self._successes = False, 0

    @property
    def is_escalated(self):
        self._expire()
        return self._active

    def budget(self):
        return self.escalated if self.is_escalated else self.base

    def record(self, ok):
        self._expire()
        if not ok:
            self._active, self._successes = True, 0
            self._last_failure = self.clock()
        elif self._active:
            self._successes += 1
            if self._successes >= self.restore_after:
                self._active, self._successes = False, 0


def call(ptz, operation, *, budget=0.8, clock=time.monotonic, adaptive=None):
    """Call a position read or Stop, retrying transient connection/read failures.

    These operations are idempotent. Do not wrap relative movement or a toggle.
    The camera may close each HTTP connection: a short connect timeout permits a
    fresh connection attempt without waiting out the entire old RPC timeout.
    All attempts share a deadline shorter than the old 0.5s connect + 0.5s read
    timeouts combined; failure is still propagated to the guard's latch.
    Only the observer thread owns this transport. Restore its timeout afterwards.

    With ``adaptive`` (Stop only) the budget comes from it and the outcome is fed back.
    An extended budget uses two attempts, a short one and a final one that owns all
    remaining time, so the extra time is usable by a late ACK instead of idling.
    """
    attempts = 4
    if adaptive is not None:
        budget = adaptive.budget()
        if adaptive.is_escalated:
            attempts = 2
        try:
            result = _call(ptz, operation, budget, clock, attempts)
        except Exception:
            adaptive.record(False)
            raise
        adaptive.record(True)
        return result
    return _call(ptz, operation, budget, clock, 4)


def _call(ptz, operation, budget, clock, attempts):
    started = clock()
    deadline = started + budget
    transport = ptz.zeep_client.transport
    original = transport.operation_timeout
    phase = None
    diag = {"attempts": 0, "errors": []}
    try:
        for attempt in range(attempts):
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("PTZ RPC deadline exceeded")
            connect = min(0.1, remaining / 3)
            if attempt == attempts - 1 and phase == "connect":
                connect = remaining - min(0.05, remaining / 4)
            read = min(0.25, remaining - connect) if attempt < attempts - 1 else remaining - connect
            transport.operation_timeout = (connect, read)
            diag["attempts"] = attempt + 1
            try:
                result = operation()
            except Exception as exc:
                phase = _transport_phase(exc)
                diag["errors"].append(diagnose(exc))
                if attempt == attempts - 1 or phase is None:
                    raise
            else:
                if clock() > deadline:
                    raise TimeoutError("PTZ RPC deadline exceeded")
                return result
    except Exception as exc:
        # Debug context for the guard's trace: how many attempts, which phase failed.
        diag["elapsed_s"] = round(clock() - started, 3)
        try:
            exc.rpc_diag = diag
        except Exception:  # noqa: BLE001 - exceptions without __dict__ keep no context
            pass
        raise
    finally:
        transport.operation_timeout = original
