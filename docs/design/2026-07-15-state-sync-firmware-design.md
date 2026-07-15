# RF433 State-Sync Firmware Primitives — Design

- **Status:** Draft — pending owner review (do not implement until approved)
- **Date:** 2026-07-15
- **Repo:** `esphome-rf433-mqtt-bridge` (firmware). The Home Assistant integration
  (`zemismart_blinds`) is the *consumer* of this contract; its state-sync logic is a **separate
  follow-up spec** (see §10).
- **Reviewers so far:** Codex `gpt-5.6-sol` (max reasoning) — design gate, verdict `REVISE`; all 11
  findings accepted and folded in (dispositions in §11).

---

## 1. Goal & scope

Let the integration **correlate a physical remote press** — heard over RF by one or more bridges — with
a blind, and **update its travel-time position model** as if it had commanded that move ("mirror"),
**without the bridge ever transmitting in response to a heard frame** (the RX-never-TX invariant).

The sniff-based **onboarding/reconfigure** path already shipped (`rf433_rx.h`, `/cmd sniff`, `/rx`; PR
merged as `2e1c4d5`). This spec adds the **continuous state-sync primitives** that onboarding
deliberately deferred.

**In scope (this spec):** the firmware MQTT contract additions and the firmware behavior behind them —
continuous idle-listen, an enriched emission/handoff timestamp, a boot-session id, and a fail-safe
`disarm`. **Out of scope:** all integration-side correlation logic (offset estimation, echo windows,
peer selection, retry policy, mirror semantics) — described here only far enough to prove the firmware
surface is *sufficient*.

---

## 2. Settled decisions & grounding facts

**Owner-approved decisions (do not relitigate):**

- **D1 — Thin firmware, integration-central brain.** The firmware listens-when-idle and reports
  observations with good timestamps; **all** echo-classification / press-correlation lives in the
  integration. No bridge-to-bridge gossip, no firmware-side classification.
- **D2 — Listen whenever truly idle.** Auto-enter Portisch bucket sniff whenever the bridge is idle;
  yield to TX the instant a frame or STOP is due. Gated by a per-bridge **compile-time** enable
  (default **off**) so existing bridges are unaffected until deliberately re-flashed.
- **D3 — Mirror semantics.** A heard press drives the integration's travel model; the bridge never
  transmits on a heard press.
- **D4 — Echo / RF-emission proof needs ≥2 bridges, integration-side, configurable.** "A peer bridge
  hearing our transmitted frame" is the only available proof that RF actually left the antenna.

**Grounding facts (verified against the code / hardware model; Codex refinements applied):**

- **G1 — RF frames carry no controllable provenance.** A physical AOK remote and a bridge replaying the
  learned frame are **identical at the decoded-RF-payload level** (onboarding learns the remote's frame;
  TX replays it). *Nuance:* raw B0/B1 UART strings and captured bucket timings differ across receivers,
  so classification must compare the integration's **canonical decode**, never raw-string equality.
  ⇒ correlation is a **time** problem, not a tagging problem.
- **G2 — The EFM8BB1 does RX or TX, never both.** A transmitting bridge is deaf. Continuous listen =
  "sniff when idle, yield to TX." *Nuance:* `send_raw`, B1-start, and A7-leave are **unacknowledged UART
  writes** — the firmware gets no coprocessor confirmation of on-air completion (this drives finding 1).
- **G3 — "Idle" excludes a long move's wait-for-STOP.** A `WAIT_STOP` command stays in `commands_`, and a
  displaced obligation stays in `flush_stops_`, until its STOP drains — so listening never delays a STOP.
  (Confirmed at `rf433_scheduler.h` phase transition.)

---

## 3. Correlation model (why time, not tags)

Because of G1, the integration classifies a heard frame purely by **when** it was heard relative to what
it commanded. The four jobs and what each needs from the firmware:

| Integration job | Needs from firmware |
|---|---|
| **Suppress our own echo** | A heard frame that matches (canonically) a frame we commanded, within the air-time window of that command's **handoff timestamp** → echo, ignore. |
| **Prove RF emission (≥2 bridges)** | We commanded bridge A (saw A's handoff) **and** a *peer* bridge heard the matching frame right after → the frame truly emitted. A bridge's own handoff is **not** emission proof (G2). |
| **Detect a physical press** | A heard frame that matches **no** recent command of ours → a real press → mirror the move, timing from the hearing bridge's clock. |
| **Not fight a takeover** | When a press is correlated to a blind that has an **armed fail-safe STOP**, cancel that STOP so it does not fire against the user. |

Everything hangs on one shared idea: stamp the messages the integration correlates with a monotonic
per-bridge clock **`t`** and a **`boot`** session id, and give the integration a precise **handoff**
instant for each emission plus a **disarm** it can trust.

---

## 4. MQTT contract (`rf433/<bridge_id>/…`)

### C1 — `/rx` (heard-frame report) — now continuous
Published on every accepted RX capture **whenever listening is active** (continuous idle-listen *or* a
bounded onboarding sniff), not only during onboarding. QoS 1, **not retained**.

```json
{ "frame": "<raw B1 capture>", "t": <uint32 ms>, "boot": <uint32> }
```

Firmware publishes the **raw** B1 capture. Canonical decode / AOK signature / matching stays entirely in
the integration's codec (thin firmware, no duplicated DSP; a firmware signature would fork the codec and
create version skew — Codex-confirmed).

### C2 — `/status "started"` (emission **handoff** timestamp) — enriched
`started` already fires the instant a command's first action frame is handed to the coprocessor, and
already carries `command_id`. We **enrich it**, rather than invent a new `/emitted` topic (Codex:
"preferable — this hardware supplies no emission acknowledgement").

```json
{ "status":"started", "command_id":"…", "age_ms":N, "t":<uint32 ms>, "boot":<uint32> }
```

**Single-instant rule (finding 8).** There is exactly one authoritative dispatch instant per command:
`Command.started_at_ms`, recorded once inside `next()` when the first frame is selected
(`rf433_scheduler.h:409`). **Both** the fresh-dispatch and the replay publications compute:

```
t      = millis()                       // message time
age_ms = millis() − started_at_ms       // ALWAYS computed, never hardcoded 0
handoff = t − age_ms  (mod 2^32)  ==  started_at_ms   // the single stored instant
```

The integration uses **`handoff`** (the bridge-clock instant the frame reached the coprocessor).
`started.t` is **UART-handoff time, not RF-emission time** — emission proof comes only from a peer's
`/rx` (D4/G2). The fresh path stops hardcoding `age_ms = 0`; it publishes the true few-ms `send_raw`
latency, so both paths derive `handoff` from the same `started_at_ms`.

### C3 — `/info` (retained bridge metadata) — add capability fields
Retained, republished ≤ every 60 s (existing self-heal). Add `boot`, `listen`, and a contract version so
the integration can (a) learn the live boot session with no traffic and (b) know **which** bridges
actually listen — required for the ≥2-bridge corroboration in D4 (finding 10).

```json
{ "bridge":"…", "area":"…", "default":<bool>, "boot":<uint32>, "listen":<bool>, "v":2 }
```

`v` = contract version (`2` = state-sync capable; pre-state-sync builds omit it or send `1`).

### C4 — `/cmd {"action":"disarm","command_id":"…"}` (new action)
Cancels the fail-safe STOP obligation for `command_id`. **Emits no RF** (a suppression, not an emission —
preserves RX-never-TX). Semantics in §5-B2. Cancellation always wins (bypasses `CMD_RATE_LIMIT_MS`, same
rule as `sniff seconds==0`).

### C5 — `/status "disarmed"` (disarm acknowledgement) — new
Published whenever a `disarm` is processed — **including** for an unknown/never-seen id (idempotent), so
the integration can retry-until-acked and *know* the cancellation landed (finding 7). Without it, takeover
suppression would be silently best-effort.

```json
{ "status":"disarmed", "command_id":"…", "t":<uint32 ms>, "boot":<uint32> }
```

---

## 5. Behavior

### B1 — Idle-listen state machine

**Enable.** Per-bridge compile-time substitution `${listen_enabled}` (default `"false"`), alongside
`bridge_area` / `default_bridge`. Off ⇒ **today's exact behavior**, zero RX overhead. Participation is a
deployment decision, so a runtime toggle is unnecessary (§7).

**"Truly idle" = scheduler-empty AND RF-air-clear (finding 1).** Scheduler emptiness
(`commands_.empty() && flush_stops_.empty()`, exposed as a new cheap `TargetScheduler::idle()`) means the
last frame was *handed over UART* — but its embedded repeats may still be on air for up to
`MAX_FRAME_AIRTIME_US` (G2; the ~2 s bound at `rf433_scheduler.h:26`). Entering bucket-sniff then could
truncate TX or corrupt the mode switch. So idle also requires **`now ≥ rf_busy_until`**, where
`rf_busy_until` is set after each dispatch to `dispatch_time + airtime_of_frame + margin`. The airtime is
**already computed** by the scheduler (`airtime_us * embedded_repeat`, `rf433_scheduler.h:141`); reuse it
rather than recompute. A conservative fallback is the existing `MAX_FRAME_AIRTIME_US` upper bound.

