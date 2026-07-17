# Vendored ESPHome rf_bridge component

`__init__.py`, `rf_bridge.h`, and `rf_bridge.cpp` were copied from
`esphome/components/rf_bridge` in **ESPHome 2026.6.5** (tag commit
`3bfbaaebf378e61ac0012ab5ece0014eab4227e9`). The upstream component is licensed under ESPHome's
MIT license.

This fork keeps the upstream namespace, schemas, actions, A4/A6 receive callbacks, and all transmit
methods intact. Its local changes are limited to:

- an `on_bucket_received` callback carrying compact uppercase `AAB1...55` hex;
- declared-length B1 framing and AOK envelope filtering in `rf_bridge_protocol.h`, including
  CANDIDATE/quiet disambiguation so an interior `0x55` at any allowed boundary cannot truncate the
  capture;
- **no ACKs are ever written for received frames** (upstream ACKs every completed non-ACK frame, a
  leftover from the stock Itead protocol). On Portisch every delivery is fire-and-forget, and a
  host ACK is consumed as `PCA0_DoSniffing(last_sniffing_command)` — which the `0xB1` command
  handler leaves at `RF_CODE_RFIN` — so a single ACKed capture silently reverts the radio to
  standard sniffing and ends bucket listening (root cause of the 2026-07-17 field failure where
  the first ambient capture deafened continuous listen). A `receive_idle()` accessor lets the
  bridge package's periodic B1 keepalive re-arm without clipping a frame mid-delivery;
- exact declared-length framing and bounds checks for A6/AB advanced receive data;
- a bounded 250 ms timeout for an in-progress B1 frame instead of the upstream 50 ms timeout; and
- an unconditional startup A7 stop-sniff plus partial-capture reset for ESP-only restarts.

The bridge package uses `on_bucket_received` only for the bounded Learn/onboarding flow:

- `{"action":"sniff","seconds":30}` on `rf433/<bridge_id>/cmd` starts or extends a sniff; integer
  values 1 through 60 select the window, with larger values hard-capped;
- `{"action":"sniff","seconds":0}` cancels immediately;
- `rf433/<bridge_id>/rx` publishes QoS 1, non-retained captures only while that sniff is active; and
- the callback never schedules or triggers TX.

Continuous listen and live state-sync are planned follow-up work and are not part of this firmware
slice. Real OEM capture decoding remains deferred hardware validation; current AOK fixtures are
synthesized from the documented envelope assumptions.

Rebase these files deliberately when changing the pinned ESPHome version; a local external
component shadows the complete upstream `rf_bridge` implementation.
