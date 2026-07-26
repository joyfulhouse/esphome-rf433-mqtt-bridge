# ESPHome RF433 MQTT Bridge

Turn a **Sonoff RF Bridge R2** into a dumb, reliable relay that puts 433.92 MHz radio signals on
air for Home Assistant — the hardware half of controlling AOK/Zemismart roller blinds without a
cloud, app, or hub.

[![License][license-shield]](LICENSE)
[![CI][ci-shield]][ci]
[![Project Maintenance][maintenance-shield]][maintenance]
[![GitHub Sponsors][sponsors-shield]][sponsors]
[![Ko-fi][kofi-shield]][kofi]

---

## Is this for you?

Home Assistant can't speak 433 MHz on its own. This firmware turns a cheap Sonoff RF Bridge into a
radio it *can* drive — a **deliberately dumb** relay that takes MQTT commands and transmits them,
and nothing more.

**You probably got here from the [zemismart-blinds][zemismart-blinds] integration.** If so, your
path is:

1. **Build a bridge** — flash this firmware onto a Sonoff RF Bridge R2. That's what
   **[HARDWARE.md](HARDWARE.md)** is for. It's the hardest step in the whole product; the guide
   walks you through it slowly.
2. **Go back to the integration** and add your blinds.

The bridge holds **no device codes and no cover entities.** A controller — the zemismart-blinds
integration — publishes correlated JSON commands; the bridge validates them, schedules them, and
transmits Portisch **B0** raw frames. All the smarts about blinds live in the controller.

## What it does

- **Takes commands over MQTT and transmits them** as raw 433.92 MHz frames, one at a time.
- **Confirms what it did** — every command gets an `accepted`/`rejected` answer, then `started`
  when the frame actually goes out.
- **Stops blinds on its own** if the controller disappears mid-move, using a fail-safe STOP deadline
  armed on the device.
- **Can listen** for physical remote presses (opt-in, off by default) so Home Assistant follows
  along when someone uses the wall remote.

<details>
<summary><b>The full capability list (what "reliable" actually means here)</b></summary>

- **Correlated acknowledgements** — every command carries a `command_id`; the bridge answers
  `accepted`/`rejected` on admission and `started` on the first actual RF dispatch. `started`
  means the frame was handed to the RF coprocessor over UART (ESPHome's `send_raw` does not wait
  for an EFM8BB1 ack); it is proof of dispatch, not of RF emission.
- **Per-target scheduling** — one RF frame on air at a time, while each target keeps its own
  repeat phase and an **absolute monotonic STOP due time** (`stop_after_ms` + `stop_raw`) so a
  partial movement is stopped by the bridge itself even if the controller disappears. The due
  time raises STOP to safety priority; it is not a guaranteed on-air timestamp because an
  already-transmitting frame and earlier safety STOPs must physically clear first.
- **Latest command wins** — a new command whose channels overlap an active target on the same
  remote displaces it: the displaced command's pending fail-safe STOP is flushed on air first,
  and a `displaced` status with its `command_id` tells the controller to retire its motion model.
  `displaced` is published at admission of the replacing command and, like `started`, carries
  `age_ms`, `t`, and `boot` anchored on the displacement instant (`displacement = t - age_ms`), so
  the controller can *measure* its post-displacement flush-tolerance window rather than budget a
  wider one. The first flushed STOP bypasses the discretionary `repeat_gap_ms` floor, but still
  waits for physical RF occupancy and earlier safety STOPs. A replacement overlapping N active
  targets can therefore put the last first-STOP copy roughly N physical frame-occupancy intervals
  after `displaced`; the status is not an on-air timestamp.
- **Duplicate suppression** — a ring of recent `command_id`s suppresses QoS-1 broker redeliveries
  and same-boot retained replays. The ring lives in RAM, so a retained `tx` command *can* replay
  after a reboot: **retained `tx` publishes are unsupported and dangerous — never publish them.**
- **Bounded memory and latency** — admission enforces both a stored-frame heap budget and a
  4,000 ms aggregate first-STOP occupancy budget. Lifecycle statuses use a 32-event FIFO outbox;
  duplicate command/status transitions coalesce, sustained overflow drops the oldest event, and
  the bridge logs a monotonic drop counter.
- **Retained discovery** — the beacon publishes retained `availability` (birth/will) and `info`
  (area, default flag, boot session, listen capability, and contract version) so controllers can
  discover online bridges and prefer one in the same area.
- **Strict input validation** — B0 frames, target keys, repeat counts, and stop deadlines are
  validated on-device before anything reaches the RF coprocessor.
- **Time-boxed onboarding capture** — validated Portisch **B1** raw-bucket captures are published
  for controller-side decoding during an active sniff of at most 60 seconds. Continuous idle
  listening uses the same observation-only path only when explicitly enabled; it is off by default.

