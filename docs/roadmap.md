# Roadmap

tapo-monitor aims to be a local, privacy-first person alert system for Tapo and Reolink
cameras. Frames are scored on hardware you own, and nothing goes to a cloud service
except the alert you asked for. Each camera has a digital twin: the monitor knows how it
should be configured, notices when it drifts and says when it stops watching. Detection
quality is measured and improved from the operator's own labelled frames, not from
generic benchmarks. Home Assistant sees the cameras over MQTT discovery.

This page is the forward plan. The phase-by-phase record up to 2026-11-17, with every
measurement and decision, is in the [roadmap history](roadmap-history.md). Old item
numbers appear as "(was 10.15)", so references in the changelog, issues and code still
lead to their history entry.

## Safety and privacy boundaries

- Read-only introspection is the default. Probes use verified pytapo getters on the
  daemon's existing session and never start a second login loop. Raw bulk
  `performRequest` probing stays out of production: it has made a camera API unavailable.
- An empty getter result means `unknown` or `unsupported`, never a fault and never a
  reason to enable a feature automatically.
- The event ledger stores timestamps, sources, event types, decisions and confidences;
  no frames, credentials, device IDs, MAC addresses or face IDs.
- Camera writes are explicit, allow-listed, auditable and bounded. Calibration, firmware
  upgrades and destructive storage actions are never automatic.
- Interpretations are scoped to model and firmware. Unknown event bits get no public
  meaning without repeatable ground truth.

## How a change is accepted

Implemented, released and accepted are three separate milestones. A release does not
mean acceptance, and a checked implementation item does not close its trial.

A candidate needs a real soak of at least 24 hours with its own code and configuration:
useful activity, no unexplained scorer failures, tracebacks, unscored deliveries or
duplicate incidents. A simulated soak or a quiet period cannot replace it. New behaviour
ships opt-in and the default changes only on its own evidence. Site details stay in
private operator notes.

## Where it stands

The latest release is **0.14.0** (2026-11-15). Details per version are in the
[changelog](../CHANGELOG.md).

| Capability | State |
|---|---|
| Person alerts to Telegram from camera events: live frame, sampler, SD card, recorder and hub clips | Released; scored by a local detector (RF-DETR Medium 896 in production, YOLOX as fallback) |
| Reolink cameras as a detection source | Released opt-in (0.12.0) |
| Photo selection and one message per visit | Released (0.10.0–0.10.2, 0.13.0); incident-level review open |
| Alert clips sent as a reply to the photo | Released opt-in (0.12.0) |
| Outbox: alerts that survive an internet outage | Released opt-in (0.9.0, byte budget 0.12.0); first real outage not yet reviewed |
| Camera digital twin, drift detection, self-heal (per lens on dual-lens cameras), privacy and detection-off notices | Released (since 0.2.0) |
| Shadow ledger, labelling, `label-stats`, `replay` and the detector comparison tools | Released |
| Watch zones, loitering and vehicle alerts | Released opt-in (0.9.0); MQTT Vehicle sensor 0.14.0 |
| Home Assistant over MQTT discovery | Released (0.8.0) |
| Network history, network diagnosis lines, camera address failover and MAC rediscovery | Released (0.13.0 for diagnosis and MAC rediscovery) |
| Virtual PTZ endstops (stop guard) | Deployed candidate, **not accepted** (see theme C) |
| Problems-only daily digest | Released opt-in (0.11.0); soak open |

## Themes

The four themes below are ordered A to D. The order is a proposal for the owner, not a
decision. Inside each theme, open items are listed in the suggested order of work.

### A. Detection quality and the data loop

**Why it matters.** Misses and false alarms are what the owner notices. Moving the
threshold changes little; knowing where the detector fails, per camera and day or night,
is what improves it.

**Current state.** RF-DETR Medium 896 ranked first on a labelled week and runs in
production with threshold 0.35. The 2026-11-17 split showed that one camera holds about
half of the misses and false alarms, recall is 80 % by day and 62 % at night, and many
misses are small persons; a night threshold for that camera was rejected in favour of a
zone. The labelled data is still biased toward model disagreements.

- [ ] Collect unbiased data: switch on the random sample of dropped frames
  (`TAPO_REVIEW_DROP_SAMPLE`) on the busy cameras (was 10.14). Review and sent frames
  already outlive the hosts' 14-day retention: a nightly `tools/collect_frames.sh` run
  copies them into one labelling set and pre-labels them.
