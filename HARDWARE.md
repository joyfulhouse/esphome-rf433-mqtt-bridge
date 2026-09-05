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

**Both current revisions of the Sonoff RF Bridge R2 can run this project — but by different
routes, and one of them is markedly harder.** Which chip you have decides which route you take, so
find out before you buy.

| | |
|---|---|
| ✅ **Works — the validated path** | R2 **V1.0 / V2.0** — Silicon Labs **EFM8BB1** radio chip. Portisch, flashed through Tasmota, no extra hardware. **[Step 1](#step-1--flash-portisch-to-the-radio-chip).** |
| ⚠️ **Works — alternate path, harder and not validated as a drop-in** | R2 **V2.2** (2022 and later) — **OB38S003** radio chip. A community Portisch port, flashed with an external programmer. **[Alternate path](#alternate-path--r2-v22-with-the-ob38s003-radio).** |

> ⚠️ **Sonoff changed the radio chip in 2022 without renaming the product.**
>
> Boards up to R2 V2.0 use the **EFM8BB1**, which runs the Portisch firmware this project was
> built against. The **R2 V2.2 replaced it with an OB38S003**, which *cannot* run upstream
> Portisch. It is not a dead end — [mightymos/RF-Bridge-OB38S003][mightymos] ports Portisch to
> that chip, keeps the same serial command set, and does transmit `B0` buckets — but it is a
> different job with real caveats:
>
> - **You need a second microcontroller** (a Wemos D1 mini, NodeMCU, ESP32, or Arduino Mega) as an
>   external programmer. There is no Tasmota-based flasher for this chip.
> - **The first flash is one-way.** Stock OB38S003 firmware is read-protected, and the erase that
>   unprotects the chip destroys it. No stock image is published anywhere. **You cannot go back.**
> - **Your existing B0 codes may not replay.** Transmitted bucket timings run long on this
>   firmware, so codes captured on an EFM8BB1 bridge often need re-capturing and re-tuning on the
>   V2.2 board itself.
> - **It can freeze after 24–48 h**, and upstream is unmaintained.
>
> **This project is developed and tested on EFM8BB1 boards.** New stock from any seller —
> including the official link below — may ship either revision, and the listing almost never says
> which.
>
> **If you want the path of least resistance, buy secondhand or old-stock R2 V1.0/V2.0** and check
> the listing photos for the chip marking before committing. If you already own a V2.2, or you are
> comfortable with the trade above, the alternate path is fully documented below.

**Where to buy:** [itead.cc — Sonoff RF Bridge 433](https://itead.cc/product/sonoff-rf-bridge-433/)
(the 433 MHz variant, **not** the 315 MHz one) and the usual marketplaces.

<details>
<summary><b>How to read the chip on a board you already have</b></summary>

Open the case — the screws sit under the rubber feet — and read the chip marking next to the RF
section:

- **`EFM8BB1`** → the validated path. Continue to [Step 1](#step-1--flash-portisch-to-the-radio-chip).
- **`OB38S003`** → the alternate path. Read
  [Alternate path — R2 V2.2](#alternate-path--r2-v22-with-the-ob38s003-radio) end to end *before*
  you touch the board; the first flash cannot be undone.
- The board silkscreen also prints the revision, e.g. `RF-Bridge-R2 V2.0`.

For what it's worth: seven R2 V1.0/V2.0 boards run this firmware in the author's house.

</details>

---

## Why two firmwares?

This is the thing that trips everyone up, so here it is once, clearly. The bridge contains **two
separate microcontrollers**. They are not the same chip and they do not run the same firmware.

```text
        ┌──────────────────────── Sonoff RF Bridge R2 ────────────────────────┐
        │                                     EFM8BB1 (V1.0/V2.0)             │
 Wi-Fi ─┤  ESP8285  ──── UART @ 19200 ────  or OB38S003 (V2.2)  ─── 433.92 ───┤─📡
        │  (this ESPHome package)             (Portisch firmware)             │
        └─────────────────────────────────────────────────────────────────────┘
```

| Chip | Its job | Firmware it needs | You flash it in |
|---|---|---|---|
| **EFM8BB1** or **OB38S003** | The 433 MHz radio | **Portisch** (or, on OB38S003, the [mightymos port][mightymos]) | Step 1 — or the [alternate path](#alternate-path--r2-v22-with-the-ob38s003-radio) |
| **ESP8285** | Wi-Fi + the brains | **This ESPHome package** | Step 2 |

- The **radio chip** is an EFM8BB1 on V1.0/V2.0 boards and an OB38S003 on V2.2 boards. Either
  way, its **stock** firmware only understands a handful of fixed 24-bit protocols and **cannot
  transmit the raw bucket-encoded frames AOK/Zemismart motors use.**
  [Portisch firmware](https://github.com/Portisch/RF-Bridge-EFM8BB1) replaces it and adds raw `B0`
  transmit and `B1` capture. **This is mandatory — without it the blinds cannot be controlled at
  all.** Both chips end up speaking the same Portisch serial protocol, which is why the ESP8285
  side is identical on both revisions.
- The **ESP8285** is the Wi-Fi side. It runs **this package**, which turns the MQTT commands from
  Home Assistant into UART instructions for the radio.

**Flash the radio chip first (Step 1), then the Wi-Fi chip (Step 2).** The rest of this guide is
those two steps.

---

## Step 1 — Flash Portisch to the radio chip

*One time, per bridge. This is the part with the soldering.*

> 📻 **OB38S003 board (R2 V2.2)?** This step does not apply — the chip has no C2 debug interface
> and cannot be flashed through Tasmota. Skip to the
> [alternate path](#alternate-path--r2-v22-with-the-ob38s003-radio), then rejoin at Step 2.

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

## Alternate path — R2 V2.2 with the OB38S003 radio

*Replaces [Step 1](#step-1--flash-portisch-to-the-radio-chip) on V2.2 boards. When it's done,
rejoin the guide at [Step 2](#step-2--flash-this-package-to-the-wi-fi-chip) — the ESP8285 side is
identical on both revisions.*

The OB38S003 has no Silicon Labs C2 debug interface, so Tasmota's RF flasher cannot touch it. It
is programmed over I²C by a **second microcontroller acting as an external programmer**, running
[mightymos/OnbrightFlasher][onbright]. The firmware it receives is
[mightymos/RF-Bridge-OB38S003][mightymos] — a port of Portisch to this chip that keeps the same
serial command set, so once flashed the ESP8285 side cannot tell the difference.

### Read this before you start

Three things about this path are materially different from the EFM8BB1 path. None of them is a
reason not to do it — they are reasons to do it with your eyes open.

> 🚫 **1. The first flash is one-way. There is no rollback.**
>
> Stock OB38S003 firmware ships **read-protected**, and the only way to clear that protection is
> the `erase` command — which destroys the stock image in the same operation. Upstream's own
> instructions say it plainly: *"this erases flash, cannot be recovered!"*
>
> This is the asymmetry with the EFM8BB1 path, where Tasmota bundles
> `RF_Bridge_iTead_Original.hex` and you can always put the board back to stock. **No equivalent
> stock image exists for the OB38S003** — not from Sonoff, not from Tasmota, not from upstream.
>
> **There is no read-back that gets you a backup**, and it is worth understanding why before you
> reach for one. The flasher's `readhex` sums all 8192 flash bytes and prints a single
> `Checksum: 0x…` line; `readconfigs` prints 64 config bytes and their checksum. Neither writes a
> `.hex` file, and neither reports read failures — both discard the return status of the underlying
> block read, so they print a checksum on a protected chip exactly as they do on an open one. That
> checksum is a record, not an image: **there is nothing to re-flash from, whatever they print.**
> Once you type `erase` the board is a V2.2-with-Portisch board permanently.

> ⏱️ **2. Your existing B0 codes probably will not replay. Budget for re-capturing them.**
>
> This firmware transmits bucket timings **longer than requested** — one reporter measured roughly
> **+30 µs** per bucket with a Flipper Zero, another **+76 µs** against a calibrated RTL-SDR
> ([issue #27][issue27], open). Receivers with tight timing windows reject the result, and a code
> captured on an EFM8BB1 Portisch bridge is exactly such a code.
>
> The cause is in the port, not in your codes. It dropped the startup-delay compensation upstream
> Portisch applies before each bucket, it performs a 16-bit division *after* asserting the RF edge
> (~38–40 µs of arithmetic charged to the pulse already on air), and its Timer-1 reload is off by
> one interval. The three add up to an **additive** per-bucket error — a fixed number of
> microseconds, *not* a percentage of the bucket's length — which is why a fixed correction is the
> right shape and a proportional one is not. Additive does not mean identical on every edge: the
> RTL-SDR measurement that came out at +76 µs overall resolved into roughly **+90 µs on pulses and
> +56 µs on gaps**. We do not patch the firmware: it is an unmaintained upstream binary and
> calibrating a fix needs measurement hardware this project does not have.
>
> **Re-capture every code on the V2.2 board itself, with the original remote in hand.** This is
> not optional cleanup you can defer — it is the step that makes transmit work. Upstream
> [issue #36][issue36] is the worked example: old EFM8BB1-captured codes did nothing until the
> reporter re-sniffed with this firmware and regenerated `B0` from the fresh `B1`, at which point
> transmission worked. Some users additionally hand-tune by subtracting the measured overshoot
> from each bucket value — that is an **alternative** to caveat 2a's `tx_bucket_offset_us`, never
> a companion to it.
>
> Keep the original remotes until every blind is confirmed working. Use this package's own
> onboarding capture (`{"action":"sniff","seconds":30}`, see
> [README.md → MQTT topic contract](README.md#mqtt-topic-contract)) once Step 2 is done.

> 🎛️ **2a. `tx_bucket_offset_us` — opt-in host-side correction, OFF by default.**
>
> If freshly captured codes *still* do not move a blind, the package can subtract a fixed number
> of microseconds from every bucket of every outbound `B0` frame, narrowing the overshoot above.
>
> **It cannot cancel the overshoot, and no host-side value can.** A bucket index is referenced as
> both a *pulse* and a *gap* inside the same frame, and each index carries exactly one 16-bit
> duration, so one number cannot hold two corrections. With the measured +90 µs on pulses and
> +56 µs on gaps, a single constant nulls only their **mean**: the best available setting still
> leaves roughly **±17 µs on every edge** — pulses **still long** by ~17 µs (90 − 73), gaps
> **short** by ~17 µs (56 − 73). That is a
> real improvement on an uncorrected +56 µs to +90 µs, and it is the whole of what this knob buys
> you. If a receiver rejects your frames at ±17 µs, the fix is not a different offset.
>
> Set it in your per-device YAML:
>
> ```yaml
> substitutions:
>   hardware_variant: ob38s003-mightymos
>   tx_bucket_offset_us: "73"   # OB38S003 / R2 V2.2 boards ONLY; see "Finding the value"
> ```
>
> **Default is `"0"`, which leaves every well-formed frame byte-identical** — an existing bridge
> that does not set it transmits exactly the bytes it always did, as long as every bucket its
> frames' data nibbles reference is already ≥ 100 µs. The exceptions are deliberate and apply at
> every offset including `"0"`: a frame carrying the `AAB0` magic that is *not* valid, even-length
> hex — or is too short to carry the header that magic implies — is **dropped rather than
> transmitted**, because the serializer would otherwise invent nibbles the author never wrote (see
> the malformed-frame note below); and a *referenced* bucket shorter than 100 µs is **floored to
> 100 µs** (see the floor note below), because both RF coprocessors turn it into a stuck carrier.
> **EFM8BB1 boards (R2 V1.0/V2.0) must leave it at
> `"0"`**: they run stock Portisch, which already compensates, so a non-zero value there breaks
> transmits that currently work.
>
> > ⚠️ **Do not double-compensate.** Caveat 2 above notes that some users hand-tune by subtracting
> > the measured overshoot from each bucket value. **If you did that, leave this at `"0"`.**
> > Applying both corrections subtracts the overshoot twice, the buckets land as far *short* of
> > the receiver's window as they were long, and **your blinds silently stop responding** — the
> > bridge keeps publishing `started` for every command, because `started` only proves the frame
> > reached the coprocessor over UART, never that it was emitted or received. Pick one correction:
> > untouched codes plus this knob, or hand-tuned codes plus `"0"`.
> >
> > Nothing in the frame distinguishes the two. A hand-tuned `B0` is structurally identical to an
> > untuned one — same header, same table shape, only smaller numbers — so neither the bridge nor
> > this knob can detect the mistake and warn you. The effective value is published on retained
> > `/info` as `tx_offset_us` and printed at boot as `TX bucket offset`; those are the only places
> > it is visible, and they tell you what the bridge is doing, not what your codes already assume.
>
> **Finding the value.** What is V-shaped is the **worst edge**, not every edge. Below 56 µs both
> roles are still long and above 90 µs both are short, but *between* them the two move in opposite
> directions at once — one role long while the other is already short — so no offset makes every
> edge better than the last. The quantity worth minimizing is therefore the larger of the two
> errors, `max(|90 − offset|, |56 − offset|)`, and that minimax lands on the **mean of the pulse
> and gap errors**, `(pulse_error + gap_error) / 2` — with the measured +90/+56 split, **73 µs**,
> where the ±17 µs residual above is what remains. Start from your board's mean if you have measured
> one, otherwise from 73, and search a **small range around it** rather than climbing toward the
> pulse-only figure of 90; silicon, supply, and temperature all move it, so treat it as per-board.
> The package accepts `0`–`120`, roughly 1.3× the highest number anyone has reported.
>
> **Three different numbers circulate for this error, and they are not alternatives to each
> other:** `+30 µs` is one reporter's Flipper Zero measurement; `+76 µs` is the original
> single-number RTL-SDR measurement of the same effect; `+90 µs`/`+56 µs` is that same RTL-SDR
> measurement later resolved per edge (pulse/gap), whose mean is `73 µs`. Do not read `30–90` as a
> range of equally likely values to sweep — it is the spread across two boards and two measurement
> methods.
>
> Buckets are floored at 100 µs, so neither compensation nor a hand-written frame can put a
> short *referenced* bucket on air. The hazard differs per coprocessor. On the OB38S003 (the
> vendored mightymos port) the Timer-1 ISR decrements its remaining-interval counter *before*
> testing it for zero, so a referenced bucket of 0–9 µs wraps to 65,535 intervals — about
> **659.5 ms of stuck carrier** per referenced occurrence, multiplied by the frame's embedded
> repeat. On the EFM8BB1 (stock Portisch) the pulse timeout is computed as `timeout - 65`, so a
> referenced bucket of 0–64 µs underflows to about **65.5 ms** of stuck line. The floor sits
> above both hazard ranges and far below the shortest real AOK bucket (280 µs).
>
> The floor is enforced twice, and both times only on buckets a data nibble actually
> **references**. `/tx` admission *rejects* a frame that references a sub-100 µs bucket
> (`"reason":"frame references a bucket shorter than 100 us"`). `rf_bridge.send_raw` — a public
> action that bypasses admission — instead *floors* each referenced sub-100 µs bucket to 100 µs
> at **every** offset, including the default `"0"`, and logs a warning naming the number of
> floored buckets: immediately, then at most once a minute, because the condition is
> deterministic per frame and would otherwise repeat on every repeat of every dispatch. When the
> floor engages, the frame goes out no longer encoding the timing it was written with — silent
> is the one thing that must not happen. At the default offset of `"0"`, unreferenced
> bucket-table entries are never rewritten, whatever their duration: they never key the
> transmitter and pass through byte-verbatim. At a non-zero offset the compensation pass rewrites
> the *whole* bucket table, referenced entries or not — see
> [#24](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/24). And on a real AOK
> capture (shortest bucket 280 µs) the compensation floor cannot engage below an offset of 181 µs
> — which is why the accepted range stops at 120.
>
> **Not every transmit is compensated.** This applies to the bucket table of `B0` frames sent
> through `rf_bridge.send_raw`, which is every transmit this package performs. The two stock
> transmit actions write to the UART without passing through it, and they are not in the same
> position as each other:
>
> - `rf_bridge.send_advanced_code` (`0xA8`) carries a protocol **ID**. The coprocessor generates
>   the edges from its own protocol table, so there are no host-supplied timings to correct.
> - `rf_bridge.send_code` (`0xA5`) is different: its `sync`, `low`, and `high` fields **are**
>   host-supplied microsecond timings, written straight from your YAML. They are **not** corrected
>   by `tx_bucket_offset_us`, and whether they suffer issue #27 the way bucket timings do has
>   **not been measured**. Treat A5 transmits on OB38S003 as unverified rather than as either
>   working or broken.
>
> **A malformed `B0` frame is dropped, not transmitted.** If a frame carries the `AAB0` magic but
> is not valid, even-length hex — or is too short to carry the header that magic implies — the
> bridge refuses it and logs a warning; nothing reaches the coprocessor. The serializer would
> otherwise turn an unparseable nibble into `0` and truncate an odd one, putting timings on air
> that the author never wrote. This check runs whatever `tx_bucket_offset_us` is set to, including
> `"0"`. Leading and trailing whitespace is trimmed first, so a frame read out of a text sensor
> keeps working with its newline attached; whitespace *inside* a frame is refused, because
> `AAB0 05…` gives no honest reading of where the nibbles begin.
>
> **What that check is, and is not.** The line it draws is **authorship**: the serializer must not
> invent nibbles. It is not a ban on short buckets. A frame whose bucket is a literal `0000` is
> valid hex, self-consistent, and exactly what its author wrote, so it is accepted by this check —
> but it no longer reaches the coprocessor as a `0`: if a data nibble *references* that bucket,
> `/tx` admission rejects it, and `send_raw` floors it to 100 µs at every offset including `"0"`.
> The stuck-carrier residual that earlier revisions of this note documented is closed.
>
> The check stays narrow on purpose. A declared bucket need never be *referenced* by a data nibble
> (the scheduler rejects references *above* `bucket_count`, not unused entries below it), so an
> over-sized, zero-filled bucket table is legal, is admitted, and passes through verbatim — an
> unreferenced entry never reaches the air on either coprocessor variant. Refusing or rewriting
> every frame containing a zero bucket would break those legitimate tables.
>
> This is **compile-time**, not runtime-settable: changing it needs `esphome run` (recompile plus
> OTA), so budget a flash per trial value. It applies only to the bucket table — data nibbles,
> lengths, and trailers are untouched — and it is applied at the UART boundary, *after* the
> scheduler has computed its RF pacing from the uncompensated durations, so airtime accounting
> deliberately stays conservative.

> 🧊 **3. Receive can stall after 24–48 h, and upstream is unmaintained.**
>
> [Issue #19][issue19] (open) reports that after a day or two of uptime the radio MCU **stops
> receiving** — it no longer decodes inbound codes. **Transmit keeps working, and so do the
> ESP8285 and Wi-Fi**: *"For receive only. Transmit keeps working fine!"* Restarting the ESP does
> not clear it; only the radio MCU itself has to be reset. There is no firmware fix and there will
> not be one — the v0.4.16 notes say *"This will likely be the last release… I no longer own this
> hardware."*
>
> Mitigate it operationally, with a **prophylactic reset on a timer** rather than a reaction to a
> fault signal: send the MCU reset `AA FE 55` (the workaround named in issue #19, schedulable from
> Home Assistant or ESPHome) on a schedule, or power-cycle the board from a smart plug.
>
> ⚠️ **Do not alarm on `rf433/<bridge_id>/availability` for this.** The ESP stays connected through
> an RX-only stall, so availability remains `online` and never goes stale — it cannot detect this
> failure. If inbound codes matter to you, alarm instead on the *absence of expected receive
> activity*: watch `rf433/<bridge_id>/rx` and alert when a sensor that normally reports on a known
> cadence goes quiet.

### What you need

| | |
|---|---|
| ☐ **An external programmer board** | Wemos D1 mini, NodeMCU (ESP8266), an ESP32, or an Arduino Mega 2560. Confirmed working by upstream. On the Mega, level-shift to 3.3 V. |
| ☐ **The Arduino IDE** | To build and upload the flasher sketch. |
| ☐ **Four jumper wires** | SDA, SCL, GND, 3V3 — to the RF chip's `J3` header. No soldering on this path. |
| ☐ **The pinned firmware from this repo** | [`firmware/ob38s003-mightymos/`](firmware/ob38s003-mightymos/) |

### A1. Use the pinned firmware image — don't re-download

This repo vendors the exact image to flash, with its hash:

```text
firmware/ob38s003-mightymos/
├── portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex
├── SHA256SUMS
├── LICENSE
└── README.md
```

Verify it before you flash (the checksum file lists a bare filename, so run the check from
inside the firmware directory):

```shell
(cd firmware/ob38s003-mightymos && shasum -a 256 -c SHA256SUMS)
```

**Use this file, not whatever the releases page offers today.** Three reasons, and the first is
the one that bites:

- **The variant matters and the names are easy to confuse.** The release ships both
  `..._BUCKET_SNIFFING_INCLUDED.hex` and `..._MULTIPLE_PROTOCOLS_INCLUDED.hex`. Only the
  bucket-sniffing build has the **112-byte** RF buffer; the other has **64 bytes**. This project's
  AOK/Zemismart `B0` frames run to about **82 bytes** — on the 64-byte build they are silently
  truncated and transmitted wrong rather than rejected. The failure looks like "the blind just
  doesn't respond", with nothing in any log.
- **Bucket timings are per-build.** Since you must re-tune timings against the firmware anyway
  (caveat 2 above), every bridge in a fleet needs to be running the *same* build for one set of
  captured codes to work across all of them.
- **Upstream is unmaintained**, so a pinned copy removes the dependency on a release page staying
  up — and removes the classic "saved GitHub's HTML preview instead of the raw `.hex`" flash
  failure entirely.

[`firmware/ob38s003-mightymos/README.md`](firmware/ob38s003-mightymos/README.md) records the full
provenance and the reasoning in detail.

✅ **Done when** `shasum -a 256 -c` prints `OK`.

### A2. Erase the ESP8285 first

Recommended, for two reasons: it drops the board's power draw during MCU flashing, and it stops
the ESP from driving the shared lines while you program the radio.

1. Wire your 3.3 V USB-to-serial adapter to the ESP header (`J2`): `3V3→3V3`, `TX→RX`,
   `RX→TX`, `GND→GND`. **Never feed it 5 V.**
2. Hold the board's push button, *then* plug the adapter into your computer, and release.
3. Erase it:

   ```shell
   uvx --from esptool esptool.py --chip auto --port /dev/cu.usbserial-XXXX erase_flash
   ```

Coming from a board that already runs ESPHome? Some users have skipped this successfully. Coming
from stock firmware, do it.

✅ **Done when** esptool reports `Chip erase completed successfully`.

### A3. Build the external programmer

1. Clone [mightymos/OnbrightFlasher][onbright].
2. Open `OnbrightFlasher.ino` in the Arduino IDE, and upload it to your programmer board.
3. Open the serial monitor at **115200 baud** with line ending set to **Both NL & CR**.

> ⚠️ **ESP32 users:** several have had to downgrade the `arduino-esp32` core for I²C to work
> ([espressif/arduino-esp32#11374](https://github.com/espressif/arduino-esp32/issues/11374)). If
> the handshake never acknowledges on an ESP32, try an ESP8266 board before assuming the target is
> bad.

✅ **Done when** the sketch's prompt responds to `?` in the serial monitor.

### A4. Wire the programmer to the radio chip

With **everything unpowered**, connect the RF chip's `J3` header to your programmer:

| RF chip `J3` (OB38S003) | Programmer (Wemos D1 / NodeMCU / ESP32) |
|---|---|
| `SCL` | `SCL` |
| `SDA` | `SDA` |
| `GND` | `GND` |
| `3V3` | `3V3` — **leave this one disconnected for now** |

> ⚠️ **Stay off the 5 V rail.** Do not connect anything to your programmer board's 5 V pin.

The 3V3 wire stays off because **handshaking happens at target power-up**. The flasher has to be
listening *before* the radio chip gets power. (You can also leave 3V3 disconnected permanently and
power the whole bridge from its micro-USB port at the prompt instead — upstream reports success
either way, and it avoids brown-outs from a weak on-board regulator.)

✅ **Done when** SDA, SCL and GND are connected and 3V3 is loose.

### A5. Record what you can, then erase

This is the point of no return — and there is no backup to take first. See caveat 1.

1. In the serial monitor, type `handshake`.
2. Apply power to the radio chip (plug the 3V3 wire in, or power the bridge by USB).
3. You should see `Handshake succeeded` and `Chip read: 0xA`.

   > On fresh boards the handshake often succeeds while reporting the **wrong chip type**, or the
   > chip read appears to fail with a NACK. Both are expected on a **protected** chip. Continue.

4. Optionally run `readhex`, then `readconfigs`, and save what they print. Both will print a
   `Checksum: 0x…` line whether or not the chip is protected — that output is a record for your own
   notes, **not** a backup, and it is not a protection check. There is no stock image to preserve
   either way (caveat 1).
5. Type `erase`. This unprotects the chip **and destroys the stock firmware permanently.**
6. Type `signature` — it should now report the chip type correctly.

✅ **Done when** `erase` reports `Chip erase successful` and `signature` reads correctly.

### A6. Flash the firmware with `flashScript.py`

Manual mode means pasting ~520 hex lines into a serial monitor by hand. Use the script.

1. Copy the pinned `.hex` into the `OnbrightFlasher` checkout — the script offers you every
   `.hex`/`.ihx` in its own directory, so make sure the one you want is the obvious choice there:

   ```shell
   cp firmware/ob38s003-mightymos/portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex \
      /path/to/OnbrightFlasher/
   ```
2. Remove power from the radio chip again (pull the 3V3 wire / unplug the bridge), so the next
   handshake can catch it at power-up.
3. Close the Arduino serial monitor — it holds the port the script needs.
4. Run the script with `pyserial` supplied by `uv`:

   ```shell
   cd /path/to/OnbrightFlasher
   uv run --with pyserial python flashScript.py
   ```

   > The script will otherwise try to `pip install pyserial` into whatever interpreter it finds.
   > Running it under `uv run --with pyserial` makes the import succeed immediately, so that path
   > never fires.
5. Pick your serial port by number, then pick
   `portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex` from the file list.
6. **Apply power to the radio chip when the script logs `cycle power to target`.**

The script then drives the whole sequence itself: `handshake` → `erase` → `setfuse 18 249`
(reconfigures the reset pin as reset rather than GPIO) → write every hex line → `mcureset`. Each
line prints `Write successful`. It finishes in well under a minute.

Disconnect all four wires from both boards when it's done.

✅ **Done when** the script logs `MCU reset...` with no failed writes.

### A7. Verify the radio chip

Flash the ESP8285 first — the radio chip has no other route to the outside world. Do
[Step 2](#step-2--flash-this-package-to-the-wi-fi-chip) now, then come back here.

With this package running, the bridge and the radio speak Portisch over the same UART, so the
standard checks apply. If you flashed Tasmota instead (as a diagnostic aid), set the module to
**Sonoff Bridge (25)** — not `Generic (0)`, which is for passthrough builds only — and run:

```text
RfRaw AAB155
```

A correctly flashed radio answers:

```json
{"RfRaw":{"Data":"AAA055"}}
```

**Useful command: `AA FE 55` resets the radio MCU.** That is Portisch's `RF_RESET_MCU` opcode
(`0xFE`) between the standard start (`0xAA`) and stop (`0x55`) bytes. Sending it restarts the
radio coprocessor without power-cycling the ESP8285 — the first thing to try if the radio stops
responding, and a useful building block for automating around the [24–48 h freeze][issue19].

✅ **The alternate path is done when** the radio answers `AAA055` and this package is running on
the ESP8285.

Then work through your codes with caveat 2 in mind: **re-capture each one on this board**, confirm
each blind responds, and only then retire the original remotes.

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
   uvx --from "esphome==2026.7.3" esphome run living-room.yaml
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
accept Portisch through this path — it has no C2 debug interface for Tasmota's flasher to drive.
That board is not a dead end, but it takes the
[alternate path](#alternate-path--r2-v22-with-the-ob38s003-radio) and an external programmer.

</details>

<details>
<summary><b>The bridge is online but blinds don't move</b></summary>

That's past this guide — see the integration's [troubleshooting section][zemismart-troubleshooting].

</details>

[mightymos]: https://github.com/mightymos/RF-Bridge-OB38S003
[onbright]: https://github.com/mightymos/OnbrightFlasher
[issue19]: https://github.com/mightymos/RF-Bridge-OB38S003/issues/19
[issue27]: https://github.com/mightymos/RF-Bridge-OB38S003/issues/27
[issue36]: https://github.com/mightymos/RF-Bridge-OB38S003/issues/36
[zemismart-blinds]: https://github.com/joyfulhouse/zemismart-blinds
[zemismart-install]: https://github.com/joyfulhouse/zemismart-blinds/blob/main/INSTALL.md
[zemismart-troubleshooting]: https://github.com/joyfulhouse/zemismart-blinds#troubleshooting
