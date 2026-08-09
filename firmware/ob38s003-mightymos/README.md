# Vendored OB38S003 radio firmware (mightymos Portisch port)

This directory pins one prebuilt firmware image for the **OB38S003** radio coprocessor found in
the Sonoff RF Bridge **R2 V2.2**. It is the alternate path that makes a V2.2 board usable with
this project; the EFM8BB1 boards (R2 V1.0/V2.0) use upstream Portisch instead and do not need
anything from this directory.

Flashing instructions live in [HARDWARE.md → Alternate path: R2 V2.2 / OB38S003](../../HARDWARE.md#alternate-path--r2-v22-with-the-ob38s003-radio).
Read the caveats there before you erase anything — the first flash is one-way.

## Provenance

| | |
|---|---|
| **Upstream project** | [mightymos/RF-Bridge-OB38S003](https://github.com/mightymos/RF-Bridge-OB38S003) |
| **Release** | [v0.4.16](https://github.com/mightymos/RF-Bridge-OB38S003/releases/tag/v0.4.16) (published 2025-03-10, tag commit `bb8bb6e59a6feb198cd64cbd1e48a356e605dc94`) |
| **Asset** | `portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex` |
| **Download URL** | <https://github.com/mightymos/RF-Bridge-OB38S003/releases/download/v0.4.16/portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex> |
| **Size** | 22,384 bytes (Intel HEX; 8,048 bytes of code spanning `0x0000–0x1F8E`) |
| **SHA256** | `1648f9f5d1e5077dd71aed82a80035ad6aa4311d443caf0e65546d990dbd2f26` |
| **License** | BSD-2-Clause — full text in [LICENSE](LICENSE); © 2023 Jonathan Armstrong (upstream, vendored verbatim) |

Verify before flashing:

```shell
shasum -a 256 -c SHA256SUMS
```

## Why this exact asset

The v0.4.16 release ships 16 assets — four firmware flavours (`passthrough`, `rcswitch`,
`portisch`, and for `portisch` two RF-receive feature builds) across four target MCUs
(`EFM8BB1`, `EFM8BB1LCB`, `EFM8BB52`, `OB38S003`). Two axes narrow that to exactly one file, so
the naming is not ambiguous:

- **`_OB38S003`** — the radio MCU on an R2 V2.2 board. The `EFM8BB*` builds are for other
  silicon and will not run here.
- **`portisch_main_`** — only the `portisch` flavour implements the Portisch serial command set
  (`0xB0` bucket transmit, `0xB1` bucket capture) this package's `rf_bridge:` UART protocol
  speaks. `passthrough_main_` bit-bangs the radio pins straight through to the ESP and
  `rcswitch_main_` implements a different, incompatible command set.
- **`_BUCKET_SNIFFING_INCLUDED`** — **required for this project's frame sizes.** Upstream's
  `inc/portisch_rf_handling.h` sets `RF_DATA_BUFFERSIZE` to **112** bytes under
  `BUCKET_SNIFFING_INCLUDED` and to **64** bytes under `MULTIPLE_PROTOCOLS_INCLUDED`. The AOK /
  Zemismart B0 frames this bridge transmits run to roughly **82 bytes** of bucket payload, which
  fits the 112-byte buffer and overflows the 64-byte one. On overflow, upstream's UART receive
  state machine (`src/portisch_main.c`, `case RECEIVING`) clamps `packetLength` to the buffer
  size and proceeds to the stop-byte state — it truncates the frame and transmits it rather than
  rejecting it. The only signal is a `printf_tiny()` that non-logging builds compile out, so on
  the 64-byte build an oversized `B0` goes out **silently wrong**. The bucket-sniffing build
  also serves the `B1` capture path this package uses for onboarding, and it can replay `B0`
  codes too — there is no reason to prefer the other variant here.

**Without UART logging**, as required: upstream gates all diagnostic output behind
`UART_LOGGING_ENABLED` in `project-defs.h`, which is **commented out at v0.4.16**, so every
released asset — including this one — is built with logging off. No released asset carries a
UART-logging suffix; a logging build would have to be compiled by hand. That matters because the
OB38S003 has a single UART, the same one the ESP8285 reads Portisch frames on (upstream's own
comment scopes logging to "hardware with a second uart … e.g. EFM8BB52"). A logging build would
interleave `printf_tiny()` ASCII into the framed byte stream and corrupt it.

## Why vendor a binary at all

Upstream is **unmaintained**. The v0.4.16 release notes read, in full:

> This will likely be the last release of this firmware. It appears to (mostly) work for many
> people using various devices. I no longer own this hardware.

There will be no follow-up release to track, and the author no longer has the hardware to
validate one. Vendoring the exact image with its hash means:

- every bridge in a fleet gets a **bit-identical** radio firmware, which matters because bucket
  timings must be re-tuned per firmware build (see the re-capture caveat in HARDWARE.md);
- flashing does not depend on a third-party release page staying reachable, and a replaced or
  re-uploaded asset cannot silently change what gets flashed;
- the "download the raw file, not GitHub's HTML preview" failure mode — the most common cause of
  a failed radio flash — is removed entirely.

Rebuilding from source is not a practical fallback for most users: it needs an SDCC 8051
toolchain, and `.hex` is what the flasher consumes either way.

## Known upstream issues carried by this image

These are properties of the firmware, not of this repository. They are documented at length in
HARDWARE.md; summarised here so this directory is self-contained.

| Issue | Effect | Handling |
|---|---|---|
| [#27](https://github.com/mightymos/RF-Bridge-OB38S003/issues/27) (open) | Transmitted `B0` bucket timings run long on air — roughly **+30 µs** per bucket measured with a Flipper Zero, and **+76 µs** measured against a calibrated RTL-SDR (that same RTL-SDR measurement resolves per edge into **+90 µs on pulses, +56 µs on gaps**). The port dropped Portisch's per-bucket startup-delay compensation, does a 16-bit division *after* asserting the RF edge (~38–40 µs charged to the pulse already on air), and reloads Timer-1 one interval long — an error that is **additive** rather than proportional to bucket length, but not identical on every edge | **Re-capture every code on the V2.2 board itself**; codes captured on an EFM8BB1 Portisch bridge may not replay. Then pick **one** correction, never both: hand-tune the bucket values yourself, **or** leave them untouched and set the package's `tx_bucket_offset_us` substitution (**default `"0"`, byte-identical for every well-formed frame**) — see the notes below the table. |
| [#19](https://github.com/mightymos/RF-Bridge-OB38S003/issues/19) (open) | After ~24–48 h the radio MCU's **receive** path stops decoding codes. Transmit and the ESP8285/Wi-Fi keep working normally, and restarting the ESP does not clear it | Reset the radio MCU on a schedule (`AA FE 55`) or power-cycle the board; there is no firmware fix. Alarm on missing **inbound** traffic — availability stays `online` throughout. |
| No stock image published | Stock OB38S003 firmware is read-protected and is destroyed by the `erase` that unprotects the chip | There is nothing to roll back to. Unlike the EFM8BB1 path, no vendor original `.hex` exists. |

### Why issue #27 is corrected in the host, not here

Fixing the timing properly means editing this firmware, and we cannot: upstream is unmaintained
(see above), and calibrating a timing fix needs RF measurement hardware this project does not
have. Because the error is additive, the host can *narrow* it by subtracting a constant from every
bucket before the frame goes out over UART — no rebuild of an 8051 image, no new binary to trust,
and the correction stays per-board where the measurement actually lives.

Narrow, not cancel. A bucket index is used as both a pulse and a gap within one frame and carries
a single 16-bit duration, so one constant cannot correct +90 µs and +56 µs separately. Subtracting
the mean of the two nulls the mean and leaves roughly **±17 µs on every edge**; no single
host-side value removes that residual.

That is `tx_bucket_offset_us`, a package substitution documented in
[HARDWARE.md → caveat 2a](../../HARDWARE.md#alternate-path--r2-v22-with-the-ob38s003-radio):

- **Default `"0"`, opt-in, and byte-identical for every well-formed frame** — a bridge that does
  not set it emits exactly the bytes it always did. EFM8BB1 boards must leave it at `"0"`; stock
  Portisch already compensates. One deliberate exception applies at every offset including `"0"`:
  a frame carrying the `AAB0` magic that is not valid, even-length hex — or is too short to carry
  the header that magic implies — is dropped rather than transmitted, because the serializer would
  otherwise invent nibbles the author never wrote.
- **It replaces hand-tuning; it does not follow it.** These are two ways to apply the same
  correction, so pick one. If you already subtracted the overshoot from your bucket values, this
  knob subtracts it a second time, the buckets fall as far short of the receiver's window as they
  were long, and **blinds silently stop responding** — the bridge still publishes `started`, which
  only proves UART dispatch, never RF emission. A hand-tuned frame is structurally identical to an
  untuned one, so nothing can detect the mistake for you: the knob will over-correct it silently.
- **Found empirically per board** and **compile-time**: changing it requires a recompile and an
  OTA, not a runtime setting. The residual is V-shaped in the offset with its minimum at the mean
  of the pulse and gap errors — `73 µs` for the +90/+56 measurement above — so search a small
  range around the mean. The three figures quoted here are one effect measured three ways, not a
  range to sweep: `+30 µs` is the Flipper Zero reading, `+76 µs` the original single-number
  RTL-SDR reading, and `+90`/`+56` that same RTL-SDR reading resolved per edge.

## Updating this pin

If a newer upstream release ever appears: download the matching
`portisch_main_OB38S003_BUCKET_SNIFFING_INCLUDED.hex`, replace the file here, regenerate
`SHA256SUMS`, and update the provenance table above. Re-verify the `RF_DATA_BUFFERSIZE` value in
that release's `inc/portisch_rf_handling.h` before trusting the buffer-size rationale, and treat
the bucket timings on every already-flashed bridge as needing re-tuning against the new build.