- [ ] Review production quality on the remaining cameras, per camera and day or night:
  misses, false alerts, latency and fallback use; confirm the fallback before retiring
  any serving path. Label high-mounted night views, where clearly visible people scored
  0.09–0.17 (was 10.14).
- [ ] Run a recording audit on a stratified, unbiased sample of recorded windows to
  estimate the persons that neither model catches (was 10.14).
- [ ] With a few weeks of sampled night frames, set `night_threshold` only on a camera
  whose night cut differs from the day's, checked with `replay --compare` (was 9.2).
- [ ] Enable `scorer.unscored_at_night: hold` on the card sites and read the first scorer
  outage that falls at night (was 10.14).
- [ ] Trial hold expiry in `observe` mode to the end, label what it would have sent and
  promote to `send` only from incident-level numbers (was 10.2).
- [ ] Trial `sampler.keep_sampling_on_defer` and `scorer.person_bit_skips_hold` on the
  busiest recording-source camera, then re-run the replay on that week (was 10.10).
- [ ] Trial zones, loitering and vehicle alerts in observe mode on one fixed camera for
  five days (vehicle threshold 0.5 is untested). Gate: alert volume within 30 % of the
  baseline, no unexpected restart or traceback (was 10.12, 10.13).
- [ ] Review delivered photos and first-alert latency after the frame-selection rollout,
  per camera and day or night, including card-source photo quality against each camera's
  own clips. Keep face preference opt-in until clips show a gain (was 10.5).
- [ ] Only if labelled residual errors justify it: train a small person/no-person
  verifier on scorer-box crops (`tools/build_verifier_dataset.py` builds the data),
  shadow-run it for a week, promote only with a measured gain. On a shared GPU host keep
  to one directory, check disk and GPU first and clean up after (was 9.3, 9.4).

### B. Reach: Home Assistant OS and docs

**Why it matters.** Home Assistant OS users have no place to run the monitor without a
second machine; forum feedback asked for exactly this.

**Current state.** The monitor runs as a service or container and talks MQTT only.

- [ ] A Home Assistant app (formerly add-on) built on the published container image: app
  options map to `cameras.yaml` and the environment, credentials use the app's secret
  options, state lives in the app's data folder, and the Mosquitto app is found through
  the Supervisor's MQTT service (was Phase 11).
- [ ] Ship it from a separate app repository with the same release gate as the package: a
  tagged image and a real overnight soak on one Home Assistant OS install (was Phase 11).
- [ ] Update the README and [getting started](getting-started.md) once the app exists.
- [ ] Only after the app has users: decide whether a custom integration (config flow,
  device registry, re-park service) is worth a second code path (was Phase 11).

### C. Reliability and safety

**Why it matters.** An alert that arrives late, twice or not at all erodes trust faster
than a false alarm. A pan/tilt camera that drives past its configured limit can point
somewhere it must not.

**Current state.** The stop guard is a deployed candidate, not accepted. Stop calls failed
31–38 % of the time. On 2026-11-16 a read-outage tolerance was rejected (the lens moved
during read outages, once past the limit), and the firmware tracker crossed the limit
39–64 s after a verified tracking OFF. Both stop-mode sites run recall mode. An unreleased
candidate stops the lens at once on uncommanded motion and adds opt-in
`stop_rearm_quiet_s`. See [stop guard safety](stop-guard-safety.md).

Endstops (was 10.15):

- [ ] Test at night whether also switching smart tracking off stops the motor after a
  verified OFF, then soak the local candidate in stop mode.
- [ ] Explain the observed limit crossings from natural episodes (a Stop acknowledgement
  does not prove the tracker stopped), then accept only after 24 hours with several real
  boundary interventions, no crossing and confirmed Stop and OFF in every episode.
- [ ] An opt-in recall-first mode without the evidence frame, for sites where the 3 s
  grab before `GotoPreset` matters more than the frame.
- [ ] Decide the stop guard's status from the soak evidence: if Stop calls keep failing
  and the tracker still crosses the limit after a verified OFF, document recall mode as
  the recommended setting and stop mode as best-effort, instead of another round of fixes.

Alert delivery:

- [ ] Outbox: review the first real outage against the review log, and run a controlled
  send-failure and restart trial before enabling it on other hosts (was 10.9).
- [ ] Validate late-event deduplication against real delayed events and check duplicates
  across delivery paths at incident level (was 10.6).
- [ ] Latency: card follow-ups take about 150 s. Trial `sd_early_look`, then
  `sd_early_span: 12`, and `hub_clip_worker` on a hub site; consider a lower freshness
  guard or a local recorder (was 10.4). Report the first-delivery median per path in
  `soak_report` (was 10.13).
