# Batch B safety hardening — design

**Date:** 2026-08-02
**Scope:** firmware v1.4.0 (this repo) + zemismart-blinds v0.8.0 (consumer)
**Driver:** the 2026-07-24 release-hardening review identified three platform-boundary
failure paths that gate publishing this firmware for strangers to flash. Batch A landed in
v1.3.0; this design closes the remaining three ("Batch B") ahead of the Home Assistant
community announcement.

The three blockers, each a way the RAM-held fail-safe STOP (or the command model) can be
lost at a boundary the scheduler cannot see:

1. **Inbound MQTT payload assembly is unbounded** — ESPHome's stock `mqtt` component
   executes `payload_buffer_.reserve(total)` with a *broker-declared* size before any of
   our validation runs (`mqtt_client.cpp:48-63` in ESPHome 2026.6.5). A tens-of-KB publish
   during a timed move can exhaust ESP8285 heap or stall past a STOP deadline. The JSON
   layer's 5120-byte document cap applies only after assembly.
2. **OTA has no scheduler interlock beyond the v1.3.0 early-STOP flush** — an update
   mid-move interrupts motion (fail-safe direction, but still interrupts), and a *failed*
   OTA leaves `ota_active` latched true forever, rejecting all `/tx` until manual reboot
   (latent bug: `ota_active` is set in `on_begin` and never reset on any path).
3. **A retained `/tx` replays after reboot** — the ESP8266 MQTT backend discards the
   retained flag, and the 64-entry command-id dedup ring is RAM-only, so it cannot
   suppress a replay across a reboot. Today this is only mitigated by documentation
   ("never publish retained tx").

## 1. Inbound MQTT payload cap (vendor now, upstream later)

Vendor ESPHome 2026.6.5's `mqtt` component into `components/mqtt/`, following the exact
precedent of `components/rf_bridge/` (README records the source commit and the full
behavioural diff). The patch, in the inbound fragment-assembly path of `mqtt_client.cpp`:

- If a message's declared total payload size exceeds **`MAX_INBOUND_PAYLOAD = 4096`
  bytes**, the message is dropped *before* `payload_buffer_.reserve()` — no allocation,
  no assembly, no delivery. Subsequent fragments of the same oversized message are
  likewise discarded.
- The drop emits a throttled warning log (topic + declared size), not one log per
  fragment.
- The accept/reject decision is a pure function in a new header (`rf433_mqtt_guard.h`,
  same include-from-host pattern as `rf433_scheduler.h`) so host tests pin the constant
  and the boundary cases (4096 accepted, 4097 dropped, fragment-continuation dropped).
- A pytest asserts the vendored `mqtt` source actually calls the guard, so a future
  re-vendor from upstream cannot silently lose the patch.

Rationale for 4 KiB: the largest legitimate `/tx` is bounded by `MAX_B0_INPUT_CHARS`
(1040) for each of `raw`/`trailer_raw`/`stop_raw` plus small fields — comfortably under
4 KiB; `/cmd` is far smaller. All other subscribed topics are ESPHome-internal and small.

Broker `max_packet_size` guidance remains in the README as defense in depth. After
release, file an upstream ESPHome issue/PR proposing a configurable inbound cap so the
vendored copy can eventually be retired; tracked as a follow-up GitHub issue, not a gate.

## 2. OTA bounded wait-for-idle, then flush

`ota.on_begin` behaviour becomes:

1. Latch `ota_active = true` — new `/tx` commands are rejected immediately (unchanged).
2. **Wait up to `OTA_IDLE_WAIT_MS = 30000`** for `scheduler.idle() && rf_air_clear()`.
   While `on_begin` blocks, ESPHome's 5 ms interval tick does not run, so the wait loop
   must pump the scheduler's own dispatch path itself (the same code the tick lambda
   calls). In-flight trains therefore complete normally and their armed STOPs fire **at
   their real deadlines** — motion is not interrupted in the common case.
3. If still not idle at the deadline (e.g. an hour-scale `stop_after_ms`), fall back to
   the v1.3.0 behaviour: `drain_armed_stops()` and synchronously transmit every early
   STOP, waiting through physical RF occupancy between frames.
