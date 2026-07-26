# Changelog

All notable changes to this firmware are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Flashing instructions live in [HARDWARE.md](HARDWARE.md); the MQTT contract is documented in
[README.md](README.md#mqtt-topic-contract).

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

[1.2.2]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.2
[1.2.1]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.1
[1.2.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.2.0
[1.1.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.1.0
[1.0.0]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/releases/tag/v1.0.0
