# ESPHome RF433 MQTT Bridge

ESPHome firmware that turns a **Sonoff RF Bridge R2** (Portisch) into a dumb, reliable
MQTT-to-433.92 MHz transmitter with correlated acknowledgements, per-target scheduling, and
time-boxed RF capture for onboarding.

[![License][license-shield]](LICENSE)
[![CI][ci-shield]][ci]
[![Project Maintenance][maintenance-shield]][maintenance]
[![GitHub Sponsors][sponsors-shield]][sponsors]
[![Ko-fi][kofi-shield]][kofi]

## What it does

The bridge is deliberately dumb: it contains **no device codes and no cover entities**. A
controller — for example the [joyfulhouse/zemismart-blinds][zemismart-blinds] Home Assistant
integration for AOK/Zemismart roller blinds — publishes correlated JSON commands; the beacon
validates, schedules, and transmits Portisch **B0** raw frames.

- **Correlated acknowledgements** — every command carries a `command_id`; the bridge answers
  `accepted`/`rejected` on admission and `started` on the first actual RF dispatch. `started`
  means the frame was handed to the RF coprocessor over UART (ESPHome's `send_raw` does not wait
  for an EFM8BB1 ack); it is proof of dispatch, not of RF emission.
- **Per-target scheduling** — one RF frame on air at a time, while each target keeps its own
  repeat phase and an **absolute monotonic STOP deadline** (`stop_after_ms` + `stop_raw`) so a
  partial movement is stopped by the bridge itself even if the controller disappears.
- **Latest command wins** — a new command whose channels overlap an active target on the same
  remote displaces it: the displaced command's pending fail-safe STOP is flushed on air first,
  and a `displaced` status with its `command_id` tells the controller to retire its motion model.
  `displaced` is published at admission of the replacing command, while the flushed STOP physically
  transmits within the next pacing gap(s); a controller that freezes the model at displaced-time is
  within one pacing gap of physical truth.
- **Duplicate suppression** — a ring of recent `command_id`s suppresses QoS-1 broker redeliveries
  and same-boot retained replays. The ring lives in RAM, so a retained `tx` command *can* replay
  after a reboot: **retained `tx` publishes are unsupported and dangerous — never publish them.**
- **Bounded memory** — admission enforces a total stored-frame budget sized for the ESP8285 heap.
- **Retained discovery** — the beacon publishes retained `availability` (birth/will) and `info`
  (area and default flag) so controllers can discover online bridges and prefer one in the same
  area.
- **Strict input validation** — B0 frames, target keys, repeat counts, and stop deadlines are
  validated on-device before anything reaches the RF coprocessor.
- **Time-boxed onboarding capture** — validated Portisch **B1** raw-bucket captures are published
  for controller-side decoding only during an active sniff of at most 60 seconds. Receive is off by
  default on every boot.

## MQTT topic contract

All topics live under the fixed `rf433/` root:

| Topic | Direction | Payload |
|---|---|---|
| `rf433/<bridge_id>/availability` | bridge → broker (QoS 0, retained) | `online` / `offline` |
| `rf433/<bridge_id>/info` | bridge → broker (QoS 0, retained) | `{"bridge":"rf433-bridge","area":"living_room","default":false}` |
| `rf433/<bridge_id>/tx` | controller → bridge (QoS 1, non-retained) | JSON transmit command (below) |
| `rf433/<bridge_id>/status` | bridge → controller (QoS 1, non-retained) | `{"status","command_id"[,"reason"][,"age_ms"]}` |
| `rf433/<bridge_id>/rx` | bridge → broker (QoS 1, non-retained) | `{"frame":"AAB1...55","t":123456}` |
| `rf433/<bridge_id>/cmd` | controller → bridge (QoS 1, non-retained) | `{"action":"sniff","seconds":30}` or `{"action":"sniff","seconds":0}` |

`status` is `accepted`, `rejected` (with `reason`), `started` (first RF dispatch), or `displaced`
(a newer overlapping command replaced this one — see below).

QoS-1 broker redeliveries are answered idempotently: an already-admitted `command_id` gets its
`accepted` (and, if RF already started, `started`) statuses replayed instead of a fresh rejection.
A replayed `started` carries `age_ms` — how long ago the original RF handoff happened — so the
controller can anchor its motion model at the true start. A `command_id` that was rejected by a
state-dependent admission check (scheduler full, storage budget) is remembered and re-rejected,
never silently admitted later.

Command body on `tx`:

```json
{
  "command_id": "unique-correlation-id",
  "target": "a1b2c3:42:1,2",
  "raw": "AAB0...55",
  "trailer_raw": "AAB0...55",
  "repeats": 5,
  "stop_after_ms": 8000,
  "stop_raw": "AAB0...55"
}
```

`target` is `prefix:remote_id:channels` (lowercase hex, strictly increasing channels 1..16).
`trailer_raw`, `stop_after_ms`, and `stop_raw` are optional; a timed command requires `stop_raw`.

### Time-boxed onboarding sniff

Receive is **default OFF** on every boot. This is a privacy boundary: ambient 433 MHz traffic can
identify nearby remotes and activity. On startup the bridge unconditionally sends Portisch
stop-sniff (A7) and clears any partial B1 capture, including after an ESP-only restart where the
independently running EFM8BB1 may still be sniffing.

