# ESPHome RF433 MQTT Bridge

ESPHome beacon firmware that turns a **Sonoff RF Bridge R2** (Portisch) into a dumb, reliable
MQTT-to-433.92 MHz transmitter with correlated acknowledgements and per-target scheduling.

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
  `accepted`/`rejected` on admission and `started` on the first actual RF dispatch.
- **Per-target scheduling** — one RF frame on air at a time, while each target keeps its own
  repeat phase and an **absolute monotonic STOP deadline** (`stop_after_ms` + `stop_raw`) so a
  partial movement is stopped by the bridge itself even if the controller disappears.
- **Latest command wins** — a new command whose channels overlap an active target on the same
  remote displaces it: the displaced command's pending fail-safe STOP is flushed on air first,
  and a `displaced` status with its `command_id` tells the controller to retire its motion model.
- **Duplicate suppression** — a ring of recent `command_id`s rejects QoS-1 broker redeliveries
  and accidentally retained commands replayed on reconnect. Never publish retained `tx` commands.
- **Bounded memory** — admission enforces a total stored-frame budget sized for the ESP8285 heap.
- **Retained discovery** — the beacon publishes retained `availability` (birth/will) and `info`
  (area, default flag) so controllers can discover online bridges and prefer one in the same area.
- **Strict input validation** — B0 frames, target keys, repeat counts, and stop deadlines are
  validated on-device before anything reaches the RF coprocessor.
- **Diagnostics** — Portisch advanced/bucket sniffing exposed as diagnostic buttons for protocol
  capture work.

## MQTT topic contract

All topics live under the fixed `rf433/` root:

| Topic | Direction | Payload |
|---|---|---|
| `rf433/<bridge_id>/availability` | bridge → broker (retained) | `online` / `offline` |
| `rf433/<bridge_id>/info` | bridge → broker (retained) | `{"bridge","area","default"}` |
| `rf433/<bridge_id>/tx` | controller → bridge | JSON command (below) |
| `rf433/<bridge_id>/status` | bridge → controller | `{"status","command_id"[,"reason"]}` |

`status` is `accepted`, `rejected` (with `reason`), `started` (first RF dispatch), or `displaced`
(a newer overlapping command replaced this one — see below).

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

## Hardware

- **Sonoff RF Bridge R2** with the EFM8BB1 RF coprocessor flashed to
  [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1) (required — the stock RF
  firmware cannot transmit raw B0 buckets).
- The ESP8285 runs this ESPHome package (`rf_bridge:` UART @ 19200; GPIO1/GPIO3 belong to the RF
  coprocessor, so serial logging is disabled).

## Install

Any MQTT broker works — with Home Assistant's Mosquitto add-on (the common setup), the broker is
simply your HA host; a standalone broker works identically.

1. Copy `rf433-mqtt-bridge.yaml`, `rf433_scheduler.h`, and `examples/living-room.yaml` into one
   directory, renaming the example for your device.
2. Create `secrets.yaml` from `secrets.example.yaml`.
3. Adjust the substitutions (bridge id, area, broker, credentials). Set `default_bridge: "true"`
   on exactly one bridge in your home. Networking is DHCP by default; a commented `manual_ip`
   block in the example shows how to pin a static address.
4. Validate and flash with the hardware-tested ESPHome release:

```shell
uvx --from "esphome==2026.6.5" esphome config living-room.yaml
uvx --from "esphome==2026.6.5" esphome run living-room.yaml
```

First flash of a stock device requires serial; later updates are OTA.

## Development

The C++ target scheduler and the package contract are tested on the host:

```shell
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Roadmap

- **RX/listen (Phase 2):** stock ESPHome logs Portisch B1 raw-bucket captures but exposes no
  `on_bucket_received` trigger. A planned external `rf_bridge` component will publish B1 data to
  `rf433/<bridge_id>/rx` for controller-side decode. RX is not motor feedback and must never
  trigger TX.

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
