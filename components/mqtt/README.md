# Vendored ESPHome mqtt component

All 53 files in this directory were copied from `esphome/components/mqtt` in **ESPHome
2026.7.3** (tag commit `985a08e2473d56c23f8ab31746a119fe5f5bbae9`). The upstream component
is licensed under ESPHome's MIT license.

This copy carries one behavioural change on top of upstream: the inbound payload guard
described below.

Note: the fleet's esphome-config CI currently builds against ESPHome 2026.7.2 and is
scheduled to bump to 2026.7.3 at Batch B rollout; the `mqtt` component is byte-identical
between those two releases, so this vendor commit's provenance and the fleet's eventual
runtime match exactly.

## Why this exists

The stock `mqtt` component has no bound on the declared size of an inbound message before
it allocates a reassembly buffer for fragmented payloads. This bridge's `/cmd` and `/tx`
topics only ever carry small, bounded payloads (see `MAX_B0_INPUT_CHARS`), so an
oversized or malformed inbound message — malicious or not — has no legitimate reason to
reach `payload_buffer_.reserve()`. The stock component cannot express that cap; it has to
be vendored to add it.

## Inbound payload guard patch

`mqtt_client.cpp`'s `setup()` installs an on-message lambda that reassembles fragmented
MQTT publishes into `payload_buffer_`. Before this patch, that lambda called
`payload_buffer_.reserve(total)` using the broker-declared `total` size with no upper
bound, so a hostile or errant tens-of-KB publish could exhaust the heap or stall the loop
past an armed fail-safe STOP deadline on the ESP8285.

The patch adds a check ahead of that `reserve()` call: any message whose declared `total`
exceeds `rf433::MAX_INBOUND_PAYLOAD` (4096 bytes, see `rf433_inbound_guard.h`) is dropped
before any buffer allocation happens, and the buffer is cleared so any further fragments
of that same oversized message (which all carry the same `total`) are dropped too, rather
than partially reassembled. A throttled warning (topic + declared size, once per 5 s) is
emitted via `ESP_LOGW` instead of one log line per fragment, so a flood of oversized
publishes cannot itself become log spam.

The accept/reject decision (`rf433::accept_inbound_payload`) and the log throttle
(`rf433::inbound_drop_log_due`) are pure functions in `rf433_inbound_guard.h`, so host
tests in `tests/test_firmware.py` can pin the constant and boundary cases without
building the full ESPHome toolchain.

Broker `max_packet_size` configuration remains recommended as defense in depth
independent of this guard.

## Rebase discipline

Rebase these files deliberately when changing the pinned ESPHome version; a local
external component shadows the complete upstream `mqtt` implementation, so an upstream
security fix or behavioural change will not reach this bridge automatically.