- [ ] Alert clips: confirm night playback on a phone, card follow-ups no slower, a clean
  recorder-backed soak; reuse an SD follow-up's card read for the clip (was 10.17).
- [ ] Problems-only digest: finish the soak and verify degraded and unknown health in
  normal operation before recommending it (was 10.16).
- [ ] Check that something outside the monitored hosts alerts when the whole fleet or its
  scorer goes silent (a dead-man's switch), not only per-host watchers.

Devices and network:

- [ ] Dual-lens camera (C545D): grow the event sample (night, pets, vehicles) and re-check
  the 180 s hold; compare `lens_pick_stream` photos with wide-lens ones and decide from
  per-lens scores whether the pan/tilt lens goes first or gets a lower threshold (was 8.6).
- [ ] Wire in the helper-host MAC lookup (`macresolve`, not used by the daemon yet) for
  sites where ARP is invisible across a subnet route (was 10.13). Network history and
  diagnosis lines (was 10.11) are complete.

### D. Maintenance

**Why it matters.** The monitor depends on unofficial camera APIs; a library or firmware
release can change behaviour without warning.

**Current state.** A weekly workflow opens an issue when PyPI has a newer release than
`.github/tested-versions.txt`; only pytapo is listed, and a release is soaked on real
cameras before the line is bumped. The [local API reference](tapo-local-api.md) is
published (was 10.7).

- [ ] Add `reolink-aio` and `onvif-zeep` to `.github/tested-versions.txt`, so the
  dependency watch covers every camera library.
- [ ] Camera firmware watch: notice when a camera's firmware version changes, say so in
  the digest, and re-run the camera report for that model so its fixture stays current.
- [ ] Add confirmed device findings to the local API reference as they land, for example
  the tracker moving after a verified OFF (10.15).
- [ ] Prove backup and restore of a host: back up `cameras.yaml` and the twin state, then
  restore onto a clean machine and confirm the monitor starts with the same fingerprint.
- [ ] Run [getting started](getting-started.md) once on a clean machine, as a new user
  would, and fix every step that needs unwritten knowledge.

## Later / parked

- [ ] Poor-link mode (designed, parked 2026-11-17): after repeated scorer transport
  failures, no clips, one card read, no retries, old alerts into the outbox summary.
  The weak-link site moved to a wired host and its failures stopped. Trigger: a site
  needs it again (design in the [history](roadmap-history.md#current-priorities--2026-11-16)).
- [ ] `pan_limit` `mode: stop` on a weak link. The read-outage tolerance it needed was
  rejected (2026-11-16); such sites stay on recall mode (was 10.15).
- [ ] Opt-in feedback buttons under each alert (built on a branch, parked 2026-11-17).
  Production false alarms are rare (one in 24 empty night frames on the busiest camera,
  none by day); the open question is missed persons, which a button on a delivered alert
  cannot reveal. The random drop sample answers that first (was 10.14).
- [ ] A local fallback scorer while the shared one is unreachable (was 10.9).
- [ ] Extend camera-address failover to recorder paths, MQTT and hub cameras (was 10.13).
- [ ] Dual-camera handoff through `HandoffManager`, observe-only at first, once a second
  overlapping camera is deployed (was 8.5, Phase 5).
- [ ] Re-measure the scene gate's reach whenever a new delivery path appears (was Phase 5).
- [ ] Fine-tune the detector with box-level labels, only if the verifier plateaus (was 9.4).
- [ ] The firmware smart lamp lights about 20 s late while the PTZ moves; trial a night
  without tracking on that camera.

## Research tracks

Out of production until evidence repeats ([details](roadmap-history.md#research-tracks)).

1. Meaning of unknown `events_1` bits, from deliberate ground-truth triggers.
2. ONVIF PullPoint as an event source (tested firmware sends mostly init messages).
3. Exposure profiles and headlight compensation for sharper night subjects.
4. Known/unknown-face alert policy with local mappings only (`learn-face` shipped).
5. A capability manifest across models and firmware from anonymized
   `tapo-monitor report` submissions; TC46 tracking, lighting and SD still need checks.
6. Standalone battery camera onboarding without a hub (paused).

## Contributing

Land each change as a separately testable step: data model and redaction, then storage
and deterministic logic, then daemon integration behind an opt-in flag, then CLI and
reporting, then documentation with sanitized examples, then an observe-only trial, and
only then promotion of the proven policy. Before a public commit, run `pytest -q`,
`ruff check .` and the repository anonymization scan, and review every staged path. See
[CONTRIBUTING](../CONTRIBUTING.md).