4. **Bugfix:** `ota.on_error` resets `ota_active = false`, so a failed transfer no longer
   leaves the bridge rejecting `/tx` until someone reboots it. (Success reboots, so no
   `on_end` handling is needed.)

30 s covers the realistic envelope: a full timed travel is ~16 s and the full absorption
span at production `repeats=3` is ~9.5 s. The fallback keeps worst-case behaviour exactly
as shipped today. Unexpected reset/power loss remains out of scope by design — armed STOP
state is deliberately RAM-only.

## 3. Retained-`/tx` session binding (contract v3)

New **required** field on `/tx`: `boot` (uint32), which must equal the bridge's current
`boot_id` (re-randomized on every boot; already published on retained `/info` and on
`/status`/`/rx`).

- Missing, non-uint32, or mismatched `boot` → command rejected with
  `{"status":"rejected","reason":"boot_mismatch","command_id":…}` on `/status`, plus a
  warning log. Validation order: after `command_id` extraction (so the rejection can name
  it), before any scheduling side effects.
- A retained `/tx` replayed after a reboot now carries the previous boot's value and is
  rejected — the blocker closes structurally rather than by documentation.
- `/info` contract version bumps to `v: 3`. README topic tables in both repos update.
  Verified 2026-08-02: the consumer stores `contract_v` for diagnostics only and does not
  gate on its value, so the bump requires no consumer-side version handling.
- `/cmd` stays unbound: replayed sniff/cancel/disarm commands are benign (bounded sniff,
  no motion; disarm of an already-gone command id is a no-op) — documented rationale.

**Consumer change (zemismart-blinds v0.8.0):** stamp `boot` into every `/tx` body at the
finalize-and-publish seam (`transport.py`), sourced from the bridge registry's
already-strict `bridge.boot` (`_strict_uint32`). If the transport has no learned boot for
a bridge, it must not publish to it — such a bridge has never delivered a retained
`/info` and is not meaningfully provisioned. Implementation verifies (and tests) that the
routing layer already guarantees this, adding the guard if it does not.

**Rollout order (fleet-safe):**

1. Ship + deploy blinds v0.8.0 fleet-wide — old firmware (≤ v1.3.0) ignores unknown
   `/tx` keys, so stamping `boot` early is a no-op against the live fleet.
2. Then firmware v1.4.0 enforces the field (canary first — §4).

## 4. Validation and rollout — office bridge as canary, no bench hardware

Host gate (all before any flash): extended host tests via the existing YAML-lambda
extraction harness (`tests/test_firmware.py` `_firmware_lambda`) — OTA wait-for-idle with
a fake clock (busy → waits, STOPs at natural deadlines, no early STOP inside the window;
past deadline → flush; `on_error` resets `ota_active`), boot-binding accept/reject
matrix, `rf433_mqtt_guard.h` boundaries, vendored-diff presence check. Mutation-verify
the key guards (delete the guard call → the pinning test must fail). `uv run ruff check`,
`uv run pytest`, and the full local `esphome compile` (the CI compile has no substitute
for a local close of this gate).

Canary (during an idle window, using **bogus-identity frames** so no blind physically
moves):

1. OTA `rf433-bridge-office` to the v1.4.0 candidate.
2. Oversized publish (declared ≫ 4 KiB) to its `/tx` → dropped, bridge stays online,
   heap logged before/after.
3. Start a bogus timed move, begin an OTA → transfer waits, STOP fires at its natural
   deadline, update completes.
4. `/tx` with a stale/wrong `boot` → `boot_mismatch` rejection on `/status`. Then the
   real thing: publish a **retained** bogus `/tx`, reboot the bridge, confirm zero
   scheduling + a rejection, then clear the retained message.
5. Fleet OTA the remaining 6 bridges, soak, tag + release v1.4.0.

## Public tracking

File the three blockers as GitHub issues on this repo before the implementation PR(s),
closed by the work — the community announcement then has an honest public audit trail
instead of an empty tracker that overstates readiness.

## Out of scope

- Durable (flash-persisted) STOP state across unexpected resets — rejected by design.
- Structural refactors already deferred with reason (B1 analyzer unification, command
  container, `ReplayState` enum, `Frame` struct).
- The Home Assistant community announcement itself — follows the fleet soak.
