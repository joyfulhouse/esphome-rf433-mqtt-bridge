# RX / listening — hardware debug (2026-07-17) — RESOLVED (two root causes)

First on-hardware test of the state-sync **RX / idle-listen** path found TX fully working but
**no `/rx` at all** on physical remote presses. Root cause found and fixed the same day; the fix
is live on the Office canary and verified end-to-end with over-the-air captures.

## Root cause (confirmed live)

**Portisch silently reverts from bucket sniffing to standard sniffing the moment the host ACKs a
delivered capture — so listening died on the first frame it ever heard (usually ambient noise
within minutes of arming).**

Mechanism, from the Portisch sources (`RF_Bridge_main.c`, `RF_Handling.c`):

1. `PCA0_DoSniffing(active_command)` sets `uart_command`/`last_sniffing_command` to its argument
   but **returns the previous** `last_sniffing_command`.
2. The `0xB1` command handler assigns that return value back:
   `last_sniffing_command = PCA0_DoSniffing(RF_CODE_SNIFFING_ON_BUCKET);` — so after arming
   bucket mode, `last_sniffing_command` still points at `RF_CODE_RFIN` (standard mode, set by the
   preceding `0xA7`).
3. Portisch's handler for a **host ACK** (`AA A0 55`) is
   `last_sniffing_command = PCA0_DoSniffing(last_sniffing_command);` — with the stale value, this
   drops the radio back to **standard sniffing**. Bucket capture and delivery
   (`case RF_CODE_SNIFFING_ON_BUCKET:` in the main loop, keyed on `uart_command`) stop entirely.
4. Both stock ESPHome `rf_bridge` and our vendored fork ACKed every received frame (a leftover
   from the stock Itead firmware protocol), so the **first delivered capture — any ambient
   burst — killed listening**. The ESP-side `radio_sniffing_` stayed true; nothing re-armed until
   the next TX busy/idle cycle, which the next ambient capture would kill again.

This is why Tasmota's `RfRaw 177` streams continuously (Tasmota never ACKs sniffed frames) and
why AOK remote presses were never seen while occasional ambient junk appeared exactly once.

### The observation that cracked it

With logs attached, a PT2262-style probe frame transmitted from the Living Room bridge was
decoded by the *supposedly bucket-sniffing* Office bridge as a **standard-mode `0xA4` frame**
(`Received RFBridge Code: … code=0xAAAAAA`) — proving the radio and cross-house RF path were fine
and the EFM8BB1 was simply no longer in bucket mode. Working backwards: it had delivered one
ambient capture at `08:01:01` (`Rejected non-AOK RFBridge Bucket frame`), received our ACK, and
reverted.

Ruled out along the way (all with live evidence): consumer, AOK filter, scheduler gate,
`listen_enabled` propagation (verified in the compiled `main.cpp`), EFM8→ESP UART health (TX
`Action OK` ACKs present), B1 command support (ACKed, and bucket captures delivered), Portisch
bucket-capture requirements vs the AOK waveform (5140 µs preamble is a valid sync; 66-bit frame
fits the 112-byte capture buffer; ~2.2 repeats needed, our bursts carry 8), RF range (probe heard
across the house), and receiver hardware.

## Fix (deployed)

Two changes on `feat/state-sync-firmware`:

1. **The vendored `rf_bridge` component never writes ACKs for received frames** (removed from
   `finish_bucket_capture_` and the parse tail; `ack_()` deleted). Portisch deliveries are
   fire-and-forget — the main loop clears `RF_DATA_STATUS` and re-enables the capture interrupt
   immediately after `uart_put_RF_buckets` — so the ACK was at best useless and at worst the kill
   switch above.
2. **A 5 s idempotent B1 keepalive re-arm** in the dispatch tick (`RX_KEEPALIVE_MS`,
   `RxState::note_radio_armed`/`keepalive_due`, gated on `receive_idle()` so a frame mid-delivery
   is never clipped). This bounds any *remaining* silent exit from bucket mode — EFM8 watchdog
   reset, power glitch, a corrupted B1 command — to one keepalive period. Each re-arm costs the
   EFM8 ~10 ms of capture blackout (0.2% duty).

## Verification (2026-07-17, Office canary, firmware live)

Probes transmitted from the Living Room bridge (old firmware) as raw B0 with a **bogus remote
identity `a1b2c3:42`** (no house remote uses prefix `a1…`; STOP on an unpaired identity moves
nothing):

- Ambient capture rejected at `08:32:48` → listening survived (previously fatal).
- PT2262-style probe captured as a bucket frame and rejected by the AOK filter at `08:32:55` →
  listening survived.
