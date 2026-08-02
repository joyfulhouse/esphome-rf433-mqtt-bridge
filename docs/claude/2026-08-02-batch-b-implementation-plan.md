# Batch B Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three publication-gating safety blockers (MQTT inbound cap, OTA bounded wait-for-idle, retained-`/tx` boot binding) per the approved spec at `docs/design/2026-08-02-batch-b-safety-design.md`, across the firmware (v1.4.0) and the zemismart-blinds consumer (v0.8.0).

**Architecture:** Blocker 1 vendors ESPHome 2026.7.3's `mqtt` component with a pre-allocation payload cap behind a host-testable guard header. Blocker 2 rewrites `ota.on_begin` to pump the scheduler's own dispatch path for up to 30 s before falling back to the existing early-STOP flush, and resets the `/tx` latch on OTA error. Blocker 3 adds a required `boot` field to `/tx` (contract v3), stamped by the consumer from its already-strict registry boot snapshot and enforced by the firmware.

**Tech Stack:** ESPHome 2026.7.3 YAML lambdas + header-only C++17 (host-tested via inline-compiled `.cpp` per test), pytest, `uv`; consumer is a Home Assistant custom integration (Python 3.13, pytest).

## Global Constraints

- Firmware repo root: `/Users/bryanli/Projects/joyfulhouse/homeassistant-dev/esphome-rf433-mqtt-bridge` ("FW"); consumer repo root: `/Users/bryanli/Projects/joyfulhouse/homeassistant-dev/zemismart-blinds` ("HA"). Each task states which repo it runs in.
- Python: always `uv` (`uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`); never pip.
- Host C++ tests compile with `c++ -std=c++17 -Wall -Wextra -Werror -I <PROJECT_ROOT>` (existing harness pattern; keep it).
- ESPHome version is pinned: **2026.7.3** everywhere (vendor source, compile checks, this repo's CI, README dev commands). Decided 2026-08-02: the fleet's esphome-config CI (currently 2026.7.2) bumps to 2026.7.3 at rollout — the mqtt component is byte-identical between 7.2 and 7.3. The old 2026.6.5 pin was stale.
- Spec-pinned constants, verbatim: `MAX_INBOUND_PAYLOAD = 4096` (bytes), `OTA_IDLE_WAIT_MS = 30000` (ms), rejection reason string `"boot_mismatch"`, `/info` contract version `"v": 3`.
- Never disable linter rules; fix root causes.
- Commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Work on branch `batch-b-safety` (FW) and `contract-v3-boot-binding` (HA); do NOT merge or tag — PRs stop for review.
- No house-specific identifiers (real bridge names, broker hosts, remote identities) in any public-repo file; canary material with real names goes to the private repo only (Task 9).

---

### Task 1: File the three public tracking issues (FW)

**Files:** none (GitHub only).

**Interfaces:**
- Produces: three issue numbers, referenced throughout this plan as `#<cap>`, `#<ota>`, `#<boot>` — substitute the real numbers in every later commit message and in Task 8's PR bodies.

- [ ] **Step 1: Create the issues**

```bash
cd /Users/bryanli/Projects/joyfulhouse/homeassistant-dev/esphome-rf433-mqtt-bridge
gh issue create --title "Inbound MQTT payload assembly is unbounded (heap exhaustion / STOP-deadline stall)" --body "ESPHome's stock mqtt component reserves the broker-declared total payload size (payload_buffer_.reserve(total)) before any integration-level validation runs. On the ESP8285 a tens-of-KB publish can exhaust heap or stall past an armed fail-safe STOP deadline. The JSON layer's 5120-byte document cap applies only after assembly. Fix per docs/design/2026-08-02-batch-b-safety-design.md §1: vendor the mqtt component with a 4 KiB pre-allocation cap; upstream the cap afterwards."
gh issue create --title "OTA lacks a scheduler interlock beyond the early-STOP flush; failed OTA latches /tx shut" --body "An OTA mid-move interrupts motion (v1.3.0 flushes armed STOPs early — fail-safe but interrupting), and ota_active is never reset, so a FAILED transfer leaves the bridge rejecting all /tx until manual reboot. Fix per docs/design/2026-08-02-batch-b-safety-design.md §2: on_begin waits up to 30 s for natural idle (pumping dispatch so STOPs fire at their real deadlines) before falling back to the flush; on_error resets ota_active."
gh issue create --title "A retained /tx replays after reboot (RAM dedup ring cannot see it)" --body "The ESP8266 MQTT backend discards the retained flag and the 64-entry command-id ring is RAM-only, so a retained /tx replays after a reboot with no defense beyond documentation. Fix per docs/design/2026-08-02-batch-b-safety-design.md §3: contract v3 requires a boot field on /tx equal to the bridge's current boot_id; retained replays carry the previous boot and are rejected with reason boot_mismatch."
```

- [ ] **Step 2: Verify and note the numbers**

Run: `gh issue list --state open`
Expected: exactly 3 open issues. Note their numbers for Task 8's PR body (`Closes #a, Closes #b, Closes #c`).

---

### Task 2: Vendor the ESPHome 2026.7.3 mqtt component (FW, no behavior change yet)

**Files:**
- Create: `components/mqtt/` (full vendored copy)
- Create: `components/mqtt/README.md`
- Modify: `rf433-mqtt-bridge.yaml:52-57` (register `mqtt` in `external_components`)

**Interfaces:**
- Produces: a byte-identical vendored `components/mqtt/` that Task 3 patches. ESPHome resolves the local copy instead of the built-in component.

- [ ] **Step 1: Copy the component from the pinned ESPHome release**

```bash
cd /Users/bryanli/Projects/joyfulhouse/homeassistant-dev/esphome-rf433-mqtt-bridge
SRC=$(uvx --from "esphome==2026.7.3" python -c "import esphome.components.mqtt as m, pathlib; print(pathlib.Path(m.__file__).parent)")
cp -R "$SRC" components/mqtt
find components/mqtt -name "__pycache__" -type d -exec rm -rf {} +
```

- [ ] **Step 2: Write the vendor README**

Create `components/mqtt/README.md` following the pattern of `components/rf_bridge/README.md` (state: vendored from ESPHome 2026.7.3, why — inbound payload cap the stock component cannot apply, see design doc — and that the only behavioural diff is the guard added in the next task; everything else is byte-identical to upstream).

- [ ] **Step 3: Register the component**

In `rf433-mqtt-bridge.yaml`, extend the existing block:

```yaml
external_components:
  - source:
      type: local
      path: components
    components:
      - rf_bridge
      - mqtt
```

- [ ] **Step 4: Prove the vendored copy compiles unpatched**

```bash
cp secrets.example.yaml secrets.yaml
cp examples/living-room.yaml .
uvx --from "esphome==2026.7.3" esphome compile living-room.yaml
rm living-room.yaml secrets.yaml
```

Expected: clean compile (this is the same staging CI performs). If ESPHome rejects the vendored layout, fix before proceeding — nothing else in this plan works without it.

- [ ] **Step 5: Bump the repo's own ESPHome pin**

Update every `esphome==2026.6.5` occurrence to `esphome==2026.7.3` in `.github/workflows/ci.yml` and `README.md` (dev commands ~lines 158-159; leave the historical rf_bridge vendor provenance note at README ~line 134 as-is — it records where that copy came from).

- [ ] **Step 6: Commit**

```bash
git add components/mqtt rf433-mqtt-bridge.yaml .github/workflows/ci.yml README.md
git commit -m "vendor: ESPHome 2026.7.3 mqtt component, byte-identical (pre-patch); bump CI pin"
```

---

### Task 3: Inbound payload guard + patch (FW)

**Files:**
- Create: `components/mqtt/rf433_inbound_guard.h`
- Modify: `components/mqtt/mqtt_client.cpp` (the `setup()` `set_on_message` lambda, currently `payload_buffer_.reserve(total)` around line 48-63)
- Test: `tests/test_firmware.py`

**Interfaces:**
- Consumes: vendored `components/mqtt/` from Task 2.
- Produces: `rf433::MAX_INBOUND_PAYLOAD` (size_t, 4096), `rf433::accept_inbound_payload(size_t declared_total) -> bool`, `rf433::inbound_drop_log_due(uint32_t now_ms) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_firmware.py`:

```python
VENDORED_MQTT_CLIENT = PROJECT_ROOT / "components" / "mqtt" / "mqtt_client.cpp"


def test_vendored_mqtt_carries_inbound_payload_guard() -> None:
    """A re-vendor of the mqtt component must not silently drop the cap patch."""
    client = VENDORED_MQTT_CLIENT.read_text()
    assert '#include "rf433_inbound_guard.h"' in client
    on_message = client.split("set_on_message", maxsplit=1)[1].split(
        "set_on_disconnect", maxsplit=1
    )[0]
    # The guard must run before the broker-declared reserve() it exists to stop.
    assert "rf433::accept_inbound_payload(total)" in on_message
    assert on_message.index("accept_inbound_payload") < on_message.index("reserve(total)")


def test_native_inbound_guard_boundaries_and_log_throttle(tmp_path: Path) -> None:
    """Pin the 4 KiB cap boundary and the once-per-window drop log."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native guard test")
    source = tmp_path / "guard_test.cpp"
    binary = tmp_path / "guard_test"
    source.write_text(
        r"""
#include <cassert>
#include "components/mqtt/rf433_inbound_guard.h"

int main() {
  static_assert(rf433::MAX_INBOUND_PAYLOAD == 4096, "spec-pinned cap");
  assert(rf433::accept_inbound_payload(0));
  assert(rf433::accept_inbound_payload(4096));
  assert(!rf433::accept_inbound_payload(4097));
  assert(!rf433::accept_inbound_payload(60000));
  // First drop logs; repeats inside the 5 s window stay quiet; the window
  // reopens afterwards and survives a millis() rollover.
  assert(rf433::inbound_drop_log_due(0));
  assert(!rf433::inbound_drop_log_due(1));
  assert(!rf433::inbound_drop_log_due(4999));
  assert(rf433::inbound_drop_log_due(5000));
  assert(!rf433::inbound_drop_log_due(5001));
  assert(rf433::inbound_drop_log_due(0xFFFFFF00u));
  assert(rf433::inbound_drop_log_due(0xFFFFFF00u + 5000u));
  return 0;
}
"""
    )
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(PROJECT_ROOT),
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_firmware.py -k "inbound" -v`
Expected: both FAIL (missing header / missing patch).

- [ ] **Step 3: Create the guard header**

Create `components/mqtt/rf433_inbound_guard.h`:

```cpp
#pragma once

#include <cstddef>
#include <cstdint>

// Vendored-mqtt inbound payload guard. The stock on_message fragment
// assembler reserves the broker-declared total before any integration-level
// validation can run; on the ESP8285 a hostile or errant tens-of-KB publish
// can exhaust the heap or stall past an armed fail-safe STOP deadline.
// Everything this bridge legitimately receives fits well under 4 KiB (the
// largest /tx carries three ~1 KiB B0 fields plus small keys).
namespace rf433 {

static constexpr size_t MAX_INBOUND_PAYLOAD = 4096;

inline bool accept_inbound_payload(size_t declared_total) {
  return declared_total <= MAX_INBOUND_PAYLOAD;
}

// Once-per-5 s throttle so a flood of oversized publishes cannot itself
// become log spam. Rollover-safe via the signed-difference idiom used
// throughout rf433_scheduler.h.
inline bool inbound_drop_log_due(uint32_t now_ms) {
  static bool logged_once = false;
  static uint32_t next_log_ms = 0;
  if (logged_once && static_cast<int32_t>(now_ms - next_log_ms) < 0)
    return false;
  logged_once = true;
  next_log_ms = now_ms + 5000;
  return true;
}

}  // namespace rf433
```

- [ ] **Step 4: Patch the vendored client**

In `components/mqtt/mqtt_client.cpp`: add `#include "rf433_inbound_guard.h"` with the file's other local includes, then change the `setup()` on-message lambda from:

```cpp
  this->mqtt_backend_.set_on_message(
      [this](const char *topic, const char *payload, size_t len, size_t index, size_t total) {
        if (index == 0) {
          this->payload_buffer_.clear();
          this->payload_buffer_.reserve(total);
        }
```

to:

```cpp
  this->mqtt_backend_.set_on_message(
      [this](const char *topic, const char *payload, size_t len, size_t index, size_t total) {
        // rf433 vendored patch (rf433_inbound_guard.h): drop oversized
        // publishes before the broker-declared total forces a heap
        // reservation. Every fragment of an oversized message carries the
        // same total, so continuations die on the same test.
        if (!rf433::accept_inbound_payload(total)) {
          if (rf433::inbound_drop_log_due(millis())) {
            ESP_LOGW(TAG, "Dropping inbound publish on '%s': declared %u bytes exceeds cap %u",
                     topic, static_cast<unsigned>(total),
                     static_cast<unsigned>(rf433::MAX_INBOUND_PAYLOAD));
          }
          this->payload_buffer_.clear();
          return;
        }
        if (index == 0) {
          this->payload_buffer_.clear();
          this->payload_buffer_.reserve(total);
        }
```

(`TAG` and `millis()` already resolve inside this file's `esphome::mqtt` namespace; if the compile in Step 6 disagrees, use `esphome::millis()`.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_firmware.py -k "inbound" -v`
Expected: both PASS. Then the full suite: `uv run pytest -q` — no regressions.

- [ ] **Step 6: Full ESPHome compile**

Same staging commands as Task 2 Step 4. Expected: clean compile; RAM/flash within ~1% of v1.3.0's 47.3%/48.2%.

- [ ] **Step 7: Mutation check, then commit**

Temporarily delete the `if (!rf433::accept_inbound_payload(total))` block, run `uv run pytest tests/test_firmware.py -k vendored -v` — the presence test MUST fail. Restore the block, re-run to green, then:

```bash
git add components/mqtt tests/test_firmware.py
git commit -m "safety: cap inbound MQTT payload assembly at 4 KiB in the vendored client (#<cap>)"
```

---

### Task 4: OTA bounded wait-for-idle + on_error latch reset (FW)

**Files:**
- Modify: `rf433_scheduler.h` (constants block near `MAX_REPEAT_GAP_MS`, ~line 21-45)
- Modify: `rf433-mqtt-bridge.yaml:115-134` (`ota:` block)
- Test: `tests/test_firmware.py` (rewrites `test_generated_ota_begin_flushes_armed_stop_before_return`, whose extraction end-marker also changes)

**Interfaces:**
- Consumes: `TargetScheduler::next(uint32_t now_ms, std::string &started_command_id) -> optional<std::string>`, `idle()`, `rf_air_clear(uint32_t)`, `drain_armed_stops()` — all existing.
- Produces: `rf433::OTA_IDLE_WAIT_MS` (uint32_t, 30000). YAML gains an `on_error` block; the `on_begin` extraction marker pair becomes `("    on_begin:", "    on_error:")`.

- [ ] **Step 1: Add the constant**

In `rf433_scheduler.h`, next to the other tunables:

```cpp
// OTA begin blocks the main loop, so the 5 ms dispatch tick cannot run;
// on_begin pumps dispatch itself for at most this long waiting for natural
// idle before falling back to the early-STOP flush.
static constexpr uint32_t OTA_IDLE_WAIT_MS = 30000;
```

- [ ] **Step 2: Rewrite the OTA tests (failing first)**

In `tests/test_firmware.py`, replace `test_generated_ota_begin_flushes_armed_stop_before_return` with three tests. All three reuse its exact compile/run scaffolding (FakeBridge, `fake_now_ms`, `millis()`, `delay()` advancing the fake clock, `#define id(value) value`) — copy that scaffolding verbatim into each; only the extraction markers, embedded C++ `main()`, and lambda names change.

Test A — natural deadline inside the window (`ota_lambda = _firmware_lambda("    on_begin:", "    on_error:")`):

```cpp
int main() {
  const std::string action = "AAB005010100010055";
  const std::string stop = "AAB005010100000055";
  auto &scheduler = rf433::tx_scheduler(35);
  std::string started;
  std::string reason;
  std::vector<std::string> displaced;

  // One started timed command, repeats=1: only its fail-safe STOP remains,
  // due at 100 + 2000 = 2100.
  assert(scheduler.schedule("armed", "a1b2c3:01:1", action, "", 1, 2000, stop, 100,
                            displaced, reason));
  fake_now_ms = 100;
  auto raw = scheduler.next(100, started);
  assert(raw && *raw == action && started == "armed");
  portisch_rf_bridge.send_raw(*raw);

  fake_now_ms = 200;
  generated_ota_begin();
  assert(ota_active);
  // The pump let the STOP fire AT its deadline -- not early, not flushed.
  assert(portisch_rf_bridge.sent == std::vector<std::string>({action, stop}));
  assert(fake_now_ms >= 2100);
  assert(fake_now_ms < 2100 + 1000);  // and exited promptly once idle
  assert(scheduler.idle());
  assert(scheduler.drain_armed_stops().empty());
  return 0;
}
```

Test B — deadline beyond the window falls back to the flush:

```cpp
int main() {
  const std::string action = "AAB005010100010055";
  const std::string stop = "AAB005010100000055";
  auto &scheduler = rf433::tx_scheduler(35);
  std::string started;
  std::string reason;
  std::vector<std::string> displaced;

  assert(scheduler.schedule("armed", "a1b2c3:01:1", action, "", 1, 60000, stop, 100,
                            displaced, reason));
  fake_now_ms = 100;
  auto raw = scheduler.next(100, started);
  assert(raw && *raw == action && started == "armed");
  portisch_rf_bridge.send_raw(*raw);

  fake_now_ms = 200;
  generated_ota_begin();
  assert(ota_active);
  // Still armed at window end (due 60100 > 200 + 30000): early flush.
  assert(portisch_rf_bridge.sent == std::vector<std::string>({action, stop}));
  assert(fake_now_ms >= 200 + 30000);
  assert(fake_now_ms < 60100);  // flushed early, NOT at the natural deadline
  assert(scheduler.idle());
  return 0;
}
```

Test C — `on_error` resets the latch (`ota_error_lambda = _firmware_lambda("    on_error:", "\n\nmqtt:")`; scaffolding only needs the globals and `#define id`):

```cpp
int main() {
  ota_active = true;
  generated_ota_error();
  assert(!ota_active);
  return 0;
}
```

- [ ] **Step 3: Run to verify the new tests fail**

Run: `uv run pytest tests/test_firmware.py -k "ota" -v`
Expected: Test A/B fail (current lambda flushes immediately: `fake_now_ms >= 2100` in A fails); Test C fails (no `on_error:` section to extract).

- [ ] **Step 4: Implement the YAML change**

Replace the `ota:` block in `rf433-mqtt-bridge.yaml` (keep the platform/password lines and the leading comment, updating the comment's claims):

```yaml
    on_begin:
      then:
        - lambda: |-
            id(ota_active) = true;
            auto &sched = rf433::tx_scheduler(${repeat_gap_ms});
            // Bounded wait-for-idle: the 5 ms dispatch tick cannot run while
            // this callback blocks, so pump the scheduler's own dispatch path
            // here. In-flight trains then complete normally and their armed
            // STOPs fire at their real deadlines instead of early. `started`
            // is deliberately not published from this loop: the outbox cannot
            // flush while the loop blocks and a successful update reboots
            // moments later; the controller's started-timeout absorbs the
            // rare command that first dispatches inside this window.
            const uint32_t wait_start_ms = millis();
            while (!(sched.idle() && sched.rf_air_clear(millis()))) {
              if (static_cast<uint32_t>(millis() - wait_start_ms) >= rf433::OTA_IDLE_WAIT_MS)
                break;
              std::string started_command_id;
              const auto raw = sched.next(millis(), started_command_id);
              if (raw.has_value()) {
                id(portisch_rf_bridge).send_raw(*raw);
              } else {
                delay(1);
              }
            }
            // Anything still armed past the window gets the v1.3.0 early
            // flush: one immediate STOP per armed command, never delayed by
            // the discretionary gap, always honoring physical occupancy.
            const auto stops = sched.drain_armed_stops();
            for (const auto &stop : stops) {
              while (!sched.rf_air_clear(millis()))
                delay(1);
              id(portisch_rf_bridge).send_raw(stop.raw);
              delay(stop.occupancy_ms);
            }
    on_error:
      then:
        - lambda: |-
            // A failed transfer must not leave /tx latched shut until a
            // manual reboot.
            id(ota_active) = false;
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_firmware.py -k "ota" -v` → all three PASS. Then `uv run pytest -q` → full suite green (nothing else extracts the ota section, but verify).

- [ ] **Step 6: Full ESPHome compile, then commit**

Same staging commands as Task 2 Step 4; expected clean.

```bash
git add rf433_scheduler.h rf433-mqtt-bridge.yaml tests/test_firmware.py
git commit -m "safety: OTA waits up to 30 s for natural idle before the early-STOP flush; on_error unlatches /tx (#<ota>)"
```

---

### Task 5: Boot binding on /tx + contract v3 (FW)

**Files:**
- Modify: `rf433-mqtt-bridge.yaml` (/tx lambda ~line 225-229 region; tick lambda `"v"` at ~line 522)
- Test: `tests/test_firmware.py` (`test_generated_tx_and_tick_replay_started_once_after_reconnect` is the only test embedding the /tx lambda)

**Interfaces:**
- Consumes: `id(boot_id)` global (uint32_t; test scaffolding fakes it as `uint32_t boot_id{42};`).
- Produces: `/tx` requires `boot` (uint32, == current `boot_id`); rejection reason is exactly `"boot_mismatch"`; `/info` publishes `"v": 3`. The consumer (Task 6) relies on both.

- [ ] **Step 1: Extend the test harness (failing first)**

In `test_generated_tx_and_tick_replay_started_once_after_reconnect`'s embedded C++:

1. Widen the variant: `std::variant<std::monostate, std::string, int, bool, uint32_t>`.
2. Mirror ArduinoJson's integer semantics in `JsonValue`:

```cpp
  template<typename T> bool is() const {
    if (this->data == nullptr)
      return false;
    if constexpr (std::is_same_v<T, const char *>)
      return std::holds_alternative<std::string>(this->data->value);
    if constexpr (std::is_same_v<T, int>)
      return std::holds_alternative<int>(this->data->value);
    if constexpr (std::is_same_v<T, uint32_t>)
      return std::holds_alternative<uint32_t>(this->data->value) ||
             (std::holds_alternative<int>(this->data->value) &&
              std::get<int>(this->data->value) >= 0);
    return false;
  }

  template<typename T> T as() const {
    if constexpr (std::is_same_v<T, const char *>)
      return std::get<std::string>(this->data->value).c_str();
    if constexpr (std::is_same_v<T, int>)
      return std::get<int>(this->data->value);
    if constexpr (std::is_same_v<T, uint32_t>)
      return std::holds_alternative<uint32_t>(this->data->value)
                 ? std::get<uint32_t>(this->data->value)
                 : static_cast<uint32_t>(std::get<int>(this->data->value));
  }
```

3. Add `void set_uint(const std::string &key, uint32_t value) { this->values[key].value = value; }` to `FakeJson`.
4. Every command body the test builds gains `command.set_uint("boot", 42);` (the fake `boot_id` is 42).
5. New assertions in `main()` before the existing happy path:

```cpp
  // Contract v3: missing, mistyped, and mismatched boot all reject with the
  // single reason "boot_mismatch" and never reach the scheduler.
  mqtt_client.connected = true;
  FakeJson no_boot;
  no_boot.set_string("command_id", "no-boot-1");
  no_boot.set_string("target", "a1b2c3:01:1");
  no_boot.set_string("raw", frame);
  generated_tx_handler(no_boot);
  assert(mqtt_client.messages.back().payload.at("status") == "rejected");
  assert(mqtt_client.messages.back().payload.at("reason") == "boot_mismatch");

  FakeJson stale_boot;
  stale_boot.set_string("command_id", "stale-boot-1");
  stale_boot.set_string("target", "a1b2c3:01:1");
  stale_boot.set_string("raw", frame);
  stale_boot.set_uint("boot", 41);  // previous boot: the retained-replay shape
  generated_tx_handler(stale_boot);
  assert(mqtt_client.messages.back().payload.at("status") == "rejected");
  assert(mqtt_client.messages.back().payload.at("reason") == "boot_mismatch");
  assert(rf433::tx_scheduler(35).idle());
  mqtt_client.messages.clear();
  mqtt_client.connected = false;
```

6. Where the test's `generated_tick()` assertions inspect the `/info` message, assert `payload.at("v") == "3"` (the `JsonSlot` writer stringifies ints). If the current test doesn't inspect `/info`, add that assertion where the tick publishes it.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_firmware.py -k "replay_started_once" -v`
Expected: FAIL — the happy-path command now carries `boot` but the lambda doesn't require it, so the new rejected-status assertions fail (`messages` empty at that point).

- [ ] **Step 3: Implement the lambda change**

In the `/tx` lambda of `rf433-mqtt-bridge.yaml`, directly after the `if (id(ota_active)) { ... }` block and before the replay-state block, insert:

```cpp
          // Contract v3 session binding: a command must present this boot's
          // id (learned from retained /info). A retained /tx replayed after
          // a reboot carries the previous boot and dies here -- the RAM
          // dedup ring cannot survive the reboot; this check does not need
          // to. Missing, mistyped, and mismatched all collapse to one reason
          // so a controller has exactly one recovery path: re-read /info,
          // re-issue with the current boot.
          if (!x["boot"].is<uint32_t>() || x["boot"].as<uint32_t>() != id(boot_id)) {
            reject("boot_mismatch");
            return;
          }
```

In the tick lambda's `/info` publish, change `root["v"] = 2;` to `root["v"] = 3;`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: full suite green (also confirms no other test publishes `/tx` bodies — this is the only lambda-embedding one).

- [ ] **Step 5: Full ESPHome compile, then commit**

Same staging commands as Task 2 Step 4.

```bash
git add rf433-mqtt-bridge.yaml tests/test_firmware.py
git commit -m "safety: /tx requires the current boot (contract v3); retained replays die structurally (#<boot>)"
```

---

### Task 6: Consumer stamps boot and refuses boot-less bridges (HA)

**Files:**
- Modify: `custom_components/zemismart_blinds/transport.py` (`_finalize_and_publish`, ~line 2255-2291)
- Modify: `tests/test_cover.py` (`online_registry` helper, line 96; new tests)
- Modify: any other test fixture that exercises a publish path with `update_info` lacking `boot` (find via `grep -rn "update_info(" tests/`)

**Interfaces:**
- Consumes: `self._air_bridge_boot(bridge_id) -> int | None` (existing, transport.py:2294), `CommandRejectedError` (transport.py:295), `_revalidated_body` (returns the mutable body dict).
- Produces: every published `/tx` JSON body carries `"boot": <int>`; publishing to a bridge whose boot is unknown raises `CommandRejectedError`.

- [ ] **Step 1: Update fixtures and write the failing tests**

In `tests/test_cover.py`, give the standard fixture a boot:

```python
def online_registry(bridge_id: str = "bridge-a") -> BridgeRegistry:
    """Return one same-area online bridge with contract-v3 boot evidence."""
    registry = BridgeRegistry()
    registry.update_info(bridge_id, {"area": "living_room", "boot": 7})
    registry.update_availability(bridge_id, "online")
    return registry
```

Run `grep -rn "update_info(" tests/` and add `"boot"` to every fixture whose registry feeds a hub that publishes (test_init.py, test_state_sync.py, etc. — mechanical; fixtures that only test registry parsing stay as-is).

Add two tests (imports: `CommandRejectedError` from the transport module, plus the file's existing helpers):

```python
@pytest.mark.asyncio
async def test_published_tx_body_carries_bridge_boot(hass: HomeAssistant) -> None:
    """Contract v3: every /tx body is stamped with the routed bridge's boot."""
    published: list[str] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        published.append(payload)
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    try:
        await entity.async_open_cover()
        assert published
        body = json.loads(published[-1])
        assert body["boot"] == 7
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_bridge_without_boot_evidence_is_never_published_to(
    hass: HomeAssistant,
) -> None:
    """An availability-only bridge (retained /info lost) must not receive /tx."""
    published: list[str] = []

    async def publish(topic: str, payload: str) -> None:
        published.append(payload)

    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})  # no boot
    registry.update_availability("bridge-a", "online")
    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(hass, hub)
    try:
        with pytest.raises(CommandRejectedError):
            await entity.async_open_cover()
        assert published == []
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()
```

(If the cover entity wraps transport failures in `HomeAssistantError`, assert on that outer type instead — match whatever `async_open_cover` actually surfaces for `CommandRejectedError` today; the non-negotiable assertions are the raise and `published == []`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cover.py -k "boot" -v`
Expected: first test fails with `KeyError: 'boot'`; second fails because the publish currently succeeds.

- [ ] **Step 3: Implement the stamp + guard**

In `_finalize_and_publish`, immediately before `payload = json.dumps(body, separators=(",", ":"))`:

```python
        boot = self._air_bridge_boot(bridge_id)
        if boot is None:
            # Contract v3: without a strict boot snapshot from retained /info
            # the bridge cannot check session binding; a v1.4.0 bridge would
            # reject the command as boot_mismatch anyway, and an older bridge
            # this registry has no /info evidence for should not be driven.
            msg = f"bridge {bridge_id} has no boot evidence; refusing contract v3 publish"
            raise CommandRejectedError(msg)
        body["boot"] = boot
```

(The air-plan computation above already ran on the unstamped body, so `plan_for_body` input is unchanged.)

- [ ] **Step 4: Run the full consumer gate**

Run: `uv run ruff check --fix && uv run ruff format && uv run pytest -q`
Expected: green. Fix any existing test that asserted an exact `/tx` body without `boot` (add the key, don't weaken the assertion).

- [ ] **Step 5: Commit**

```bash
git add custom_components/zemismart_blinds/transport.py tests/
git commit -m "contract v3: stamp bridge boot into every /tx and refuse boot-less bridges"
```

---

### Task 7: Documentation + version bumps (both repos)

**Files:**
- FW modify: `README.md` (topic table `/tx` row + command-body block gains required `boot`; `/info` example `"v":3`; retained-tx warnings updated to "structurally rejected since v1.4.0, and still never publish them"; OTA behaviour section describes the 30 s wait-for-idle and the on_error unlatch; a short vendored-mqtt note pointing at `components/mqtt/README.md`), `CHANGELOG.md` (v1.4.0 section: the three closures, `Closes #<cap>/#<ota>/#<boot>`, lockstep note "requires a controller that stamps boot — zemismart-blinds ≥ 0.8.0")
- HA modify: `custom_components/zemismart_blinds/manifest.json` (version → `0.8.0`), `CHANGELOG.md`, and the README section that documents the `/tx` contract (add `boot`, note firmware ≥ 1.4.0 enforces it and ≤ 1.3.0 ignores it — deploy the integration first)

**Interfaces:** none new — prose only, but the lockstep-order sentence (integration first, firmware second) must appear in BOTH changelogs.

- [ ] **Step 1: FW docs edit** — make the README/CHANGELOG edits above.
- [ ] **Step 2: FW verify + commit** — `uv run pytest -q` (README-contract tests exist in this repo and may pin wording — fix honestly, never by weakening), then `git add README.md CHANGELOG.md && git commit -m "docs: contract v3, inbound cap, and OTA wait-for-idle for v1.4.0"`.
- [ ] **Step 3: HA docs edit** — manifest 0.8.0 + CHANGELOG + README contract section.
- [ ] **Step 4: HA verify + commit** — `uv run pytest -q`, then `git add -A && git commit -m "docs: contract v3 boot stamping; v0.8.0"`.

---

### Task 8: Push branches and open the two PRs (both repos)

- [ ] **Step 1: FW PR**

```bash
cd /Users/bryanli/Projects/joyfulhouse/homeassistant-dev/esphome-rf433-mqtt-bridge
git push -u origin batch-b-safety
gh pr create --title "v1.4.0: Batch B safety hardening — inbound cap, OTA wait-for-idle, boot binding" --body "Implements docs/design/2026-08-02-batch-b-safety-design.md.

Closes #<cap>. Closes #<ota>. Closes #<boot>.

Lockstep: requires zemismart-blinds >= 0.8.0 (deploys FIRST; older firmware ignores the stamped boot field).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: HA PR**

```bash
cd /Users/bryanli/Projects/joyfulhouse/homeassistant-dev/zemismart-blinds
git push -u origin contract-v3-boot-binding
gh pr create --title "v0.8.0: contract v3 — stamp bridge boot into /tx" --body "Consumer half of esphome-rf433-mqtt-bridge Batch B (see its design doc). Safe against <= v1.3.0 firmware (unknown keys ignored); REQUIRED by >= v1.4.0. Deploys before the firmware.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 3: Stop.** Do not merge, tag, or flash — review, canary, and fleet rollout are user-gated (Task 9 prepares the runbook).

---

### Task 9: Canary runbook (private repo — real names allowed here only)

**Files:**
- Create: `/Users/bryanli/Projects/joyfulhouse/homeassistant-dev/zemismart-private/docs/2026-08-02-batch-b-canary-runbook.md`

- [ ] **Step 1: Write the runbook** — concrete steps for the office-bridge canary per spec §4, with real bridge/broker names and a bogus-identity target (per the established probe technique) so nothing physically moves:
  1. Deploy blinds v0.8.0 fleet-wide first; confirm live moves still work against v1.3.0 firmware (boot ignored).
  2. Bump `ESPHOME_VERSION` 2026.7.2 → 2026.7.3 in the Forgejo esphome-config CI (config source of truth, GitOps to `/config/esphome`) in the same change that syncs the v1.4.0 sources; then OTA the office bridge to the v1.4.0 candidate during an idle window.
  3. Cap probe: `mosquitto_pub` a `/tx` whose payload pads past 20 KB → expect a single throttled drop log, bridge stays online, no status.
  4. OTA-interlock probe: start a bogus-identity timed move, immediately re-OTA → expect the transfer to wait, the STOP to fire at its natural deadline, update completes.
  5. Boot probe: publish a bogus `/tx` with `boot: 1` → expect `rejected/boot_mismatch` on `/status`. Then publish a **retained** bogus `/tx` with the CURRENT boot, reboot the bridge, confirm the replay is rejected (new boot) and nothing schedules; clear the retained message with `mosquitto_pub -r -n`.
  6. Fleet OTA the remaining six, soak overnight, then tag v1.4.0 + GH release; announcement follows.
- [ ] **Step 2: Commit it to the private repo.**

---

## Self-review notes (already applied)

- Spec §1→Tasks 2-3, §2→Task 4, §3→Tasks 5-6, §4→Task 9 + per-task compile/test gates, "Public tracking"→Task 1, docs contract updates→Task 7. No gaps.
- The one test embedding the /tx lambda and the extraction-marker change for the OTA test are called out explicitly (both would otherwise fail mysteriously).
- Constants appear with identical values in spec, tests, and implementation code.
