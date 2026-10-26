"""Connect the virtual endstop to existing camera sessions and scheduled control."""

import logging
import os
from dataclasses import replace

from . import camera, panlimit, ptzrpc, sessionguard, stopguard

log = logging.getLogger(__name__)


def enabled(cfg):
    return cfg.pan_limit.enabled and cfg.pan_limit.mode == "stop"


class StopControl:
    """One camera's guard, pose connection and safe recovery presets.

    The ONVIF observer never logs in to the native API. Its OFF callback uses whichever
    protected native client the main daemon currently owns. A retired reference refuses
    the operation, so reconnecting cannot revive a closed session.
    """

    def __init__(self, cfg, get_client, *, build_ptz=None, guard_factory=None, suspended=None,
                 recall=None):
        self.cfg, self.get_client = cfg, get_client
        self.recall = recall            # recall(client, preset) -> bool; the daemon's safe recall
        self._plan_preset = None
        self.build_ptz = build_ptz or (
            lambda *args: panlimit.build_ptz(*args, timeout=1))
        self.guard_factory = guard_factory or stopguard.StopGuard
        self.suspended = suspended or (lambda: False)
        self.guard = None
        self.host = None
        self.positions = {}
        self.inner_low = self.inner_high = None
        self.last_reason = None

    def _note(self, kind, detail):
        # Debug trace only; a guard without it (or failing to take it) never matters.
        note = getattr(self.guard, "note", None)
        if note is not None:
            try:
                note(kind, detail)
            except Exception:  # noqa: BLE001
                pass

    def allowed(self):
        return self.guard is not None and self.guard.snapshot().tracking_allowed

    def attach(self, client):
        sessionguard.protect_client(client, self.allowed)
        client._rotation_stop_host = camera.active_host(self.cfg)

    def inhibition(self):
        """Describe a safety-owned OFF at observation time, never a later guess."""
        if self.guard is None:
            return {"active": True, "reason": "starting", "healthy": False}
        snap = self.guard.snapshot()
        if snap.tracking_allowed:
            return None
        return {"active": True, "reason": snap.reason, "healthy": snap.healthy}

    def recover_home(self):
        """Recall the verified safe home view now instead of at the next control pass."""
        client = self.get_client()
        if client is None or self.recall is None:
            return False
        candidates = (self._plan_preset, self.cfg.tracking.day_preset,
                      self.cfg.tracking.night_preset)
        home = next((p for p in candidates if p is not None and self.safe_preset(p)), None)
        return home is not None and bool(self.recall(client, home))

    def disable_tracking(self):
        client = self.get_client()
        diagnostics = {"result": "missing", "lock_wait_s": 0.0, "request_s": 0.0}
        ok = client is not None and sessionguard.native_tracking_off(client, diagnostics=diagnostics)
        log.info("PTZ stop guard tracking OFF %s: result=%s client_unavailable=%s "
                 "lock_wait=%.3fs request=%.3fs set=%.3fs error=%s", self.cfg.name,
                 diagnostics["result"], diagnostics["result"] in {"missing", "retired"},
                 diagnostics["lock_wait_s"], diagnostics["request_s"],
                 diagnostics.get("set_s", 0.0), diagnostics.get("error", "none"))
        return ok

    def start(self):
        host = camera.active_host(self.cfg)
        if self.guard is not None and host == self.host:
            return
        if self.guard is not None:
            self.close()
        pl = self.cfg.pan_limit
        ptz, token = self.build_ptz(host, pl.onvif_port,
                                    os.getenv(pl.onvif_user_env or "", ""),
                                    os.getenv(pl.onvif_password_env or "", ""))
        # One bounded ONVIF request at a time. A timeout is loss of pose, not a fresh
        # position at the last observed angle.
        ptz.zeep_client.transport.operation_timeout = 0.5
        presets = ptz.GetPresets({"ProfileToken": token})
        positions = {}
        for p in presets or ():
            pos = getattr(p, "PTZPosition", None)
            xy = getattr(pos, "PanTilt", None) if pos is not None else None
            if xy is not None and xy.x is not None:
                positions[str(p.token)] = float(xy.x)
        right = positions[pl.right_preset]
        left = positions[pl.left_preset] if pl.left_preset is not None else None
        self.inner_high = right - pl.stop_margin
        self.inner_low = None if left is None else left + pl.stop_margin
        self.positions = positions
        # Every scheduled return must remain inside the trigger range; otherwise the
        # main control pass would fight the endstop or rearm at an unsafe view.
        for preset in (self.cfg.tracking.day_preset, self.cfg.tracking.night_preset):
            if preset is not None and not self.safe_preset(preset):
                raise ValueError("scheduled preset is not inside the stop guard range")
        # Stop is idempotent and a late ACK is valid, so only it gets an adaptive budget;
        # position reads keep 0.8 s because the guard rejects a pose older than stale_after.
        stop_budget = ptzrpc.AdaptiveBudget()
        guard = self.guard_factory(
            lambda: ptzrpc.call(ptz, lambda: panlimit.read_pan_x(ptz, token)),
            lambda: ptzrpc.call(ptz, lambda: ptz.Stop(
                {"ProfileToken": token, "PanTilt": True, "Zoom": True}),
                adaptive=stop_budget),
            self.disable_tracking, right_limit=right, left_limit=left, recover=self.recover_home,
            margin=pl.stop_margin, poll_interval=pl.stop_poll_interval,
            stop_interval=pl.stop_poll_interval, cooldown=pl.stop_cooldown,
            suspended=self.suspended, name=self.cfg.name,
            stop_escalated=lambda: stop_budget.is_escalated)
        self.host = host
        self.guard = guard
        guard.start()
        log.info("rotation stop %s: started right=%.4f left=%s margin=%.4f",
                 self.cfg.name, right, left, pl.stop_margin)

    def safe_preset(self, preset):
        x = self.positions.get(str(preset))
        return (x is not None and self.inner_high is not None and x < self.inner_high
                and (self.inner_low is None or x > self.inner_low))

    def prepare_recall(self, preset):
        """Permit only a configured safe destination while the stop latch is active."""
        if not self.safe_preset(preset):
            self._note("recall_refused", f"preset={preset} unsafe")
            return False
        if self.guard is None:
            return False
        if not self.guard.snapshot().latched:
            return True
        permitted = self.guard.prepare_recovery(self.positions[str(preset)])
        self._note("recall_permit" if permitted else "recall_refused", f"preset={preset}")
        return permitted

    def control_plan(self, plan):
        """Keep the latch authoritative over stale plans and recover only inward."""
        self._plan_preset = plan.preset
        if self.guard is not None:
            self.guard.rearm()
            snap = self.guard.snapshot()
            reason = (snap.reason, snap.healthy, snap.latched)
            if reason != self.last_reason:
                log.info("rotation stop %s: reason=%s healthy=%s latched=%s x=%s",
                         self.cfg.name, snap.reason, snap.healthy, snap.latched, snap.position)
                self.last_reason = reason
        if self.allowed():
            return plan
        # Never introduce a new return destination. Use the configured day/home view
        # only when it was verified against these explicit limits.
        candidates = (plan.preset, self.cfg.tracking.day_preset,
                      self.cfg.tracking.night_preset)
        home = next((p for p in candidates if p is not None and self.safe_preset(p)), None)
        preset = home if home is not None and self.safe_preset(home) else None
        self._note("plan_override", f"autotrack=off preset={preset}")
        return replace(plan, autotrack_on=False, preset=preset, back_time=None)

    def close(self):
        if self.guard is not None:
            if not self.guard.close(timeout=2):
                log.warning("rotation stop %s: shutdown tracking OFF unconfirmed", self.cfg.name)
            self.guard = None
