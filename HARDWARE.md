# Build your bridge

This is the hardest part of the whole project, so the guide is deliberately slow and complete.
You are going to take a stock Sonoff RF Bridge and flash **two** separate chips inside it. Do the
steps in order and check the ✅ after each one, and you'll get there.

**Time:** about an hour for your first bridge.
**Difficulty:** you will solder two short wires once. Everything else is software.

---

## Before you start

Get these on the bench first. Nothing here is exotic — but stopping halfway to order a part is the
main way this step goes wrong.

| | |
|---|---|
| ☐ **A Sonoff RF Bridge R2** | 433 MHz variant. **The revision matters — [read this first](#will-my-board-work).** |
| ☐ **A USB-to-serial adapter** | Must be **3.3 V**. Used for the first flash of each chip. |
| ☐ **Two short jumper wires + a soldering iron** | For two solder joints on the board. One-time. |
| ☐ **A computer** | To run the flashing tools and, later, ESPHome. |

> ⚠️ **You will solder two wires.** They're two small joints, not fine-pitch work, and getting
> them wrong only makes the flash fail — it does not damage the board. But there is no way to skip
> them unless you own a Silicon Labs C2 programmer.

### What you're about to do

The bridge has two chips, and **each needs its own firmware**:

1. **Step 1 — the radio chip.** Flash it with *Portisch* firmware, using *Tasmota* as a temporary
   flashing tool. This is the part with the soldering. You do it once per bridge.
2. **Step 2 — the Wi-Fi chip.** Replace Tasmota with *this ESPHome package*. This is the firmware
   that actually talks to Home Assistant.

If those two sentences sound confusing, that's normal — [the two-firmware map](#why-two-firmwares)
explains exactly which chip is which. Read it before you start; it's the single most confusing
thing about this hardware.

---

## Will my board work?

**Only some revisions of the Sonoff RF Bridge R2 can run this firmware.** Buy the wrong one and
there is nothing you can do in software to fix it.

| | |
|---|---|
| ✅ **Works** | R2 **V1.0 / V2.0** — has a Silicon Labs **EFM8BB1** radio chip |
| ❌ **Does not work** | R2 **V2.2** (2022 and later) — has an **OB38S003** radio chip |

> ⚠️ **Sonoff changed the radio chip in 2022 without renaming the product.**
>
> Boards up to R2 V2.0 use the **EFM8BB1**, which runs the Portisch firmware this project depends
> on. The **R2 V2.2 replaced it with an OB38S003**, which *cannot* run Portisch and is unsupported
> here. New stock from any seller — including the official link below — may ship either revision,
> and the listing almost never says which. **This project has only ever been tested on the older
> EFM8BB1 boards.**
>
> Treat a fresh purchase as a gamble unless the seller confirms the chip or you can return it.
> **Secondhand or old-stock R2 V1.0/V2.0 units are the safe buy** — check the listing photos for
> the chip marking before committing.

**Where to buy:** [itead.cc — Sonoff RF Bridge 433](https://itead.cc/product/sonoff-rf-bridge-433/)
(the 433 MHz variant, **not** the 315 MHz one) and the usual marketplaces.

<details>
<summary><b>How to read the chip on a board you already have</b></summary>

Open the case — the screws sit under the rubber feet — and read the chip marking next to the RF
section:

- **`EFM8BB1`** → supported. This is what you want.
- **`OB38S003`** → **not supported by this project.** Portisch does have separate OB38S003
  firmware, but it needs an external programmer and is not part of this package's validated path.
- The board silkscreen also prints the revision, e.g. `RF-Bridge-R2 V2.0`.

For what it's worth: seven R2 V1.0/V2.0 boards run this firmware in the author's house.

</details>

---

## Why two firmwares?

This is the thing that trips everyone up, so here it is once, clearly. The bridge contains **two
separate microcontrollers**. They are not the same chip and they do not run the same firmware.

```text
        ┌──────────────────────── Sonoff RF Bridge R2 ────────────────────────┐
        │                                                                     │
 Wi-Fi ─┤  ESP8285  ──── UART @ 19200 ────  EFM8BB1  ──── 433.92 MHz radio ───┤─📡
        │  (this ESPHome package)           (Portisch firmware)               │
        └─────────────────────────────────────────────────────────────────────┘
```

| Chip | Its job | Firmware it needs | You flash it in |
|---|---|---|---|
| **EFM8BB1** | The 433 MHz radio | **Portisch** | Step 1 |
| **ESP8285** | Wi-Fi + the brains | **This ESPHome package** | Step 2 |

- The **EFM8BB1** is the radio. Its **stock** firmware only understands a handful of fixed 24-bit
  protocols and **cannot transmit the raw bucket-encoded frames AOK/Zemismart motors use.**
  [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1) replaces it and adds raw `B0`
  transmit and `B1` capture. **This is mandatory — without it the blinds cannot be controlled at
  all.**
- The **ESP8285** is the Wi-Fi side. It runs **this package**, which turns the MQTT commands from
  Home Assistant into UART instructions for the radio.

**Flash the radio chip first (Step 1), then the Wi-Fi chip (Step 2).** The rest of this guide is
those two steps.

---

## Step 1 — Flash Portisch to the radio chip

*One time, per bridge. This is the part with the soldering.*

There is no ESPHome-based flasher for the EFM8BB1, so the practical path is to **use Tasmota as a
one-time flashing tool**, then replace it with ESPHome in Step 2.

> If you own an external Silicon Labs C2 programmer, you can flash the coprocessor directly and
> skip straight to [Step 2](#step-2--flash-this-package-to-the-wi-fi-chip).

<details>
<summary><b>Helpful illustrated walkthroughs to keep open alongside this page</b></summary>

Both have photographs of every step:

- [Tasmota's Sonoff RF Bridge device page](https://tasmota.github.io/docs/devices/Sonoff-RF-Bridge-433/)
- [nerdiy.de's illustrated Portisch flashing guide](https://nerdiy.de/en/tasmota-sonoff-rf-bridge-rf-chipefm8bb1-with-portic-firmware-flashing-2/)

</details>

### 1a. Solder the two programming wires

The ESP has to reach the radio chip's debug pins to program it. Solder two short wires according to
your board revision:

| Board revision | Wiring |
|---|---|
| **R2** | `GPIO4 → C2D`, `GPIO5 → C2CK` |
| **R1** | `GPIO4 → C2CK`, `GPIO5 → C2D` |

> ⚠️ **The R2 silkscreen labels are reversed** relative to R1 — follow the table, not the printing
> on the board. Getting these backwards only makes the flash fail; it does not damage anything.

Leave the wires in place afterwards. They are inert during normal operation and let you reflash the
radio later.

✅ **Done when** two wires join GPIO4 and GPIO5 to the radio's C2D/C2CK pins per the table.

### 1b. Flash Tasmota to the Wi-Fi chip (temporarily)

1. Find the **5-pin serial header** next to the power switch. Slide the switch **toward the header**
   to put the ESP into programming mode.
2. Connect your 3.3 V USB-to-serial adapter: `GND→GND`, `3V3→3V3`, `TX→RX`, `RX→TX`.
   **Never feed it 5 V.**
3. Hold the board's push button while applying power to enter the bootloader, then flash a Tasmota
   build **that includes the `RF_FLASH` feature.** (The plain minimal builds omit it — check
   [Tasmota's build table](https://tasmota.github.io/docs/devices/Sonoff-RF-Bridge-433/).)
4. Slide the switch back **away from** the header, power up, and join it to your Wi-Fi.
5. In Tasmota: **Configuration → Configure Module → Sonoff Bridge (25)**, and set **GPIO4 and GPIO5
   to `None`** so the flasher can drive them.

✅ **Done when** Tasmota's web UI loads and the module is set to *Sonoff Bridge (25)* with GPIO4/5
set to `None`.

### 1c. Upload the Portisch firmware

1. **Get the firmware `.hex`.** Either source works:
   - **Portisch releases** (newest): the
     [releases page](https://github.com/Portisch/RF-Bridge-EFM8BB1/releases) ships
     `RF-Bridge-EFM8BB1.zip` — unzip it and use the `.hex` inside.
   - **Tasmota's bundled copies**:
     [`tools/fw_SonoffRfBridge_efm8bb1/`](https://github.com/arendst/Tasmota/tree/development/tools/fw_SonoffRfBridge_efm8bb1)
     in the Tasmota repo, newest first (e.g. `RF-Bridge-EFM8BB1-20190220.hex`). The same directory
     keeps `RF_Bridge_iTead_Original.hex`, which restores the stock RF firmware if you ever want to
     undo this.

   > ⚠️ **Download the *raw* file** — use the "Download raw file" button, `curl`, or `wget`. Saving
   > GitHub's HTML preview page instead of the raw `.hex` is the **single most common cause of a
   > failed flash.**
2. Slide the ON/OFF switch to **OFF** — this isolates the RF chip for programming — and power the
   board from the **3.3 V and GND pins** rather than USB. (Powering from USB during this step is
   only safe on boards modified per Tasmota's hardware-mod notes.)
3. In Tasmota's web UI: **Firmware Upgrade → Upgrade by file upload**, choose the `.hex`, and
   **Start upgrade**. It finishes in well under a minute and the device reboots.
4. Slide the switch back to **ON**.

### 1d. Verify the radio chip

In the Tasmota console, run:

```text
RfRaw AAB155
```

A Portisch-flashed radio chip answers:

```json
{"RfRaw":{"Data":"AAA055"}}
```

✅ **Step 1 is done when you get that response.** The hard part is behind you.

If you don't get it, see [Troubleshooting](#troubleshooting) — the usual causes are reversed jumper
wires (1a), GPIO4/5 not set to `None` (1b), or an HTML page saved instead of a real `.hex` (1c).

---

## Step 2 — Flash this package to the Wi-Fi chip

*Now replace Tasmota with ESPHome. This is the firmware that drives Home Assistant.*

Tasmota did its job in Step 1. But **a bridge left running Tasmota cannot drive this integration** —
here's why you swap it out.

<details>
<summary><b>Why ESPHome instead of keeping Tasmota</b></summary>

Tasmota is excellent, and for generic 433 MHz remotes it is a perfectly good permanent home for
this hardware. For **this** project it is only a stepping stone, for two reasons:

- **The integration speaks this package's contract.** The
  [zemismart-blinds][zemismart-blinds] Home Assistant integration drives the `rf433/<bridge>/…`
  topics documented in [README.md](README.md#mqtt-topic-contract) — correlated
  `accepted`/`started` acknowledgements, bridge-side fail-safe STOP deadlines, airtime-paced
  dispatch, and idle-listen `/rx`. Tasmota's `RfRaw` topics carry none of that, so **a Tasmota
  bridge is not a supported backend for the integration.**
- **ESPHome is the Home-Assistant-native path.** Configuration lives in your ESPHome dashboard
  alongside your other devices, OTA updates are one click, and the device's diagnostics appear
  where you already look for them.

| | Tasmota | This ESPHome package |
|---|---|---|
| Flash Portisch to the radio chip | ✅ built-in flasher | ❌ not supported |
| Drives the `zemismart-blinds` integration | ❌ | ✅ |
| Fail-safe STOP deadlines held on-device | ❌ | ✅ |
| Correlated command acknowledgements | ❌ | ✅ |
| Managed from the HA ESPHome dashboard | ❌ | ✅ |

</details>

1. **Set up the config files.** Follow [README.md → Install](README.md#install) for the file
   layout, `secrets.yaml`, and substitutions.
2. **Flash over serial.** Because Tasmota is still on the ESP, this **first** ESPHome flash must go
   over **serial again** — same wiring and button-hold as [step 1b](#1b-flash-tasmota-to-the-wi-fi-chip-temporarily)
   (switch toward the header, hold the button while powering on). Then run:

   ```shell
   uvx --from "esphome==2026.6.5" esphome run living-room.yaml
   ```
3. **Later updates are OTA.** Every update after this first one goes over Wi-Fi — no serial cable.

> GPIO1/GPIO3 belong to the radio chip's UART, so this package disables serial logging. Watch the
> ESPHome dashboard's Wi-Fi log stream instead.

✅ **Step 2 is done when** ESPHome reports a successful flash and the device connects to Wi-Fi.

---

## Step 3 — Confirm the bridge is alive

Check these on your MQTT broker (MQTT Explorer or `mosquitto_sub` both work):

1. The retained topic `rf433/<bridge_id>/availability` reads `online`.
2. `rf433/<bridge_id>/info` carries the bridge's id, area, and `"v":2`.
3. Send a test frame — see [README.md → MQTT topic contract](README.md#mqtt-topic-contract) — and
   watch `rf433/<bridge_id>/status` report `accepted`, then `started`.

✅ **Your bridge is finished when all three check out.**

Then continue with the [zemismart-blinds installation guide][zemismart-install] to add your blinds
to Home Assistant.

---

## Troubleshooting

<details>
<summary><b><code>RfRaw AAB155</code> returns nothing or an error</b></summary>

The radio chip is still on its stock firmware. Recheck [1a–1c](#step-1--flash-portisch-to-the-radio-chip);
the usual culprits are reversed jumper wires and a downloaded HTML page instead of the `.hex`.

</details>

<details>
<summary><b>Tasmota's upload reports "Magic byte is not 0xE9"</b></summary>

You uploaded the `.hex` to the wrong uploader, or grabbed the wrong file — that error means Tasmota
tried to read it as an ESP image.

</details>

<details>
<summary><b>The flash fails on a brand-new board</b></summary>

Check the radio chip marking ([Will my board work?](#will-my-board-work)). An `OB38S003` will never
accept Portisch through this path.

</details>

<details>
<summary><b>The bridge is online but blinds don't move</b></summary>

That's past this guide — see the integration's [troubleshooting section][zemismart-troubleshooting].

</details>

[zemismart-blinds]: https://github.com/joyfulhouse/zemismart-blinds
[zemismart-install]: https://github.com/joyfulhouse/zemismart-blinds/blob/main/INSTALL.md
[zemismart-troubleshooting]: https://github.com/joyfulhouse/zemismart-blinds#troubleshooting