`/cmd` accepts exactly one action. Publish `{"action":"sniff","seconds":30}` at QoS 1 with retain
disabled to start or extend a bucket sniff. Integer values from 1 through 60 select the window;
larger integers are hard-capped at 60, and another positive command never shortens an active
window. Positive commands are limited to one every 250 ms.
`{"action":"sniff","seconds":0}` immediately cancels the sniff, clears its bounded state, and sends
A7. Cancellation is exempt from the rate limiter. A replayed retained positive command is benign
because every sniff auto-expires, but controllers must still publish `/cmd` with retain disabled.

Only while that sniff is active, every accepted AOK-prefiltered B1 capture is forwarded to `/rx`
as QoS 1, non-retained JSON. `frame` is the compact uppercase Portisch frame, including `AAB1` and
the final `55`; `t` is the bridge's plain `millis()` value when the callback runs. A failed MQTT
enqueue is logged.

RX is observation only, not motor feedback. The receive handler never calls `send_raw`, enters the
TX scheduler, or otherwise triggers TX. Continuous listen, post-TX receive re-arm, keepalive, and
live state-sync are not included in this slice; they are planned follow-up work.

> **TODO — HARDWARE-VALIDATION:** The current host AOK fixtures are synthesized from parser
> assumptions; no real OEM-captured golden `AAB1…55` sample exists in this repository. The hardware
> spike must add UP/DOWN/STOP captures across identities and channels, including timing jitter.
> Before fleet use, also verify that cancellation A7 cleanly exits an in-flight B1 capture, real
> captures contain the OEM `[1, 0]` trailer required by the filter, and ACKing accepted, rejected,
> short, and timed-out B1 transport frames is benign. These physical checks remain deferred.

## Hardware

- **Sonoff RF Bridge R2** with the EFM8BB1 RF coprocessor flashed to
  [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1) (required — the stock RF
  firmware cannot transmit raw B0 buckets).
- The ESP8285 runs this ESPHome package (`rf_bridge:` UART @ 19200; GPIO1/GPIO3 belong to the RF
  coprocessor, so serial logging is disabled).

## Install

Any MQTT broker works — with Home Assistant's Mosquitto add-on (the common setup), the broker is
simply your HA host; a standalone broker works identically.

1. Copy the package, both headers, the complete local component directory, and an example renamed
   for your device while preserving this layout:

   ```text
   your-esphome-config/
   ├── living-room.yaml
   ├── rf433-mqtt-bridge.yaml
   ├── rf433_scheduler.h
   ├── rf433_rx.h
   └── components/
       └── rf_bridge/
   ```

   `rf433-mqtt-bridge.yaml` loads `components/rf_bridge/` as a local external component. Keep that
   directory intact rather than flattening it. It is vendored from ESPHome 2026.6.5's
   `esphome/components/rf_bridge` and extended with the B1 receive callback used by this package.
2. Create `secrets.yaml` from `secrets.example.yaml`.
3. Adjust the substitutions (bridge id, area, broker, credentials). Set `default_bridge: "true"`
   on exactly one bridge in your home. Networking is DHCP by default; a commented `manual_ip`
   block in the example shows how to pin a static address.
4. Validate and flash with the hardware-tested ESPHome release:

```shell
uvx --from "esphome==2026.6.5" esphome compile living-room.yaml
uvx --from "esphome==2026.6.5" esphome run living-room.yaml
```

First flash of a stock device requires serial; later updates are OTA.

## Development

The C++ target scheduler and the package contract are tested on the host:

```shell
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI stages `secrets.example.yaml` as `secrets.yaml`, copies `examples/living-room.yaml` beside the
package, headers, and `components/`, and performs the same full
`esphome compile living-room.yaml` build.

## Roadmap

- **Continuous listen/live state-sync:** planned follow-up after the bounded Learn/onboarding
  capture path has real OEM hardware fixtures.

## Support Development

This firmware is built and maintained in my spare time, with real hardware and tooling costs
behind every release. If it's useful to you, consider sponsoring the project or leaving a tip —
it's genuinely appreciated and helps keep the project moving.

[![GitHub Sponsors][sponsors-shield]][sponsors] [![Ko-fi][kofi-shield]][kofi]

## License

MIT — see [LICENSE](LICENSE).

---

[zemismart-blinds]: https://github.com/joyfulhouse/zemismart-blinds
[license-shield]: https://img.shields.io/github/license/joyfulhouse/esphome-rf433-mqtt-bridge?style=for-the-badge
[ci-shield]: https://img.shields.io/github/actions/workflow/status/joyfulhouse/esphome-rf433-mqtt-bridge/ci.yml?branch=main&label=CI&style=for-the-badge
[ci]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/actions/workflows/ci.yml
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40btli-blue.svg?style=for-the-badge
[maintenance]: https://github.com/btli
[sponsors-shield]: https://img.shields.io/badge/Sponsor-GitHub-EA4AAA.svg?style=for-the-badge&logo=githubsponsors&logoColor=white
[sponsors]: https://github.com/sponsors/btli
[kofi-shield]: https://img.shields.io/badge/Ko--fi-support-FF5E5B.svg?style=for-the-badge&logo=ko-fi&logoColor=white
[kofi]: https://ko-fi.com/bryanli