- AOK probe → **`rf433/rf433-bridge-office/rx` published ~300 ms after TX** (`08:33:02`).
- Same AOK probe 12 s later → published again (continuity beyond first capture).
- Both captured frames **decode byte-exactly** to the transmitted payload
  (`prefix=0xa1b2c3 remote=0x42 chans=[1] cmd=0xdc50`) despite real-world bucket jitter
  (`026C→0122/0280` etc.) — full chain: synthesize → LR TX → air → Office capture → `/rx` →
  `decode_b0`.
- Keepalive visible every ~5 s (`Raw Bucket Sniffing on` + EFM8 `Action OK`).
- HA consumer received the unconfigured `/rx` frames and dropped them silently (correlate-first);
  no zemismart errors in the core log.

## Real-remote validation (2026-07-17, later the same day) — PASSED

A physical **kitchen remote ALL-channels UP press** was heard by the Office bridge across the
house and synced end-to-end with no MQTT command anywhere on the bus:

- `15:50:49Z` Office log: `Received RFBridge Bucket: AAB104142802760122141E38192A192A1A1A19292A1929292A192A192A19292A19292A19292929292A1A192A1A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A192A19292A1A1A192929292A1A1955`
  — **the first real OEM capture**; decodes to `prefix=0x5c8a92 remote=0x0d chans=[1..6]
  cmd=0xf4e1` = the calibrated Kitchen ALL/UP reference command, byte-exact against the
  Hubitat-era calibration.
- `15:50:48.970Z` `cover.kitchen_shades` → `opening`, motion-modeled to `open/100` over its 61 s
  travel; an MQTT subscription spanning the window shows **no `/tx` on any bridge** — the
  transition came from the heard press. An earlier heard DOWN produced the matching full
  `closing → closed` cycle.
- Heard UP on a fully-open cover and heard STOP on an idle cover are deliberate model no-ops and
  leave no visible trace — expected, observed.
- Use this capture to replace/augment the synthesized AOK fixtures in `tests/`
  (`TODO(hardware)` markers).

## Root cause #2 (same day): OEM truncated trailer — RESOLVED

Direct validation of the Office remote (`5cad7c:da`) exposed a second, remote-specific failure:
**every office press reached the bridge and was rejected by the AOK envelope filter.** Adding the
rejected frames' bytes to the DEBUG log (now permanent) showed the remote transmits **65 bit
pairs — 64 payload bits plus a trailer that captures as a single 0-read** — where the filter's
`B1_MIN_PULSE_BYTES = 67` and fixed 66-pair envelope walk demanded the nominal `[1, 0]` trailer.
The kitchen remote transmits the full trailer, which is why it synced first. The codec's
calibration decoder had documented this exact truncation ("legacy captures with a truncated
trailer") — the office remote is that remote; the tolerance just never reached the receive path.

Fix (firmware `0bfc787`, consumer `d658ea6`):

- firmware: `B1_MIN_PULSE_BYTES` 67→66; `is_aok_bucket_frame` accepts 66- and 65-pair encodings
  (longest first; a lone trailer 1-read and payload-only 64-pair frames stay rejected — neither
  occurs on air); rejected captures now log their bytes.
- consumer: `codec.decode_rx_capture` (trailer-tolerant, strict superset) used by
  `state_sync.frame_signature`; transport `encode_b0`/`decode_b0` stay strict. Echo comparison is
  unaffected: signatures are payload-field based.

**Office validation PASSED (16:20:58Z):** a physical office-remote ALL/UP press was accepted
(`Received RFBridge Bucket`), published on `/rx`, and **all seven office covers** flipped to
`opening` within 126 ms — including the `office_slider` group cover's first-ever state — and
completed on their travel models. STOP applied as an idle freeze; the remote's OEM follow-on
command (`cmd 0xdba2`, after UP) classified as non-movement and dropped. Live captures of all
three actions are pinned in both repos' test suites.

## Remaining before "synced blinds" is done

1. The multi-bridge listen rollout still runs through per-bridge channel discovery
   (`zemismart-private/ROLLOUT-office.md`) — and `zemismart-private/bridge-deploy/` still holds
   the OLD firmware; sync it with these fixes before flashing other bridges. (A 7th bridge,
   `rf433-bridge-kitchen`, was enrolled separately the same day with `listen:false`.)

## Environment (for the record)

- Bridge: `rf433-bridge-office` (10.100.5.162), Sonoff RF Bridge R2, esp8266, Portisch EFM8BB1.
- Firmware: `feat/state-sync-firmware` + this fix, ESPHome 2026.7.0, `listen_enabled: "true"`.
- Consumer: `feat/state-sync-consumer` (263e8ab) on `hass.joyful.house`.
- Diagnosis used no physical access: ESPHome API log streaming + HA websocket `mqtt/publish`
  /`mqtt/subscribe`, with the Living Room bridge as a remote-controlled RF source.
