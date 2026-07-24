# Hardware and Flashing Guide

Everything needed to turn a stock Sonoff RF Bridge into a bridge this package can drive:
which board to buy, how to tell the revisions apart, and how to flash both of its chips.

Budget about an hour for your first bridge. It requires a USB-to-serial adapter and, for the
one-time RF-coprocessor flash, **soldering two short jumper wires**.

## 1. The hardware

| | |
|---|---|
| **Board** | Sonoff RF Bridge **R2** (433 MHz variant — not the 315 MHz one) |
| **Where** | [itead.cc — Sonoff RF Bridge 433](https://itead.cc/product/sonoff-rf-bridge-433/) and the usual marketplaces |
| **Validated on** | R2 **V1.0 / V2.0** boards (EFM8BB1 coprocessor) — seven of them run this firmware in the author's house |
| **Not supported** | R2 **V2.2** (2022 and later) |

> **Read this before buying.** Sonoff silently changed the RF coprocessor in 2022. Boards up to
> R2 V2.0 use a **Silicon Labs EFM8BB1**, which runs the Portisch firmware this package depends
> on. The **R2 V2.2 replaced it with an OB38S003**, which cannot run Portisch — that revision is
> unsupported here. New stock from any seller, including the link above, may ship either
> revision, and the listing does not say which. **This project has only been tested on the older
> EFM8BB1 boards**; treat a fresh purchase as a gamble unless the seller confirms the revision or
> you are willing to return it.
>
> Secondhand or old-stock R2 V1.0/V2.0 units are the safe buy. Check listing photos for the
> chip marking before committing.

### Identifying your board

Open the case (the screws sit under the rubber feet) and read the chip markings next to the RF
section:

- **`EFM8BB1`** → supported.
- **`OB38S003`** → not supported by this project. (Portisch has separate OB38S003 firmware, but
  it needs an external programmer and is not part of this package's validated path.)
- The board silkscreen also prints the revision, e.g. `RF-Bridge-R2 V2.0`.

## 2. Why there are two firmwares

The bridge contains **two** microcontrollers, and each needs its own firmware:

```text
        ┌──────────────────────── Sonoff RF Bridge R2 ────────────────────────┐
        │                                                                     │
 Wi-Fi ─┤  ESP8285  ──── UART @ 19200 ────  EFM8BB1  ──── 433.92 MHz radio ───┤─📡
        │  (this ESPHome package)           (Portisch firmware)               │
        └─────────────────────────────────────────────────────────────────────┘
```

- The **EFM8BB1** is the RF coprocessor. Its **stock** firmware only understands a handful of
  fixed 24-bit protocols and cannot transmit the raw bucket-encoded frames AOK/Zemismart motors
  use. [**Portisch firmware**](https://github.com/Portisch/RF-Bridge-EFM8BB1) replaces it and adds
  raw `B0` transmit and `B1` capture. **This is mandatory** — without it the blinds cannot be
  controlled at all.
- The **ESP8285** is the Wi-Fi side. It runs **this package**, which turns the MQTT topic
  contract into UART commands for the coprocessor.

Flash the coprocessor first (§3), then the ESP (§4).

## 3. Step 1 — Portisch on the EFM8BB1 (one time, per bridge)

There is no ESPHome-based flasher for the EFM8BB1. The practical path is to **use Tasmota as a
one-time flashing tool**, then replace it with ESPHome in §4. (If you own an external Silicon Labs
C2 programmer you can flash the coprocessor directly and skip straight to §4.)

Community-maintained walkthroughs with photographs — worth having open alongside this page:
[Tasmota's device page](https://tasmota.github.io/docs/devices/Sonoff-RF-Bridge-433/) and
[nerdiy.de's illustrated guide](https://nerdiy.de/en/tasmota-sonoff-rf-bridge-rf-chipefm8bb1-with-portic-firmware-flashing-2/).

### 3.1 Solder the two programming wires

The ESP must reach the coprocessor's debug pins to program it. Solder two short wires:

| Board revision | Wiring |
|---|---|
| **R2** | `GPIO4 → C2D`, `GPIO5 → C2CK` |
| **R1** | `GPIO4 → C2CK`, `GPIO5 → C2D` |

> **The R2 silkscreen labels are reversed** relative to R1 — follow the table, not the print.
> Getting these backwards means the flash simply fails; it does not damage the board.

Leave the wires in place afterwards. They are inert during normal operation and let you reflash
the coprocessor later.

### 3.2 Flash Tasmota to the ESP8285 (temporarily)

1. Find the **5-pin serial header** next to the power switch. Slide the switch **toward the
   header** to put the ESP in programming mode.
2. Connect a 3.3 V USB-to-serial adapter: `GND→GND`, `3V3→3V3`, `TX→RX`, `RX→TX`.
   **Never feed it 5 V.**
3. Hold the board's push button while applying power to enter the bootloader, then flash a
   Tasmota build **that includes the `RF_FLASH` feature** (the plain minimal builds omit it —
   check [Tasmota's build table](https://tasmota.github.io/docs/devices/Sonoff-RF-Bridge-433/)).
4. Slide the switch back **away from** the header, power up, and join it to your Wi-Fi.
5. In Tasmota: **Configuration → Configure Module → Sonoff Bridge (25)**, and set **GPIO4 and
   GPIO5 to `None`** so the flasher can drive them.

### 3.3 Upload the Portisch firmware

1. Get the firmware `.hex`. Either source works:
   - **Portisch releases** (newest): the
     [releases page](https://github.com/Portisch/RF-Bridge-EFM8BB1/releases) ships
     `RF-Bridge-EFM8BB1.zip` — unzip it and use the `.hex` inside.
   - **Tasmota's bundled copies**: [`tools/fw_SonoffRfBridge_efm8bb1/`](https://github.com/arendst/Tasmota/tree/development/tools/fw_SonoffRfBridge_efm8bb1)
     in the Tasmota repository, newest first (e.g. `RF-Bridge-EFM8BB1-20190220.hex`). The same
     directory keeps `RF_Bridge_iTead_Original.hex`, which restores the stock RF firmware if you
     ever want to undo this.

   Download the **raw** file — use the "Download raw file" button, `curl`, or `wget`. **Saving
   GitHub's HTML preview page instead of the raw `.hex` is the single most common cause of a
   failed flash.**
2. Slide the ON/OFF switch to **OFF** — this isolates the RF chip for programming — and power the
   board from the **3.3 V and GND pins** rather than USB. (Powering from USB during this step is
   only safe on boards modified per Tasmota's hardware-mod notes.)
3. In Tasmota's web UI: **Firmware Upgrade → Upgrade by file upload**, choose the `.hex`, and
   **Start upgrade**. It completes in well under a minute and the device reboots.
4. Slide the switch back to **ON**.

### 3.4 Verify before moving on

In the Tasmota console, run:

```text
RfRaw AAB155
```

A Portisch-flashed coprocessor answers:

```json
{"RfRaw":{"Data":"AAA055"}}
```

If you get that, the hard part is done. If not, re-check the jumper wiring (§3.1), that GPIO4/5
are set to `None`, and that you uploaded a real `.hex` rather than an HTML page.

## 4. Step 2 — this package on the ESP8285

Now replace Tasmota with ESPHome.

### Why ESPHome rather than staying on Tasmota

Tasmota is excellent, and for generic 433 MHz remotes it is a perfectly good permanent home for
this hardware. For **this** project it is a stepping stone, for two reasons:

- **The integration speaks this package's contract.** The
  [zemismart-blinds][zemismart-blinds] Home Assistant integration drives the
  `rf433/<bridge>/…` topics documented in [README.md](README.md#mqtt-topic-contract) —
  correlated `accepted`/`started` acknowledgements, bridge-side fail-safe STOP deadlines,
  airtime-paced dispatch, and idle-listen `/rx`. Tasmota's `RfRaw` topics carry none of that, so
  a Tasmota bridge is **not** a supported backend for the integration.
- **ESPHome is the Home-Assistant-native path.** Configuration lives in your ESPHome dashboard
  alongside your other devices, OTA updates are one click, and the device's diagnostics appear
  where you already look for them.

| | Tasmota | This ESPHome package |
|---|---|---|
| Flash Portisch to the coprocessor | ✅ built-in flasher | ❌ not supported |
| Drives the `zemismart-blinds` integration | ❌ | ✅ |
| Fail-safe STOP deadlines held on-device | ❌ | ✅ |
| Correlated command acknowledgements | ❌ | ✅ |
| Managed from the HA ESPHome dashboard | ❌ | ✅ |

### Flashing

Follow [README.md → Install](README.md#install) for the file layout, `secrets.yaml`, and
substitutions, then:

```shell
uvx --from "esphome==2026.6.5" esphome run living-room.yaml
```

Because Tasmota is already on the ESP, this **first** ESPHome flash must go over **serial** again
(switch toward the header, hold the button, same wiring as §3.2). Every later update is OTA over
Wi-Fi.

> GPIO1/GPIO3 belong to the RF coprocessor's UART, so this package disables serial logging. Use
> the ESPHome dashboard's Wi-Fi log stream instead.

## 5. Verify the finished bridge

1. The retained topic `rf433/<bridge_id>/availability` reads `online` on your broker.
2. `rf433/<bridge_id>/info` carries the bridge's id, area, and `"v":2`.
3. Send a test frame — see [README.md → MQTT topic contract](README.md#mqtt-topic-contract) — and
   watch `rf433/<bridge_id>/status` report `accepted` then `started`.

Then continue with the [zemismart-blinds installation guide][zemismart-install] to add your
blinds to Home Assistant.

## Troubleshooting

**`RfRaw AAB155` returns nothing or an error.** The coprocessor is still on stock firmware. Recheck
§3.1–§3.3; the usual culprits are reversed jumper wires and a downloaded HTML page instead of the
`.hex`.

**Tasmota's firmware upload reports "Magic byte is not 0xE9".** You uploaded the `.hex` to the
wrong uploader or grabbed the wrong file — that error means Tasmota tried to read it as an ESP
image.

**The flash fails on a brand-new board.** Check the coprocessor marking (§1). An `OB38S003` will
never accept Portisch through this path.

**The bridge is online but blinds do not move.** That is past this guide — see the integration's
[troubleshooting section][zemismart-troubleshooting].

[zemismart-blinds]: https://github.com/joyfulhouse/zemismart-blinds
[zemismart-install]: https://github.com/joyfulhouse/zemismart-blinds/blob/main/INSTALL.md
[zemismart-troubleshooting]: https://github.com/joyfulhouse/zemismart-blinds#troubleshooting
