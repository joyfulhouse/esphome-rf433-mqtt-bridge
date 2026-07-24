# Changelog

All notable changes to this firmware are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Flashing instructions live in [HARDWARE.md](HARDWARE.md); the MQTT contract is documented in
[README.md](README.md#mqtt-topic-contract).

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