**Logical intent vs. physical radio state (finding 2).** Represent them **separately**:

- *Logical listen intent:* `OFF` | `IDLE` | `BOUNDED(deadline)`. `BOUNDED` is an onboarding `/cmd sniff`
  window; its **deadline keeps ticking through a TX pre-emption** (a mid-window transmit must not shorten
  the user's requested sniff). `IDLE` is auto-selected when `listen_enabled && scheduler.idle()`.
- *Physical radio state:* a single `radio_sniffing` bool tracking whether the coprocessor is actually in
  bucket mode.

**Reconciler.** All `start_bucket_sniffing()` / `stop_advanced_sniffing()` (Portisch B1 / A7) calls are
**centralized in the 5 ms tick** and fired **only on a physical-state edge** (desired-sniffing changed),
never every tick — avoids A7 spam and mode-switch storms when the bridge oscillates idle↔busy:

```
desired_sniffing = (intent==BOUNDED && !expired) ||
                   (intent==IDLE && listen_enabled && scheduler.idle() && now>=rf_busy_until)
before dispatching any frame/STOP: force desired_sniffing=false (yield to TX)
if desired_sniffing != radio_sniffing: emit B1 or A7; radio_sniffing = desired_sniffing
```

A `BOUNDED` cancel (`sniff 0`) falls straight to `IDLE` when `listen_enabled` (not `OFF`).
`should_publish()` is true whenever intent ≠ `OFF` **and** `radio_sniffing` — so `/rx` fires during
idle-listen but never while the radio has been yielded for TX.

**Mode-switch latency** (leaving sniff before a TX) is **absorbed** by C2's `handoff` timestamp — it costs
a little perceived responsiveness, not model accuracy. Validate the real latency on hardware before fleet
rollout (§8); the compile-time flag lets us keep it off if it proves material.

### B2 — Disarm (atomic abort)

`TargetScheduler::disarm(command_id)` + the C4 action. Disarm is triggered by a **detected physical
takeover**, so it must stop *everything* for that command, not just the fail-safe STOP.

- **Atomic abort (finding 4).** If `command_id` is live in `commands_`, **`erase_()` the entire
  `Command`** (not merely "clear its stop phase" — clearing only the STOP would leave remaining
  action/trailer repeats eligible, so the bridge would keep driving UP while the user pressed DOWN).
  `erase_()` already repairs `order_` and `cursor_` (`rf433_scheduler.h:587`, Codex-confirmed safe under
  ESPHome's cooperative execution). Remove **every** matching entry from `flush_stops_`. After disarm,
  `next()` can never select a frame for that id.
- **Unconditional terminal tombstone (finding 5).** `/tx` and `/cmd` are separate topics with **no
  cross-topic ordering guarantee** — a disarm can arrive *before* its `/tx`. So a valid disarm
  **always** writes a terminal state for `command_id` into the dedup ring
  (`remember_(command_id, DISARMED)`), even when the id is not currently live. A later (reordered or
  QoS-1-redelivered) original `/tx` for that id then resolves to a **terminal no-op** (`/status
  "displaced"`, no scheduling), so it can never re-arm the STOP we cancelled.
- **Bounded retention, honestly scoped (finding 6).** The tombstone lives in the existing 64-entry
  `recent_ids_` ring (`COMMAND_ID_RING_SIZE`). The "never re-arms" guarantee therefore holds **within the
  same boot and the 64-id dedup window** — ample for real QoS-1 redelivery windows (seconds). Beyond it,
  the integration's own idempotency (it will not resend a `/tx` it has disarmed, and it retries disarm
  until acked) is the backstop. The spec states this bound; it does not claim an unbounded guarantee.
- **Acknowledged (finding 7).** Every processed disarm publishes C5 `/status "disarmed"` (idempotent).
- **Emits nothing / cancellation wins.** No RF (RX-never-TX preserved); bypasses `CMD_RATE_LIMIT_MS`.
- **Race with a firing STOP.** A STOP already selected/sent in the same tick **cannot be recalled**
  (Codex-confirmed). Disarm prevents *future* firing only; if the STOP already emitted, the integration
  hears its echo on `/rx` and reconciles. This is inherent and accepted.
- **Granularity:** by `command_id` only. No "disarm all" in v1 — the integration knows the ids it armed.

### B3 — Timestamp / boot / wrap contract

- **`t` = `millis()`** (uint32 ms since boot). **Monotonic modulo 2³²** — non-decreasing under
  serial-number (RFC-1982-style) arithmetic, **equal stamps allowed**, and the single ~49.7-day wrap
  interpreted via signed `int32_t` difference (the pattern the scheduler already uses, e.g.
  `deadline_reached`). It is **not** claimed non-decreasing in absolute value (finding 9).
- **`boot`** = a 32-bit value captured once at startup that **changes across reboots with overwhelming
  probability** (random 32-bit; ~2⁻³² collision — stated probabilistically, not as a guarantee). Sourced
  from an **ESP8266/ESP8285-supported RNG abstraction** (e.g. `os_random()`), confirmed by the compile
  gate — **not** ESP32's `esp_random()`.
- **Integration responsibility** (separate spec, stated to size the contract): maintain a per-bridge
  offset between bridge `t` and the HA clock; treat **either** a `boot` change **or** a large backward
  `t` jump within a stable `boot` as a reboot/wrap and re-estimate (belt-and-suspenders covers a boot
  collision). This is **windowed correlation** (~hundreds of ms), **not** wall-clock sync — per-bridge
  monotonic `t` + message arrival-time anchoring suffices; cross-bridge absolute time sync is a non-goal.

---

## 6. Firmware surfaces changed (implementation sketch)

- **`rf433_rx.h`** — replace the 3-value sniff enum with separated *intent* (`OFF/IDLE/BOUNDED(deadline)`)
  + a `radio_sniffing` bool; `should_publish()` gated on both; bounded deadline survives pre-emption;
  bounded-cancel→IDLE when enabled.
- **`rf433_scheduler.h`** — add `bool idle()`; add `rf_busy_until_` (set from the already-computed
  `airtime_us * embedded_repeat` at dispatch) + an `rf_air_clear(now)` predicate; add
  `disarm(command_id)` (erase Command via `erase_()`, purge `flush_stops_`, `remember_(id, DISARMED)`);
  add the `DISARMED` terminal state to `remember_`/`replay_state`.
- **`rf433-mqtt-bridge.yaml`** — reconcile B1/A7 in the 5 ms tick against `desired_sniffing`; enrich both
  `started` publications (`t`, `boot`, always-computed `age_ms`); add `boot`/`listen`/`v` to the retained
  `/info` string; add the `disarm` action + `/status "disarmed"` ack to the `/cmd` handler; establish the
  `boot` global at startup.
- **`components/rf_bridge/rf_bridge.cpp`** — **lower/redact the raw-B1 INFO log** (`:21`) so continuous
  listen does not stream real RF identities to the local log (finding 11).

---

## 7. Non-goals (YAGNI)

Cross-bridge absolute time sync (NTP/PTP); firmware-side echo classification / bridge gossip (D1); a
"re-arm" primitive (the integration just issues a fresh `/tx` with `stop_after_ms`); injecting provenance
into RF frames (impossible — G1); a **runtime** enable/disable of idle-listen (compile-time flag
suffices); persisting fail-safe STOPs or the boot id across reboot (a reboot mid-move is an
already-documented accepted limitation); a per-observation RF sequence number (the integration dedups
exact `{bridge,boot,t,frame}` QoS-1 duplicates and clusters RF repeats into presses — Codex-confirmed
sufficient).

---

## 8. Testing & rollout gates

**Native unit tests** (pytest over the headers, matching `tests/test_rx_firmware.py` +
`tests/test_firmware.py`):

- `rx_state` intent transitions `OFF/IDLE/BOUNDED`; scheduler-idle + `rf_air_clear` gating; TX
  pre-emption yields the radio; resume after idle; bounded deadline survives a pre-emption; bounded-cancel
  → IDLE when enabled; `should_publish` matches `radio_sniffing`.
- `TargetScheduler::idle()`; `rf_busy_until` set from frame airtime and cleared correctly.
- `disarm()`: erases a live Command so `next()` emits nothing further; purges `flush_stops_`; idempotent
  on an unknown id; writes a terminal tombstone so a **replayed original `/tx` does not re-arm**; a
  disarm delivered **before** the `/tx` still tombstones (reordering); publishes `disarmed`.
- Timestamp/boot: `age_ms`/`handoff` consistency across fresh vs. replay (same `started_at_ms`);
  serial-number monotonicity incl. a synthetic wrap; `boot` present on stamped messages.
- **Invariant test:** the RX path has no reachable route to `schedule()` / `send_raw` (RX-never-TX).

**Compile gate:** `uvx --from esphome==2026.6.5 esphome compile examples/living-room.yaml` must succeed
(the yaml lambdas — including the RNG abstraction — are validated only by compile).

**Mandatory hardware rollout gates (finding 3 — HIGH).** The AOK receive filter currently has **only
synthetic fixtures, no real OEM captures** (`README.md:110`); a real UP/DOWN/STOP that violates a
hard-coded pulse/trailer/timing assumption is **silently dropped**, so a takeover would never be
reported. Before fleet rollout:

- Capture **real OEM frames** across actions × identities × channels × timing jitter, from **multiple
  bridges**, and validate the AOK filter against them as golden fixtures.
- Hardware-verify `B1 → A7 → B0` sequencing and **post-B0 RX-resume / air-clear** timing (validates
  finding 1's `rf_busy_until` margin).
- A thermal / loop-load **soak under noisy RF** with continuous sniff enabled.

> **Privacy reconciliation (project rule).** Real captures embed real `0x5C`-prefixed identities, which
> must **never** be committed. Golden fixtures are either **identity-anonymized** (real timing/structure,
> synthetic identity bytes) or kept in the **gitignored** local house data (`.working/`) and run as a
> local-only gate. The committed repo stays synthetic-only.

---

## 9. Privacy & security (finding 11)

Continuous `/rx` is a persistent **household-activity stream** (which remote was pressed, when). Mitigations,
consistent with the bridge's existing posture (suppressed retained `log_topic`, no discovery/IP metadata):

- Keep it **compile-time opt-in** (default off) and the topic **non-retained** (no broker session backlog
  of activity).
- **Redact / lower** the vendored component's raw-B1 INFO log (§6) so identities do not stream to any log
  sink.
- **Document the broker ACL**: scope `rf433/<bridge>/rx` (and `/cmd`) to the integration's principal only;
  a wildcard subscriber must not be able to harvest the stream.

---

## 10. Integration-side follow-ups (separate spec)

The consumer spec (in `zemismart_blinds`) will cover: per-bridge `t`↔HA-clock offset estimation with
reboot/wrap detection; the echo-match air-time window; cross-bridge emission-proof correlation; peer
selection driven by `/info.listen` + `/info.v`; `{bridge,boot,t,frame}` dedup and RF-repeat clustering;
mirror application to the travel model; and **retry-until-`disarmed`-acked** takeover suppression with a
best-effort fallback if a bridge is unreachable.

---

## 11. Codex review disposition (verdict `REVISE` → all 11 folded in)

| # | Sev | Area | Resolution |
|---|-----|------|-----------|
| 1 | HIGH | idle vs. air-clear | §5-B1 `rf_busy_until` from computed airtime |
| 2 | MED | listen state model | §5-B1 intent vs. `radio_sniffing`, reconciler on edges, bounded survives pre-emption |
| 3 | HIGH | AOK validation | §8 real-OEM golden + hardware gates (with §8 privacy reconciliation) |
| 4 | HIGH | disarm scope | §5-B2 atomic abort via `erase_()` |
| 5 | HIGH | cross-topic ordering | §5-B2 unconditional terminal tombstone |
| 6 | MED | tombstone retention | §5-B2 honestly bounded to boot + 64-id window |
| 7 | MED | disarm ack | §4-C5 `/status "disarmed"` + retry-until-acked |
| 8 | MED | started timestamp | §4-C2 single `started_at_ms`, always-computed `age_ms` |
| 9 | MED | clock contract | §5-B3 monotonic mod 2³², probabilistic `boot`, ESP8266 RNG |
| 10 | LOW | `/info` capability | §4-C3 `listen` + `v` |
| 11 | MED | privacy | §6 log redaction + §9 non-retained/opt-in/ACL |

**Codex-validated (no change needed):** thin-firmware architecture; enrich `started` over a new
`/emitted`; raw-B1 contract (no firmware sig); `disarm` structurally feasible under cooperative
execution; RX-never-TX enforceable; no per-observation sequence required; continuous RX poses no TX
duty-cycle problem.
