# Vendored ESPHome mqtt component

All 53 upstream files in this directory were copied from `esphome/components/mqtt` in
**ESPHome 2026.9.0** (tag commit `c6e4c87e525dd343e470d8dea368957a002f6505`). The upstream component
is licensed under ESPHome's MIT license.

History: first vendored from 2026.7.3; re-vendored from 2026.9.0 because 2026.9 removed core
helpers the 2026.7.3 copy called (`make_name_with_suffix`), so the old copy no longer compiled.
The re-vendor copied every C++ file wholesale, re-applied the guard below, and ported upstream's
`__init__.py` changes (`CONF_DISCOVER_IP` from `esphome.const`, the `esp-tls` IDF include, and the
`USE_<ENTITY>` source-file filter) onto the restyled module. This copy therefore requires ESPHome
2026.9.0 or newer.

This copy carries one behavioural change on top of upstream: the inbound payload guard
described below.

`__init__.py` additionally carries a **non-behavioural** restyle: it is the only Python file
in this directory, so the repository's `ruff` gate (`select = ["ALL"]`) reaches it. It gained
a module docstring, type annotations, per-function docstrings, and 100-column formatting, and
its `to_code` was split into `_add_platform_libraries`, `_add_discovery`,
`_add_lifecycle_messages`, `_add_idf_options`, and `_add_triggers` to clear the complexity
rules. The split is pure code motion -- the order of `cg.add(...)` emissions, and therefore
the generated C++, is unchanged. Upstream's `AUTO_LOAD` callable is preserved as a module
attribute aliasing `_auto_load`, because ESPHome's loader resolves that name by string.

Note: any fleet esphome-config CI that builds these sources must use ESPHome 2026.9.0 or
newer; older releases lack core APIs this copy calls.

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

When rebasing `__init__.py`, take upstream's file wholesale and re-apply the restyle noted
above rather than merging into the restyled copy -- the diff against upstream is formatting
plus the `to_code` split, so a clean re-copy followed by `ruff check --fix` and
`ruff format` is cheaper and safer than resolving conflicts hunk by hunk.
