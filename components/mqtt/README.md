# Vendored ESPHome mqtt component

All 53 files in this directory were copied from `esphome/components/mqtt` in **ESPHome
2026.6.5** (tag commit `3bfbaaebf378e61ac0012ab5ece0014eab4227e9` — the same release tag
`components/rf_bridge/` is pinned to). The upstream component is licensed under ESPHome's
MIT license.

This copy is currently **byte-identical to upstream**. No behavioural changes have been
made yet.

## Why this exists

The stock `mqtt` component has no bound on the declared size of an inbound message before
it allocates a reassembly buffer for fragmented payloads. This bridge's `/cmd` and `/tx`
topics only ever carry small, bounded payloads (see `MAX_B0_INPUT_CHARS`), so an
oversized or malformed inbound message — malicious or not — has no legitimate reason to
reach `payload_buffer_.reserve()`. The stock component cannot express that cap; it has to
be vendored to add it.

## Forthcoming behavioural diff

A guard patch to `mqtt_client.cpp`'s inbound fragment-assembly path is described in
`docs/design/2026-08-02-batch-b-safety-design.md` §1 and lands in a later task in this
series, not this commit. Once applied, this README will be updated to record the diff in
the same style as `components/rf_bridge/README.md`. In summary, the patch will:

- drop any message whose declared total payload size exceeds `MAX_INBOUND_PAYLOAD`
  (4096 bytes) before any buffer allocation, and discard subsequent fragments of the same
  oversized message;
- emit a throttled warning (topic + declared size) rather than one log line per fragment;
- delegate the accept/reject decision to a pure function in a new header
  (`rf433_mqtt_guard.h`), so host tests can pin the constant and boundary cases without
  building the full ESPHome toolchain.

Broker `max_packet_size` configuration remains recommended as defense in depth
independent of this guard.

## Rebase discipline

Rebase these files deliberately when changing the pinned ESPHome version; a local
external component shadows the complete upstream `mqtt` implementation, so an upstream
security fix or behavioural change will not reach this bridge automatically.
