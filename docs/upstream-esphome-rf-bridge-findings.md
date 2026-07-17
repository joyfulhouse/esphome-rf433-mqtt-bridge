# Draft: upstream issue for `esphome/esphome` — two `rf_bridge` bugs with Portisch firmware

Status: DRAFT for owner review — not yet filed. Suggested title:
**`rf_bridge`: ACK-on-receive silently disables Portisch bucket sniffing; B1 parser truncates
at interior `0x55` bytes**

---

Both issues were found while running continuous bucket-sniff receive on Sonoff RF Bridge R2
hardware (EFM8BB1 + [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1)) and were
verified live on hardware on 2026-07-17. Working fixes are deployed in a vendored fork:
<https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/tree/main/components/rf_bridge>.

## 1. ACKing received frames reverts Portisch to standard sniffing (kills `start_bucket_sniffing`)

`RFBridgeComponent::parse_bridge_byte_` ACKs every completed non-ACK frame
(`AA A0 55` back to the coprocessor). That protocol is correct for the stock Itead firmware, but
on Portisch it is destructive:

- Portisch's `0xB1` (`RF_CODE_SNIFFING_ON_BUCKET`) command handler assigns
  `last_sniffing_command = PCA0_DoSniffing(RF_CODE_SNIFFING_ON_BUCKET)` — and `PCA0_DoSniffing`
  **returns the previous** `last_sniffing_command`, so after arming bucket mode the variable
  still points at `RF_CODE_RFIN` (standard sniffing).
- Portisch's host-ACK handler is
  `last_sniffing_command = PCA0_DoSniffing(last_sniffing_command);` — i.e. the first ACK the
  host sends after a delivered capture re-arms **standard** sniffing and exits bucket mode.

Net effect: after `start_bucket_sniffing()`, the first delivered capture (any ambient RF burst)
plus ESPHome's automatic ACK silently ends bucket sniffing. Nothing tells the host; the radio
just stops delivering B1 frames. This is why Tasmota's `RfRaw 177` streams continuously (Tasmota
never ACKs sniffed frames) while ESPHome-based bucket sniffing appears to "work once, then die."

No Portisch delivery path waits for a host ACK (its main loop clears `RF_DATA_STATUS` and
re-enables the capture interrupt immediately after `uart_put_RF_buckets`), so the ACK is at best
redundant on Portisch. Suggested fix: do not ACK received frames (or make ACK-on-receive
configurable for stock-firmware users).

## 2. B1 bucket frames truncate at the first interior `0x55`

`parse_bridge_byte_`'s `RF_CODE_RFIN_BUCKET` case ends the capture at the first `0x55` byte, but
`0x55` is a legal byte inside B1 bucket tables and pulse data (e.g. a bucket duration of
`0x0155` µs, or the pulse nibble pair `5|5`). Real captures containing interior `0x55` bytes are
truncated into malformed frames.

B1 frames carry a declared bucket count at `raw[2]` (`AA B1 <count> <count×2 bucket bytes>
<pulse bytes> 55`), so the bucket table length is known; only the pulse-data length needs an
end-byte + quiet heuristic. The vendored fork frames by declared table length and defers
ambiguous short endings until UART quiet.

## Environment

- Sonoff RF Bridge R2 (EFM8BB1, Portisch), ESPHome 2026.6.5 / 2026.7.0, `uart` @ 19200 on
  GPIO1/GPIO3, `logger: baud_rate: 0`.
- Repro for (1): `start_bucket_sniffing()` on boot, wait for one ambient capture, observe no
  further `Received RFBridge Bucket` logs; press any 433 MHz remote — nothing. Remove the ACK
  and captures continue indefinitely (a periodic idempotent `B1` re-arm also guards against
  coprocessor watchdog resets).
- Repro for (2): any remote whose capture contains an interior `0x55` (bucket durations around
  341 µs, or matching pulse nibbles).
