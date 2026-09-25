# Changelog

All notable changes to this firmware are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Flashing instructions live in [HARDWARE.md](HARDWARE.md); the MQTT contract is documented in
[README.md](README.md#mqtt-topic-contract).

## [Unreleased]

## [1.5.0] - 2026-09-25

Contract stays **v3**. Every wire change below is additive: consumers that read named fields and
ignore unknown statuses are unaffected. zemismart-blinds 0.9.5 was checked against it. At the
default `tx_bucket_offset_us: "0"`, a well-formed frame whose referenced buckets are all at least
100 µs transmits byte-identical to 1.4.0.

### Added

- **`completed` TX telemetry on `/status`**
  ([#17](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/17), part of
  [zemismart-blinds#22](https://github.com/joyfulhouse/zemismart-blinds/issues/22)): when a
  normally scheduled command finishes, the bridge emits one non-retained QoS 1
  `{"status":"completed","command_id":…,"action_repeats_delivered":N,"action_repeats_configured":M}`.
  It counts ACTION frames handed to the RF coprocessor, not motor acknowledgements. It is
  best-effort outbox traffic that never evicts or reorders an existing lifecycle status.
  Displacement and disarm keep their existing events and report no count.
- **`hardware_variant` inventory tag**
  ([#12](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/12)): a new substitution,
  default `efm8bb1-portisch`, published verbatim as `hw` in retained `/info`. It is inventory only
  and changes no behaviour. The new `examples/bedroom-v22.yaml` covers Sonoff R2 v2.2 boards
  (OB38S003 running the vendored mightymos firmware,
  [#13](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/13)).
- **Opt-in `tx_bucket_offset_us`**
  ([#15](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/15),
  [#16](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/16)): microseconds
  subtracted from every bucket of an outbound B0 frame, to offset the OB38S003 port's long
  transmit timing (upstream mightymos/RF-Bridge-OB38S003#27). It is for OB38S003 boards only,
  accepts 0–120, and defaults to 0. Do not combine it with hand-tuned codes. The effective value
  is published as `tx_offset_us` in retained `/info` so a fleet audit can see which bridges
  compensate. `send_raw` now cross-checks a B0 frame's declared length and bucket count, and
  leaves a malformed frame untouched rather than rewriting its data.

### Changed

- **Builds on ESPHome 2026.9.0, which is now the minimum version.** The vendored `mqtt`
  component is re-vendored from 2026.9.0 with the inbound payload guard re-applied. The 2026.7.3
  copy called a core helper that 2026.9 removed, so it no longer compiled. CI and the documented
  compile and flash commands now pin `esphome==2026.9.0`.
- `secrets.example.yaml` uses a non-zero placeholder API key. ESPHome 2026.9 rejects the
  all-zeros key at config validation, which failed the example compile.
- The `rf_bridge` receive fixes are now upstream in ESPHome 2026.9.0 as
  [esphome/esphome#17683](https://github.com/esphome/esphome/pull/17683). The component stays
  vendored for its fork-only features. See [components/rf_bridge/README.md](components/rf_bridge/README.md).

### Fixed

- **A timed command keeps all of its ACTION repeats under concurrency**
  ([#20](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/pull/20), part of
  [zemismart-blinds#22](https://github.com/joyfulhouse/zemismart-blinds/issues/22)): same-bridge
  round-robin could let a timed command reach its fail-safe STOP deadline before all of its
  configured repeats went out. Once a timed command dispatches its first ACTION, it now owns
  dispatch through its remaining ACTION and TRAILER copies. Untimed peers are delayed, not
  dropped. Deadlines are never recomputed, and STOP lateness stays bounded by one owner-frame
  airtime, the same bound as a solo command.
- **Frames referencing a bucket shorter than 100 µs are rejected at admission and floored in
  `send_raw`** ([#18](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/18)):
  `normalize_b0` validated structure, bucket references, and the 2 s airtime ceiling, but had no
  minimum-duration check, and the only 100 µs floor lived inside the bucket-compensation pass,
  which does not run at the default `tx_bucket_offset_us` of 0 — so a frame like
  `AAB005010800010055` (a referenced 1 µs bucket) was admitted and transmitted. On the
  OB38S003 (mightymos) a referenced 0–9 µs bucket wraps to ~659.5 ms of stuck carrier per
  occurrence; on the EFM8BB1 (Portisch) 0–64 µs underflows to ~65.5 ms. `/tx` admission now
  rejects such frames with `"reason":"frame references a bucket shorter than 100 us"`, and
  `send_raw` (a public action that bypasses admission) floors every *referenced* sub-100 µs
  bucket to 100 µs at every offset, logging a throttled warning. At the default offset of 0 only
  referenced buckets are floored: unreferenced bucket-table entries are never rewritten and never
  reach the air, and frames whose referenced buckets are all ≥ 100 µs transmit byte-identical. At
  a non-zero offset the compensation pass rewrites the whole bucket table, referenced or not (see
  [#24](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/24)). No contract-version
  change.

## [1.4.0] - 2026-08-02

### Added

- **`/tx` requires the current boot id (contract v3,
  [#10](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/10))**:
  every `/tx` command must now carry a `boot` field equal to the bridge's current boot id, as
  advertised on retained `/info` (`v` bumps to `3`). Missing, mistyped, and mismatched values are
  all rejected with `"reason":"boot_mismatch"`, giving a controller exactly one recovery path —
  re-read `/info`, then re-issue with the current boot. Because the RAM dedup ring cannot survive
  a reboot, this closes the last structural gap in retained-`tx` rejection: a retained command
  republished after a reboot now dies on the boot check instead of merely being unsupported by
  convention. This requires a controller that stamps `boot` — zemismart-blinds ≥ 0.8.0. The
  integration deploys first; firmware ≤ 1.3.0 ignores the stamped `boot` field, and this release
  is the first to enforce it.
- **OTA waits for natural idle before flushing armed STOPs** ([#9](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/9)):
  the previous OTA `on_begin` fired every armed command's fail-safe STOP immediately, early
  instead of at its scheduled deadline. `on_begin` now pumps the scheduler's own dispatch (the
  5 ms tick cannot run while the callback blocks) for up to 30 s (`OTA_IDLE_WAIT_MS`), letting
  in-flight trains complete and their STOPs fire at their real time; anything still armed past the
  window falls back to the v1.3.0 immediate flush. A new `on_error` handler unlatches `/tx` so a
  failed transfer no longer leaves the bridge refusing commands until a manual reboot.
- **Vendored `mqtt` component caps inbound payload assembly at 4 KiB**
  ([#8](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/8)): the stock `mqtt`
  component reserves the broker-declared total size for a fragmented publish before any
  integration-level validation runs, so a hostile or errant tens-of-KB publish could exhaust the
  heap or stall past an armed fail-safe STOP deadline on the ESP8285. The vendored component
  (ESPHome 2026.7.3, see [components/mqtt/README.md](components/mqtt/README.md)) drops any
  message whose declared total exceeds 4 KiB before the `reserve()` call, discards subsequent
  fragments of the same oversized message, and logs the drop throttled to once per 5 s.

## [1.3.0] - 2026-07-26

### Added

- **`displaced` carries the displacement clock** (contract change the consumer relies on,
  [#6](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/6)): the `displaced` status
  now emits `age_ms`, `t`, and `boot` — the same clock fields `started` already carried — anchored
  on the instant the firmware performed the displacement (`displacement = t - age_ms`). A redelivery
  arriving while the displaced command's owed fail-safe STOP is still draining, and one arriving
  after it has fully drained, both report the age since the *original* displacement rather than a
  stale 0. This lets the controller *measure* its post-displacement flush-tolerance window instead
  of budgeting a wider-than-necessary one; an over-wide window is not free, because while it is open
  a genuine physical STOP press on those channels is absorbed rather than acted on. A `command_id`
  that reached the terminal state by an explicit `disarm` has no displacement instant, so its
  replayed `displaced` omits the three fields rather than reporting a fabricated age. The wire
  format is unchanged (the publisher already emitted these keys when present); only the `displaced`
  event now populates them. Per-command RAM cost: `+4` bytes on each draining displaced-STOP flush
  entry (transient, at most `MAX_TARGETS`), and `0` net bytes on the recent-command ring
  (a validity flag placed in existing struct padding).

### Fixed

- **Four fail-safe-STOP delay paths closed** (safety batch A, from the release-hardening
  review — every change either removes a way to delay a due STOP or makes the delay visible):
  - `repeat_gap_ms` no longer postpones a due STOP. The pacing gate splits into physical RF
    occupancy (UART + airtime + margin, always honored) and the discretionary user floor, which
    armed STOP work bypasses once the air is clear; STOPs arm before either gate is consulted.
    The substitution is clamped 0..60000, so a negative value can no longer survive as a huge
    unsigned delay.
  - Admission bounds aggregate first-STOP occupancy at 4 s, cutting the worst-case late STOP
    from ~33 s (16 targets × 2 s frames) to ~6.2 s; beyond it commands are rejected with a
    reason and remembered.
  - A bounded 32-event lifecycle outbox retries statuses on reconnect, so a broker flap between
    admission and first RF handoff can no longer lose `started` after the command and its local
    STOP already executed.
  - OTA begin latches `/tx` closed and synchronously fires every armed STOP before the blocking
    transfer — "blind runs to its hardware limit after the update reboots" becomes "blind stops
    slightly early". The full OTA veto interlock remains deferred to hardware validation.

### Documentation

- README and HARDWARE.md rewritten for a first-time builder: per-step "done when" checkpoints,
  the radio-chip/Wi-Fi-chip two-firmware split made unmissable, protocol reference folded behind
  disclosure. Board-revision incompatibility (EFM8BB1 vs OB38S003), Tasmota-as-temporary-tool,
  and fail-safe STOP semantics verified intact. The status-contract section now states that a
  `displaced` clock anchors the admission instant, not when the owed STOP reaches air — a
  controller sizing a flush window from it must add drain latency.
- Docs corrected to match the implementation (batch A): displaced-STOP timing is N pacing gaps
  not one, `deadline_at` is a due time not an on-air guarantee, duplicate suppression is scoped
  to live commands plus the 64-ID same-boot window, and an authenticated least-privilege ACL
  matrix now covers `/tx` and `/cmd`.

## [1.2.2] - 2026-07-20

Review hardening from a multi-model adversarial pass over v1.2.1.

### Fixed

- **Serialization-aware dispatch hold**: the RF-busy window now includes each frame's
  19200-baud UART serialization time, because the EFM8BB1 only keys the radio once the whole
  command has arrived. The previous airtime-only hold under-covered the tail of a burst by
  ~45 ms (typical AOK frame) to ~135 ms (maximum frame) — enough for the next handoff to reach
  the coprocessor's ~64-byte UART ring mid-burst. Non-B0 frames keep margin-only occupancy.
- `/tx` `command_id`/`target` and `/cmd` `action`/`command_id` are length-bounded before any
  owning string is constructed.

### Changed

- `record_dispatch_()` solely owns the dispatch-timing invariant (both call sites previously set
  `next_rf_at_` independently).
- Alloc-free hex serialization on the transmit path; AA/B1/55 wire constants shared from
  `rf_bridge_protocol.h`; recent-command ring helpers deduplicated.

## [1.2.1] - 2026-07-19

### Added

- **Scheduler-gated MQTT-outage watchdog**: a bridge that cannot reach the broker for 15 minutes
  reboots itself, but only while the scheduler is idle and the air is clear, so a RAM-held
  fail-safe STOP is never dropped by the recovery. (Stock ESPHome `reboot_timeout`s stay at 0 for
  exactly that reason.)
- Deterministic back-to-back B1 split when a queued `0xAA` start byte arrives.

## [1.2.0] - 2026-07-17

### Added

- **Airtime-paced TX dispatch**: the scheduler holds each next UART handoff until the previous
  frame's air completes (`max(repeat_gap, airtime + margin)`). Gap-only pacing had been corrupting
  frames 2..N of every burst inside the coprocessor's UART ring — ten dispatches produced one
  acknowledged transmission. Repeats are now deterministic.
- Production documentation, including the hardware-validated support matrix.

## [1.1.0] - 2026-07-17

### Added

- **Continuous idle-listen receive** behind the `listen_enabled` substitution: a 5 ms reconciler
  enters bucket sniff only while the scheduler is idle and the channel is clear, yields the radio
  before transmit, and resumes afterwards. Heard frames publish on `/rx`; the bridge never
  transmits in response.
- Contract v2: `/status` `started` carries `t`, `age_ms`, and `boot`; retained `/info` advertises
  `boot`, `listen`, and `v`; `/cmd` accepts `disarm`.

### Fixed

- **Never ACK received B1 deliveries.** Acknowledging one makes Portisch re-arm its stale
  `last_sniffing_command` and silently revert to standard sniffing — which killed listening on the
  first heard frame. (Stock ESPHome's `rf_bridge` has the same latent bug; fix submitted upstream
  as [esphome/esphome#17683](https://github.com/esphome/esphome/pull/17683).)
- **Accept OEM captures without a full trailer.** Some real remotes transmit 65 bit pairs with a
  single trailing 0-read instead of the nominal `[1, 0]`; those presses were previously captured
  and then rejected by the AOK filter.
- A 5 s idempotent B1 keepalive bounds any remaining silent bucket-mode exit to one period.

## [1.0.0] - 2026-07-14

First public release.

### Added

- MQTT-to-433 MHz bridge for Portisch-flashed Sonoff RF Bridge R2 hardware: a deliberately dumb
  beacon that carries no blind codes and exposes no cover entities.
- Correlated command lifecycle on `rf433/<bridge>/tx` → `/status` (`accepted`, `rejected`,
  `started`, `displaced`), QoS 1 with a replay/dedup ring.
- **Bridge-held fail-safe STOP deadlines**: a timed move's STOP lives in the bridge's RAM and
  fires even if Home Assistant, the broker, or Wi-Fi disappears mid-travel.
- Round-robin per-target scheduling with STOP promotion at the deadline and displaced-STOP flush.
- Retained `rf433/<bridge>/availability` and `/info` discovery.
- Vendored, extended `rf_bridge` component adding the B1 receive callback with correct framing.

[Unreleased]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.5.0
[1.4.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.4.0
[1.3.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.3.0
[1.2.2]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.2
[1.2.1]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.1
[1.2.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.0
[1.1.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.1.0
[1.0.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.0.0
