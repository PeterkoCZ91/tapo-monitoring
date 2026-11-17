# Stop-guard recovery safety

In `pan_limit.mode: stop`, the configured safe day/night preset may be recalled
while the guard is latched only after a fresh ONVIF position, a verified tracking
OFF, and validation that the destination lies strictly within the configured
inner range. A successful Stop RPC and verified tracking OFF do **not** prove the
motor has stopped.

Before permitting a recall, the guard compares its last two consecutive fresh,
successful pan observations. If they show motion away from the selected target,
it refuses the preset (`recall_refused: outward_motion`) and leaves the latch and
normal Stop retries in force. A new fresh observation that is stationary or
moving toward the target may permit the recall. A failed read, stale observation
or privacy suspension invalidates motion history; it must not be treated as a
fresh direction estimate. If an outward observation arrives after the preset
permit, the existing recovery-outward path cancels the permit and forces Stop.

This is a software interlock, not a firmware endstop. It does not identify the
source of autonomous motion or guarantee that the lens cannot cross a boundary
between observations, particularly while ONVIF is unavailable. To assess a
candidate, inspect episode traces for observed configured-limit crossings and
for position peaks after confirmed Stop/OFF, not just counts of failed Stop RPCs.
Production acceptance requires a separate observation period with actual
boundary interventions; an offline test pass is not acceptance.

`pan_limit.recall_evidence` belongs to recall mode only and does not change any of
the above: stop mode takes no evidence frame, accepts the key and ignores it. In recall
mode, `recall_evidence: false` removes the evidence grab (usually about 3 s) from in
front of `GotoPreset`, so the lens has less time to travel past the limit before the
recall goes out. It removes a delay, not a check: hold grace, linkage and privacy
refusals still gate the recall exactly as before.

Camera exception messages can contain session URLs and addresses. Event API
failures therefore log only the exception class and a fixed diagnosis category;
never copy raw exception text into an operator report or public issue.