</details>

## Hardware

**→ [HARDWARE.md](HARDWARE.md) is the complete buy-it-and-flash-it guide** — board revisions,
flashing Portisch onto the RF coprocessor, and replacing Tasmota with this package. Start there if
you are setting up a bridge for the first time.

> ⚠️ **Check the board revision before you buy.** Only R2 **V1.0/V2.0** boards (Silicon Labs
> **EFM8BB1** chip) work. The 2022+ **R2 V2.2** uses an **OB38S003**, which cannot run the required
> Portisch firmware. Listings rarely say which revision they ship, so new stock is a gamble —
> secondhand V1.0/V2.0 units are the safe buy. Full detail in
> [HARDWARE.md → Will my board work?](HARDWARE.md#will-my-board-work).

<details>
<summary><b>The short version of the hardware requirements</b></summary>

- **Sonoff RF Bridge R2** ([itead.cc](https://itead.cc/product/sonoff-rf-bridge-433/), 433 MHz
  variant) with the EFM8BB1 RF coprocessor flashed to
  [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1) — required, because the stock
  RF firmware cannot transmit raw B0 buckets.
- **Supported board revisions: R2 V1.0/V2.0 (EFM8BB1)** — what this project is validated on. The
  2022+ **R2 V2.2** replaced the EFM8BB1 with an OB38S003, which cannot run Portisch and is
  unsupported. New stock may be either revision and listings rarely say which, so check before
  buying.
- The ESP8285 runs this ESPHome package (`rf_bridge:` UART @ 19200; GPIO1/GPIO3 belong to the RF
  coprocessor, so serial logging is disabled). Tasmota is used only as a one-time tool to flash
  the coprocessor — it cannot drive the [zemismart-blinds][zemismart-blinds] integration itself.

</details>

## Install

**New bridge?** Flash the RF coprocessor first — [HARDWARE.md](HARDWARE.md) walks through it.
The steps below cover the ESP8285 side, and assume the coprocessor already runs Portisch.

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

   <details>
   <summary><b>Using only the hardened <code>rf_bridge</code> component elsewhere</b></summary>

   To use only the hardened `rf_bridge` component in an unrelated ESPHome config (Portisch bucket
   receive with correct B1 framing, no delivery ACKs, `on_bucket_received`), pull it straight from
   this repository instead of vendoring:

   ```yaml
   external_components:
     - source: github://joyfulhouse/esphome-rf433-mqtt-bridge@v1.2.2
       components: [rf_bridge]
   ```

   </details>
2. Create `secrets.yaml` from `secrets.example.yaml`.
3. Adjust the substitutions (bridge id, area, broker, credentials). Set `default_bridge: "true"`
   on exactly one bridge in your home. Networking is DHCP by default; a commented `manual_ip`
   block in the example shows how to pin a static address.
4. Validate and flash with the hardware-tested ESPHome release:

   ```shell
   uvx --from "esphome==2026.6.5" esphome compile living-room.yaml
   uvx --from "esphome==2026.6.5" esphome run living-room.yaml
   ```

First flash of a stock device requires serial (5-pin header beside the power switch, switch slid
toward it, button held while powering on — [HARDWARE.md → Step 2](HARDWARE.md#step-2--flash-this-package-to-the-wi-fi-chip));
later updates are OTA.

## MQTT topic contract

This is a **genuine API**: the zemismart-blinds controller depends on these exact topics and
payloads. All topics live under the fixed `rf433/` root.

| Topic | Direction | Payload |
|---|---|---|
| `rf433/<bridge_id>/availability` | bridge → broker (QoS 0, retained) | `online` / `offline` |
| `rf433/<bridge_id>/info` | bridge → broker (QoS 0, retained) | `{"bridge":"rf433-bridge","area":"living_room","default":false,"boot":2718281828,"listen":false,"v":2}` |
| `rf433/<bridge_id>/tx` | controller → bridge (QoS 1, non-retained) | JSON transmit command (below) |
| `rf433/<bridge_id>/status` | bridge → controller (QoS 1, non-retained) | `{"status","command_id"[,"reason"][,"age_ms"][,"t"][,"boot"]}` |
| `rf433/<bridge_id>/rx` | bridge → broker (QoS 1, non-retained) | `{"frame":"AAB1...55","t":123456,"boot":2718281828}` |
| `rf433/<bridge_id>/cmd` | controller → bridge (QoS 1, non-retained) | bounded sniff/cancel or disarm command (below) |

`status` is one of `accepted`, `rejected` (with `reason`), `started` (first RF dispatch),
`displaced` (a newer overlapping command replaced this one), or `disarmed`.

> ⚠️ **Authenticated MQTT is required.** Never grant anonymous access, and apply the least-privilege
> broker ACL matrix below. `retained tx` publishes are unsupported and dangerous — never publish
> them.

<details>
<summary><b>Broker security and least-privilege ACL matrix</b></summary>

Use distinct controller and bridge principals where the broker supports them, never grant anonymous
access, and apply this least-privilege broker ACL matrix:

| Principal | May publish | May subscribe |
|---|---|---|
| Controller | `rf433/<bridge_id>/tx`, `rf433/<bridge_id>/cmd` | `rf433/<bridge_id>/status`, `rf433/<bridge_id>/rx`, `rf433/<bridge_id>/info`, `rf433/<bridge_id>/availability` |
| Bridge `<bridge_id>` | its own `status`, `rx`, `info`, and `availability` topics | its own `tx` and `cmd` topics |

No bridge principal should be able to publish `/tx` or `/cmd`, and no controller principal should
be able to publish bridge-originated `status`, `rx`, `info`, or `availability`.

Continuous receive is a household activity stream. Keep it opt-in and non-retained, and apply this
authenticated least-privilege matrix.

</details>

<details>
<summary><b>Status semantics: acknowledgements, clocks, and redelivery</b></summary>

QoS-1 broker redeliveries are answered idempotently: an already-admitted `command_id` gets its
`accepted` (and, if RF already started, `started`) statuses replayed instead of a fresh rejection.
Every `started`, both fresh and replayed, carries `t`, `age_ms`, and `boot`. `t` is the publish time
on the bridge clock and `age_ms` is measured from the command's one stored dispatch instant, so
`handoff = t - age_ms` modulo the 32-bit clock range. The controller anchors its motion model at
that handoff. `displaced` carries the same three fields anchored on the *displacement* instant
(`displacement = t - age_ms`), fresh and replayed alike — including a redelivery arriving while the
displaced command's owed STOP is still draining, which reports the age since the original
displacement, not 0. The one exception is a `command_id` that reached the terminal displaced/disarmed
state by an explicit `disarm` (not by displacement): it has no displacement instant, so its replayed
`displaced` omits `age_ms`/`t`/`boot` rather than reporting a fabricated age of 0.

Note what the displacement instant is: the **admission/decision** instant, not the moment the owed
fail-safe STOP reaches air. That follows later, behind the in-flight frame's RF occupancy and any
earlier displaced STOPs still draining — the same way `started` anchors the UART handoff rather
than the end of the train. A controller sizing a post-displacement window from this field must add
that drain latency, or it will open the window early. Duplicate/rejection
memory is authoritative for live commands and for the 64 most
recent same-boot command IDs. Within that window, a `command_id` rejected by a state-dependent
admission check (scheduler full, storage, or first-STOP budget) is re-rejected rather than silently
admitted later. The ring is RAM-only and older completed IDs can be evicted.

Failed lifecycle publishes are queued in the bounded outbox and retried on the 5 ms tick/reconnect,
FIFO across commands and in lifecycle order within one `command_id`. This keeps a failed or replayed
`accepted` ahead of its `started`; successfully delivered events are removed and are not republished
by the outbox.

</details>

<details>
<summary><b>Command body on <code>tx</code></b></summary>

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

`target` is `prefix:remote_id:channels`. Hex parsing is case-insensitive and canonical uppercase
is used internally; channels must be strictly increasing values from 1 through 16.
`trailer_raw`, `stop_after_ms`, and `stop_raw` are optional; a timed command requires `stop_raw`.
The bridge validates JSON shape, bounds, and B0 structure, but deliberately trusts the authenticated
controller for frame semantics: it cannot prove that `raw`, `trailer_raw`, and `stop_raw` address
the same physical blind or match the declared `target`.

</details>

<details>
<summary><b>Time-boxed onboarding sniff</b></summary>

Continuous receive is **default OFF** because `listen_enabled` defaults to `"false"`. This is a
privacy boundary: ambient 433 MHz traffic can identify nearby remotes and activity. On startup the
bridge unconditionally sends Portisch stop-sniff (A7) and clears any partial B1 capture, including
after an ESP-only restart where the independently running EFM8BB1 may still be sniffing.

`/cmd` accepts exactly one action. Publish `{"action":"sniff","seconds":30}` at QoS 1 with retain
disabled to start or extend a bucket sniff. Integer values from 1 through 60 select the window;
larger integers are hard-capped at 60, and another positive command never shortens an active
window. Positive commands are limited to one every 250 ms.
`{"action":"sniff","seconds":0}` immediately cancels the sniff, clears its bounded state, and sends
A7 on the next physical-state reconciliation if the radio is in bucket mode.
Cancellation is exempt from the rate limiter. A replayed retained positive command is benign because
every sniff auto-expires, but controllers must still publish `/cmd` with retain disabled.

Whenever physical bucket sniffing is active — for a bounded onboarding window or opt-in idle listen —
every accepted AOK-prefiltered B1 capture is forwarded to `/rx` as QoS 1, non-retained JSON. `frame`
is the compact uppercase Portisch frame, including `AAB1` and the final `55`; `t` is the bridge's
`millis()` value when the callback runs, and `boot` identifies that boot session. A failed MQTT
enqueue is logged.

RX is observation only, not motor feedback. The receive handler never calls `send_raw`, enters the
TX scheduler, or otherwise triggers TX.

</details>

<details>
<summary><b>Opt-in state-sync primitives</b></summary>

The state-sync firmware primitives ship now behind the compile-time `listen_enabled` substitution,
which defaults to `"false"`. When enabled, a centralized 5 ms reconciler enters bucket sniff only
while the scheduler is idle and the RF channel is clear, yields the radio before TX, and resumes
listening afterward. Continuous `/rx` observations remain QoS 1, non-retained; the bridge never
transmits in response to a heard frame.

The additional MQTT surface is:

- `/status` `started` always carries `t`, `age_ms`, and `boot`; use `handoff = t - age_ms` for the
  original UART handoff instant. `displaced` carries the same fields anchored on the displacement
  instant (`displacement = t - age_ms`) — except when the terminal state was reached by `disarm`,
  which has no such instant and so omits them.
- Retained `/info` advertises `boot`, `listen`, and `v` (`2` for this contract), allowing a controller
  to discover which bridges participate without waiting for traffic.
- Publish `{"action":"disarm","command_id":"move:42"}` to `/cmd` to cancel every future scheduled
  frame for that command. It emits no RF. The bridge acknowledges every valid request, including an
  already-unknown id, with `{"status":"disarmed","command_id":"move:42","t":123456,"boot":2718281828}`
  on `/status`.

Treat `t` as a `millis()` clock that is monotonic modulo `2^32`; equal stamps are allowed and wrap is
interpreted with serial-number arithmetic. Pair every timestamp with `boot`, and discard correlation
state when that session value changes.

</details>

<details>
<summary><b>Frame dispatch pacing and OTA behaviour</b></summary>

Frame dispatch is paced by computed airtime: the EFM8BB1 transmits each B0 frame blocking
(embedded repeats included) behind a small UART ring, so the scheduler holds the next handoff
until the previous frame's UART serialization and air complete. For normal ACTION/TRAILER work,
`repeat_gap_ms` is an additional discretionary floor, clamped to `0…60,000 ms`; due and displaced
STOPs bypass that floor but never physical occupancy. A typical AOK frame with the production
embedded repeat of 8 occupies ~560 ms of air, and controller-level `repeats` multiply that — keep
the product modest, since the bridge cannot listen while transmitting.

At an ESPHome OTA `on_begin`, the bridge stops accepting new `/tx`, synchronously sends one early
STOP for every already-armed timed command (waiting through physical RF occupancy between frames),
and clears the scheduler before the blocking transfer. This mitigates requested OTA updates; it is
not durable STOP persistence or a full idle-only OTA interlock. Armed STOP state is RAM-only by
design, so an unexpected reset or power loss still cannot preserve it.

</details>

> **HARDWARE-VALIDATED (2026-07-17):** end-to-end state sync runs in production on a
> seven-bridge fleet; physical remote presses mirror into the controller within ~150 ms.
>
> <details>
> <summary>What the hardware spike settled</summary>
>
> The hardware spike also settled every previously deferred physical question, twice against
> intuition: **ACKing received B1 frames is not benign** — a host ACK makes Portisch re-arm its
> stale `last_sniffing_command` and silently revert to standard sniffing, killing listening on
> the first heard frame, so this component never ACKs deliveries; and **real OEM captures do not
> all carry the `[1, 0]` trailer** — some remotes transmit 65 bit pairs with a single trailing
> 0-read, which the filter now accepts. One golden UP fixture derived from an OEM capture is
> pinned with a synthetic remote identity while preserving its field bucket jitter and truncated
> trailer; synthesized fixtures cover the broader envelope behavior. A 5 s idempotent B1
> keepalive bounds any remaining silent bucket-mode exit (e.g. an EFM8 watchdog reset) to one
> period.
>
> </details>

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

State sync shipped and is hardware-validated in production (see the note above); it stays behind
`listen_enabled` because continuous receive is a household activity stream that should be an
explicit choice.

<details>
<summary><b>Known deferred work, kept visible rather than forgotten</b></summary>

- **Structural cleanup on the RF path** — B1 analyzer unification and a single command container
  (behaviour-preserving, but churn on a live-validated path; wants hardware re-validation).
- **Clarity refactors** — a `ReplayState` enum and a `Frame{raw, airtime}` struct.
- **Analyzed and judged negligible** — disarm-tombstone ring eviction and 10 s watchdog sampling
  granularity.

</details>

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
