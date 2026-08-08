"""Native scheduler and ESPHome package contract tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SCHEDULER_HEADER = PROJECT_ROOT / "rf433_scheduler.h"
BRIDGE_YAML = PROJECT_ROOT / "rf433-mqtt-bridge.yaml"


def _firmware_lambda(section_start: str, section_end: str) -> str:
    """Extract a shipped ESPHome lambda for host execution."""
    package = BRIDGE_YAML.read_text()
    section = package.split(section_start, maxsplit=1)[1].split(section_end, maxsplit=1)[0]
    body = section.split("- lambda: |-", maxsplit=1)[1]
    substitutions = {
        "${bridge_id}": "test-bridge",
        "${bridge_area}": "test-area",
        "${default_bridge}": "false",
        "${default_repeats}": "5",
        "${hardware_variant}": "test-hw",
        "${listen_enabled}": "false",
        "${repeat_gap_ms}": "35",
    }
    for key, value in substitutions.items():
        body = body.replace(key, value)
    return textwrap.dedent(body).strip()


def test_native_scheduler_keeps_per_target_timed_stops_and_frame_order(tmp_path: Path) -> None:
    """Exercise the firmware's actual C++ scheduler and raw validator on the host compiler."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_test.cpp"
    binary = tmp_path / "scheduler_test"
    source.write_text(
        r"""
#include <cassert>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

// Test-only predicate: exercise the parser through its bool result.
static bool valid_target_key(const std::string &value) {
  std::string identity;
  uint16_t mask = 0;
  return rf433::parse_target(value, identity, mask);
}

int main() {
  const std::string frame =
      "AAB04D04081414026C01181414381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
      "192929292A192A1A1A1A1A1A19292A1A1A1A1A1A1A1A1A1A1A1A192A1929292A1A19292A1A1A1A1955";
  std::string normalized;
  std::string reason;
  assert(rf433::normalize_b0(frame, normalized, reason));
  assert(normalized == frame);
  assert(!rf433::normalize_b0("AAB0GG55", normalized, reason));
  assert(!rf433::normalize_b0("AAB0010055", normalized, reason));
  assert(!rf433::normalize_b0("AAB005010000011155", normalized, reason));
  // A whitespace-padded input beyond MAX_B0_INPUT_CHARS is rejected BEFORE its
  // length is reserved, so a hostile /tx field cannot force a large transient
  // heap allocation on the ESP8285.
  assert(!rf433::normalize_b0(std::string(rf433::MAX_B0_INPUT_CHARS + 1, ' '), normalized, reason));
  assert(reason == "frame exceeds maximum size");

  // The Portisch per-packet hardware repeat byte (hex chars 8..9) is validated:
  // force the valid frame's 08 repeat byte to FF and it is rejected.
  std::string ff_frame = frame;
  ff_frame[8] = 'F';
  ff_frame[9] = 'F';
  assert(!rf433::normalize_b0(ff_frame, normalized, reason));
  assert(reason == "frame embedded repeat count out of range");

  // Requested airtime is bounded: one maximum-duration bucket (0xFFFF us),
  // two pulses, and the maximum embedded repeat of 16 request ~2.1 s of
  // exclusive coprocessor time -- just over the 2 s ceiling -- and are
  // rejected even though the frame is structurally valid.
  assert(!rf433::normalize_b0("AAB0050110FFFF0855", normalized, reason));
  assert(reason == "frame requested airtime exceeds limit");
  // The same frame at the controller's embedded repeat of 8 (~1 s) passes.
  assert(rf433::normalize_b0("AAB0050108FFFF0855", normalized, reason));

  assert(valid_target_key("a1b2c3:42:1,2,16"));
  assert(!valid_target_key("target-a"));
  assert(!valid_target_key("a1b2c3:42:2,1"));
  assert(!valid_target_key("a1b2c3:42:0"));
  assert(!valid_target_key("a1b2c3:42:17"));
  // A long digit run is rejected as the channel accumulator crosses 16, before
  // it can overflow the signed int (would otherwise be undefined behavior).
  assert(!valid_target_key("a1b2c3:42:99999999999999999999"));

  TargetScheduler scheduler(35);
  const std::string target_a = "a1b2c3:42:1";
  const std::string target_b = "a1b2c3:42:2";
  const std::string target_c = "a1b2c3:42:3";
  const std::string target_d = "a1b2c3:42:4";
  std::string started;
  std::vector<std::string> displaced;
  assert(scheduler.schedule("command-a", target_a, "A", "TA", 1, 100, "SA", 100,
                            displaced, reason));
  assert(displaced.empty());
  auto raw = scheduler.next(100, started);
  assert(raw && *raw == "A");
  assert(started == "command-a");

  // Commands for other, non-overlapping targets never displace A's timed STOP.
  assert(scheduler.schedule("command-b", target_b, "B", "", 1, 0, "", 105, displaced, reason));
  assert(displaced.empty());
  raw = scheduler.next(135, started);
  assert(raw && *raw == "TA");
  assert(started.empty());
  raw = scheduler.next(170, started);
  assert(raw && *raw == "B");
  assert(started == "command-b");
  assert(scheduler.schedule("command-c", target_c, "C", "", 1, 0, "", 175, displaced, reason));
  assert(!scheduler.next(199, started));
  raw = scheduler.next(205, started);
  assert(raw && *raw == "SA");
  assert(started.empty());
  raw = scheduler.next(240, started);
  assert(raw && *raw == "C");
  assert(started == "command-c");

  // A due fail-safe STOP preempts unfinished action/trailer repeats.
  assert(scheduler.schedule("command-d", target_d, "D", "TD", 2, 10, "SD", 245,
                            displaced, reason));
  raw = scheduler.next(275, started);
  assert(raw && *raw == "D");
  assert(started == "command-d");
  raw = scheduler.next(310, started);
  assert(raw && *raw == "SD");
  assert(started.empty());
  raw = scheduler.next(345, started);
  assert(raw && *raw == "SD");
  assert(started.empty());

  // Latest command wins: an overlapping target displaces a STARTED timed
  // command, and ALL 'repeats' copies of its fail-safe STOP are flushed on air
  // (one per pacing gap) before the replacement's first dispatch.
  assert(scheduler.schedule("command-d2", target_d, "D2", "", 5, 4000, "SD2", 380,
                            displaced, reason));
  assert(displaced.empty());
  raw = scheduler.next(380, started);
  assert(raw && *raw == "D2");
  assert(started == "command-d2");
  assert(scheduler.schedule("command-e", "A1B2C3:42:4,5", "E", "", 1, 0, "", 400,
                            displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "command-d2");
  for (int index = 0; index < 5; index++) {
    raw = scheduler.next(415 + index * 35, started);
    assert(raw && *raw == "SD2");  // repeats (=5) flushed fail-safe STOP copies
    assert(started.empty());
  }
  raw = scheduler.next(590, started);
  assert(raw && *raw == "E");
  assert(started == "command-e");

  // Duplicate command_id (QoS-1 redelivery / retained replay) is rejected.
  assert(!scheduler.schedule("command-e", "a1b2c3:42:6", "X", "", 1, 0, "", 595,
                             displaced, reason));
  assert(reason == "duplicate command_id");

  // Frame storage budget rejects heap-exhausting admission (distinct remote
  // IDs so the targets never overlap and MAX_TARGETS is not the limiter).
  const std::string big(510, 'A');
  bool budget_hit = false;
  for (int index = 0; index < 14; index++) {
    const std::string target = "a1b2c3:" + std::to_string(50 + index) + ":1";
    if (!scheduler.schedule("big-" + std::to_string(index), target, big, big, 1, 3600000, big,
                            static_cast<uint32_t>(470 + index), displaced, reason)) {
      assert(reason == "scheduler frame storage budget exceeded" ||
             reason == "target scheduler is full");
      budget_hit = true;
      break;
    }
  }
  assert(budget_hit);

  // Post-drain spacing is preserved: after a frame dispatches and the queue
  // drains, a command arriving within the pacing gap still waits out the gap
  // owed to the just-sent frame (the gate is no longer reset on drain).
  TargetScheduler idle_scheduler(35);
  assert(idle_scheduler.schedule("wrap-1", target_a, "W1", "", 1, 0, "", 35,
                                 displaced, reason));
  raw = idle_scheduler.next(35, started);
  assert(raw && *raw == "W1");  // pacing gate now owes until 70
  assert(idle_scheduler.schedule("wrap-1b", target_a, "W1B", "", 1, 0, "", 60,
                                 displaced, reason));
  assert(!idle_scheduler.next(60, started));  // gap owed to W1 not yet elapsed
  raw = idle_scheduler.next(70, started);
  assert(raw && *raw == "W1B");
  assert(started == "wrap-1b");  // gate owes until 105

  // Idle-rollover regression: a single idle tick more than 60s after the last
  // dispatch resets the stale gate (the 5ms interval guarantees such a tick
  // before now-gate could wrap negative at 2^31 ms). A command admitted after
  // the reset -- even across the signed-uint32 wrap -- still transmits.
  assert(!idle_scheduler.next(105u + 60001u, started));  // >60s idle -> gate reset
  const uint32_t after_idle = 105u + 2147500000u;  // > 2^31 ms later, wrapped domain
  assert(idle_scheduler.schedule("wrap-2", target_a, "W2", "", 1, 0, "", after_idle,
                                 displaced, reason));
  raw = idle_scheduler.next(after_idle, started);
  assert(raw && *raw == "W2");
  assert(started == "wrap-2");

  // A STOP displaced mid-dispatch (phase STOP, remaining > 0) flushes exactly
  // its remaining copies -- not the full repeat count, and never zero.
  TargetScheduler stop_mid(35);
  assert(stop_mid.schedule("mid-1", "aabbcc:11:1", "M", "", 3, 10, "SM", 0,
                           displaced, reason));
  raw = stop_mid.next(0, started);   // M action; deadline armed at 10
  assert(raw && *raw == "M");
  assert(started == "mid-1");
  raw = stop_mid.next(35, started);  // deadline due: phase->STOP, dispatch SM (2 left)
  assert(raw && *raw == "SM");
  assert(started.empty());
  raw = stop_mid.next(70, started);  // SM again (1 left)
  assert(raw && *raw == "SM");
  assert(stop_mid.schedule("mid-2", "aabbcc:11:1", "M2", "", 1, 0, "", 105,
                           displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "mid-1");
  raw = stop_mid.next(105, started);  // exactly one remaining STOP flushed
  assert(raw && *raw == "SM");
  assert(started.empty());
  raw = stop_mid.next(140, started);
  assert(raw && *raw == "M2");        // not another SM: only 'remaining' (=1) was flushed
  assert(started == "mid-2");
  assert(!stop_mid.next(175, started));

  // Worst case: a STOP marked due but displaced before its FIRST dispatch (a
  // sibling STOP won the tick) must still flush its owed copy -- never zero.
  TargetScheduler zero_win(35);
  assert(zero_win.schedule("zw-p", "aabbcc:11:1", "P", "", 1, 1000, "SP", 0,
                           displaced, reason));
  assert(zero_win.schedule("zw-q", "aabbcc:11:2", "Q", "", 1, 1000, "SQ", 0,
                           displaced, reason));
  raw = zero_win.next(0, started);   // P action, deadline 1000
  assert(raw && *raw == "P");
  raw = zero_win.next(35, started);  // Q action, deadline 1035
  assert(raw && *raw == "Q");
  raw = zero_win.next(1035, started);  // both deadlines due; P's STOP wins the tick
  assert(raw && *raw == "SP");
  assert(started.empty());
  // Q is now phase STOP with zero STOP frames dispatched. Displace it.
  assert(zero_win.schedule("zw-r", "aabbcc:11:2", "R", "", 1, 0, "", 1040,
                           displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "zw-q");
  raw = zero_win.next(1070, started);
  assert(raw && *raw == "SQ");  // owed STOP flushed despite zero pre-displacement sends
  assert(started.empty());

  // The admission budget counts bytes parked in flush_stops_, not just
  // commands_. Each flush entry stores its frame ONCE with a send count, so
  // displacing a repeats=20 timed command charges 510 bytes -- not 10200.
  TargetScheduler budget2(35);
  const std::string half(510, 'A');
  assert(budget2.schedule("bx", "d1d1d1:05:1,2", half, "", 20, 3600000, half, 0,
                          displaced, reason));
  raw = budget2.next(0, started);  // start bx so its fail-safe STOP is owed
  assert(raw && *raw == half);
  assert(started == "bx");
  // Fill to 10 * 1530 = 15300 retained bytes alongside bx's 1020.
  for (int index = 0; index < 10; index++) {
    const std::string target = "d1d1d1:" + std::to_string(50 + index) + ":1";
    assert(budget2.schedule("bfill-" + std::to_string(index), target, half, half, 1,
                            3600000, half, static_cast<uint32_t>(1 + index),
                            displaced, reason));
  }
  // Displace bx: its 510-byte STOP is parked for flush (once, remaining=20).
  assert(budget2.schedule("by", "d1d1d1:05:2", "Y", "", 1, 0, "", 15,
                          displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "bx");
  // bz fits against commands_ alone (15301 + 1000 = 16301 <= 16384) but not
  // once the 510 parked flush bytes are charged -> rejected.
  const std::string kilo(1000, 'B');
  assert(!budget2.schedule("bz", "d1d1d1:61:1", kilo, "", 1, 0, "", 16,
                           displaced, reason));
  assert(reason == "scheduler frame storage budget exceeded");
  // A smaller command clears the budget with the flush bytes still parked.
  const std::string mid(500, 'C');
  assert(budget2.schedule("bz2", "d1d1d1:61:1", mid, "", 1, 0, "", 17,
                          displaced, reason));
  // All 20 owed STOP copies still go on air, one per pacing gap.
  for (int index = 0; index < 20; index++) {
    raw = budget2.next(static_cast<uint32_t>(35 + index * 35), started);
    assert(raw && *raw == half);
    assert(started.empty());
  }

  // Admission also charges the flush bytes the CURRENT displacement creates:
  // bw fits against the retained commands alone (13770 + 2500 = 16270) but not
  // once the displaced command's owed 510-byte STOP is counted -> rejected,
  // and the displaced command stays scheduled.
  TargetScheduler budget3(35);
  assert(budget3.schedule("cx", "e1e1e1:05:1", half, "", 20, 3600000, half, 0,
                          displaced, reason));
  raw = budget3.next(0, started);
  assert(raw && *raw == half);
  assert(started == "cx");
  for (int index = 0; index < 9; index++) {
    const std::string target = "e1e1e1:" + std::to_string(50 + index) + ":1";
    assert(budget3.schedule("cfill-" + std::to_string(index), target, half, half, 1,
                            3600000, half, static_cast<uint32_t>(1 + index),
                            displaced, reason));
  }
  const std::string big25(2500, 'D');
  assert(!budget3.schedule("cw", "e1e1e1:05:1", big25, "", 1, 0, "", 10,
                           displaced, reason));
  assert(reason == "scheduler frame storage budget exceeded");
  assert(displaced.empty());
  // The same displacement with a smaller replacement is admitted.
  const std::string big20(2000, 'E');
  assert(budget3.schedule("cw2", "e1e1e1:05:1", big20, "", 1, 0, "", 11,
                          displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "cx");

  // Displaced-STOP fairness: two displaced timed commands' owed STOPs rotate,
  // so the second motor's FIRST stop lands within two pacing gaps instead of
  // waiting out the first motor's whole repeat train.
  TargetScheduler fair(35);
  assert(fair.schedule("fa", "aabbcc:22:1", "FA", "", 3, 60000, "S1", 0,
                       displaced, reason));
  assert(fair.schedule("fb", "aabbcc:22:2", "FB", "", 3, 60000, "S2", 0,
                       displaced, reason));
  raw = fair.next(0, started);
  assert(raw && *raw == "FA");
  raw = fair.next(35, started);
  assert(raw && *raw == "FB");
  assert(fair.schedule("fc", "aabbcc:22:1,2", "FC", "", 1, 0, "", 70,
                       displaced, reason));
  assert(displaced.size() == 2);
  const char *fair_expected[] = {"S1", "S2", "S1", "S2", "S1", "S2", "FC"};
  for (int index = 0; index < 7; index++) {
    raw = fair.next(static_cast<uint32_t>(70 + 35 * index), started);
    assert(raw && *raw == fair_expected[index]);
  }

  // Duplicate-redelivery lifecycle memory: admitted commands are remembered
  // with their RF-start state and timestamp so a QoS-1 replay can answer
  // idempotently and report how old the original start is.
  uint32_t age = 0;
  // fa and fb were displaced by fc: their memory replays "displaced", never
  // "accepted" (a controller must not rebuild a retired motion).
  assert(fair.replay_state("fa", 500, age) == 4);
  assert(fair.replay_state("fb", 500, age) == 4);
  assert(fair.replay_state("fc", 500, age) == 2);
  assert(fair.replay_state("unknown-id", 500, age) == 0);
  TargetScheduler rep(35);
  assert(rep.schedule("r1", "aabbcc:33:1", "R1", "", 1, 0, "", 0, displaced, reason));
  assert(rep.replay_state("r1", 10, age) == 1);  // admitted, RF not yet started
  assert(age == 0);
  raw = rep.next(0, started);
  assert(raw && *raw == "R1" && started == "r1");
  assert(rep.replay_state("r1", 4000, age) == 2);  // admitted and started
  assert(age == 4000);

  // State-dependent rejections (scheduler full / storage budget) are also
  // remembered, so a redelivery after capacity drains is NOT silently
  // admitted. bz was budget-rejected in the flush-accounting block above.
  assert(budget2.replay_state("bz", 100, age) == 3);

  // Live scheduler state is authoritative for replay and survives ring
  // churn: an active timed command keeps answering "started" (never 0)
  // even after enough admissions/rejections to sweep the whole dedup ring,
  // so a QoS-1 redelivery can never re-run it. (This closes the ring-
  // eviction re-run hole for a still-scheduled command.)
  TargetScheduler live(35);
  assert(live.schedule("live-1", "c0ffee:01:1", "L", "", 1, 3600000, "SL", 0,
                       displaced, reason));
  raw = live.next(0, started);
  assert(raw && *raw == "L" && started == "live-1");
  assert(live.replay_state("live-1", 100, age) == 2);  // started, from commands_
  // 80 distinct valid admissions/rejections sweep the whole 64-slot ring
  // (each varies the 6-hex prefix, so the targets are structurally valid;
  // once the scheduler is full the surplus become remembered state-3
  // rejections). 80 > COMMAND_ID_RING_SIZE, so live-1's own ring slot is
  // evicted mid-churn, yet live-1 stays active (answered from commands_)
  // throughout — proving live state, not the ring, gates the redelivery.
  const char *hexits = "0123456789abcdef";
  for (int index = 0; index < 80; index++) {
    std::string prefix = "c0ff";
    prefix.push_back(hexits[(index >> 4) & 0xF]);
    prefix.push_back(hexits[index & 0xF]);
    const std::string target = prefix + ":02:1";
    live.schedule("churn-" + std::to_string(index), target, "C", "", 1, 0, "",
                  static_cast<uint32_t>(100 + index), displaced, reason);
  }
  assert(live.replay_state("live-1", 5000, age) == 2);  // still started
  assert(age == 5000);
  // A redelivery of the still-active command is rejected as a duplicate,
  // never re-admitted (and re-run), regardless of ring occupancy.
  assert(!live.schedule("live-1", "c0ffee:01:1", "L", "", 1, 3600000, "SL", 5000,
                        displaced, reason));
  assert(reason == "duplicate command_id");

  // A due scheduled fail-safe STOP alternates with flushed displaced STOPs
  // instead of waiting behind the entire flush queue.
  TargetScheduler alt(35);
  assert(alt.schedule("g1", "aabbcc:44:1", "G1", "", 4, 50, "SG1", 0,
                      displaced, reason));
  assert(alt.schedule("g2", "aabbcc:44:2", "G2", "", 4, 10000, "SG2", 0,
                      displaced, reason));
  raw = alt.next(0, started);
  assert(raw && *raw == "G1");  // deadline armed at 50
  raw = alt.next(35, started);
  assert(raw && *raw == "G2");
  assert(alt.schedule("g3", "aabbcc:44:2", "G3", "", 1, 0, "", 40,
                      displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "g2");
  raw = alt.next(70, started);
  assert(raw && *raw == "SG2");  // flush frame first
  raw = alt.next(105, started);
  assert(raw && *raw == "SG1");  // due scheduled STOP takes the next tick
  raw = alt.next(140, started);
  assert(raw && *raw == "SG2");  // back to the flush queue
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


def test_native_scheduler_idle_air_clear_and_atomic_disarm(tmp_path: Path) -> None:
    """Exercise idle-listen gates and physical-takeover cancellation."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_state_sync_test.cpp"
    binary = tmp_path / "scheduler_state_sync_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  const std::string target_a = "a1b2c3:42:1";
  const std::string target_b = "a1b2c3:42:2";
  std::string started;
  std::string reason;
  std::vector<std::string> displaced;

  // A fresh scheduler is logically idle and physically air-clear. A command
  // makes it non-idle until its final frame is handed off.
  TargetScheduler idle_scheduler(35);
  assert(idle_scheduler.idle());
  assert(idle_scheduler.rf_air_clear(0));
  assert(idle_scheduler.schedule("idle-command", target_a, "A", "", 1, 0, "", 0,
                                 displaced, reason));
  assert(!idle_scheduler.idle());
  auto raw = idle_scheduler.next(0, started);
  assert(raw && *raw == "A");
  assert(idle_scheduler.idle());

  // A displaced fail-safe STOP alone also keeps the scheduler non-idle.
  TargetScheduler flush_idle(35);
  assert(flush_idle.schedule("flush-original", target_a, "A", "", 1, 1000, "SA", 0,
                             displaced, reason));
  raw = flush_idle.next(0, started);
  assert(raw && *raw == "A");
  assert(flush_idle.schedule("flush-replacement", target_a, "B", "", 1, 0, "", 1,
                             displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "flush-original");
  flush_idle.disarm("flush-replacement");
  assert(!flush_idle.idle());
  raw = flush_idle.next(35, started);
  assert(raw && *raw == "SA");
  assert(flush_idle.idle());

  // This existing synthetic B0 fixture has 1,048,560 us of on-air time:
  // two 0xFFFF-us pulses repeated eight times. The EFM8 keys RF only after
  // the full command arrives over UART, so the busy window is the frame's
  // 5 ms serialization (18 hex chars at 19200 baud, ceiled) plus 1,049 ms
  // of air plus the 5 ms handoff margin: air-clear 1,059 ms after dispatch.
  const std::string airtime_frame = "AAB0050108FFFF0855";
  TargetScheduler air_scheduler(35);
  assert(air_scheduler.rf_air_clear(100));
  assert(air_scheduler.schedule("air-command", target_a, airtime_frame, "", 1, 0, "", 100,
                                displaced, reason));
  raw = air_scheduler.next(100, started);
  assert(raw && *raw == airtime_frame);
  assert(!air_scheduler.rf_air_clear(100));
  assert(!air_scheduler.rf_air_clear(1158));
  assert(air_scheduler.rf_air_clear(1159));

  // The same signed serial-number comparison remains correct when the busy
  // deadline wraps through UINT32_MAX.
  TargetScheduler wrap_scheduler(35);
  const uint32_t wrap_start = std::numeric_limits<uint32_t>::max() - 500U;
  const uint32_t wrap_clear_at = wrap_start + 1059U;
  assert(wrap_clear_at == 558U);
  assert(wrap_scheduler.rf_air_clear(wrap_start));
  assert(wrap_scheduler.schedule("wrap-air", target_b, airtime_frame, "", 1, 0, "",
                                 wrap_start, displaced, reason));
  raw = wrap_scheduler.next(wrap_start, started);
  assert(raw && *raw == airtime_frame);
  assert(!wrap_scheduler.rf_air_clear(wrap_start));
  assert(!wrap_scheduler.rf_air_clear(wrap_clear_at - 1U));
  assert(wrap_scheduler.rf_air_clear(wrap_clear_at));

  // Disarming a live command atomically removes its remaining ACTION,
  // TRAILER, and fail-safe STOP frames without disturbing a concurrent target.
  TargetScheduler concurrent(35);
  assert(concurrent.schedule("command-a", target_a, "A", "TA", 2, 100, "SA", 0,
                             displaced, reason));
  assert(concurrent.schedule("command-b", target_b, "B", "", 2, 0, "", 0,
                             displaced, reason));
  raw = concurrent.next(0, started);
  assert(raw && *raw == "A" && started == "command-a");
  raw = concurrent.next(35, started);
  assert(raw && *raw == "B" && started == "command-b");
  concurrent.disarm("command-a");
  uint32_t age = 0;
  assert(concurrent.replay_state("command-a", 40, age) == 4);
  raw = concurrent.next(70, started);
  assert(raw && *raw == "B" && started.empty());
  assert(!concurrent.next(105, started));
  assert(!concurrent.next(1000, started));
  assert(concurrent.idle());

  // Disarming a displaced command purges every owed STOP copy parked in the
  // flush queue, allowing its replacement to run normally.
  TargetScheduler purge(35);
  assert(purge.schedule("purge-original", target_a, "A", "", 3, 1000, "SA", 0,
                        displaced, reason));
  raw = purge.next(0, started);
  assert(raw && *raw == "A");
  assert(purge.schedule("purge-replacement", target_a, "B", "", 1, 0, "", 1,
                        displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "purge-original");
  purge.disarm("purge-original");
  purge.disarm("purge-original");
  assert(purge.replay_state("purge-original", 2, age) == 4);
  raw = purge.next(35, started);
  assert(raw && *raw == "B" && started == "purge-replacement");
  assert(!purge.next(70, started));

  // A disarm may arrive before its /tx. The unconditional terminal tombstone
  // makes repeated disarms emission-free and forces the reordered original
  // command to resolve as the existing terminal/displaced no-op state.
  TargetScheduler reordered(35);
  reordered.disarm("reordered-command");
  reordered.disarm("reordered-command");
  assert(reordered.idle());
  assert(!reordered.next(0, started));
  assert(reordered.replay_state("reordered-command", 0, age) == 4);
  assert(!reordered.schedule("reordered-command", target_a, "A", "", 1, 1000, "SA", 0,
                             displaced, reason));
  assert(reason == "duplicate command_id");
  assert(reordered.replay_state("reordered-command", 0, age) == 4);
  assert(!reordered.next(1000, started));
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


def test_native_scheduler_paces_dispatch_by_frame_airtime(tmp_path: Path) -> None:
    """Frames never hand off while the EFM8BB1 is still transmitting.

    The coprocessor transmits a B0 frame blocking (embedded repeats included)
    with only a ~64-byte UART ring; a frame dispatched during that window is
    corrupted in the ring instead of paced (observed in the field: 10 rapid
    dispatches produced a single completion ACK). The coprocessor keys RF only
    after the full command arrives over the 19200-baud link, so the global
    pacing gate must wait max(repeat_gap, serialization + airtime + margin)
    after every dispatch, computed from the same admission-time airtime used
    by rf_air_clear.
    """
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_airtime_test.cpp"
    binary = tmp_path / "scheduler_airtime_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  // A real AOK-length frame with the production embedded repeat of 8: its
  // airtime dominates the 35 ms repeat gap by more than an order of magnitude.
  const std::string frame =
      "AAB04D04081414026C0118141438192A192A1A1A19292A192A192A1A192A192A1A1A1A1A19292A1A"
      "192A1A192A192A1A1A1A1A1A1A192A1A1A1A1A1A1A1A1A1A192A1A1A1929292A192A192929292A1955";
  std::string normalized;
  std::string reason;
  uint64_t airtime_us = 0;
  assert(rf433::normalize_b0_with_airtime(frame, normalized, reason, airtime_us));
  const uint32_t airtime_ms = static_cast<uint32_t>((airtime_us + 999U) / 1000U);
  assert(airtime_ms > 100);  // realistic burst: far above the pacing gap
  // UART serialization of the frame at 19200 baud (hex_chars/2 bytes x 10
  // bits, ceiled to ms) -- the scheduler charges it because RF only starts
  // once the trailer byte arrives at the coprocessor.
  const uint32_t uart_ms =
      static_cast<uint32_t>((frame.size() * 5000ULL + 19199ULL) / 19200ULL);

  std::string started;
  std::string reason2;
  std::vector<std::string> displaced;
  TargetScheduler scheduler(35);
  assert(scheduler.schedule("cmd-air", "a1b2c3:42:1", frame, "", 3, 0, "", 1000,
                            displaced, reason2));
  auto raw = scheduler.next(1000, started);
  assert(raw && *raw == frame);
  assert(started == "cmd-air");

  // The old gap-only gate would re-dispatch at +35 ms, mid-transmission.
  assert(!scheduler.next(1035, started));
  assert(!scheduler.next(1000 + airtime_ms, started));
  // One margin tick after serialization + air clears, the second repeat goes.
  const uint32_t clear_at = 1000 + uart_ms + airtime_ms + 5;
  assert(!scheduler.next(clear_at - 1, started));
  raw = scheduler.next(clear_at, started);
  assert(raw && *raw == frame);
  assert(started.empty());

  // Same pacing applies between the final repeats.
  assert(!scheduler.next(clear_at + 35, started));
  raw = scheduler.next(clear_at + uart_ms + airtime_ms + 5, started);
  assert(raw && *raw == frame);
  assert(scheduler.idle());

  // Tiny/zero-airtime frames (host tests, degenerate raws) keep gap pacing.
  TargetScheduler gap_scheduler(35);
  assert(gap_scheduler.schedule("cmd-gap", "a1b2c3:42:2", "A", "", 2, 0, "", 0,
                                displaced, reason2));
  raw = gap_scheduler.next(0, started);
  assert(raw && *raw == "A");
  assert(!gap_scheduler.next(34, started));
  raw = gap_scheduler.next(35, started);
  assert(raw && *raw == "A");
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


def test_native_scheduler_bounds_gap_and_stops_override_user_pacing(tmp_path: Path) -> None:
    """Invalid user gaps are bounded and cannot postpone a physically-clear STOP."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_gap_safety_test.cpp"
    binary = tmp_path / "scheduler_gap_safety_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  std::string started;
  std::string reason;
  std::vector<std::string> displaced;

  // A due STOP bypasses the 60-second user floor as soon as the 5 ms physical
  // occupancy is clear.
  TargetScheduler due_stop(60000);
  assert(due_stop.schedule("due", "a1b2c3:04:1", "ACTION", "", 1, 10, "STOP", 0,
                           displaced, reason));
  auto raw = due_stop.next(0, started);
  assert(raw && *raw == "ACTION");
  assert(!due_stop.next(4, started));
  raw = due_stop.next(10, started);
  assert(raw && *raw == "STOP");

  // A negative substitution must not survive conversion as a huge uint32_t.
  // Clamping it to zero leaves only the 5 ms physical safety margin.
  TargetScheduler negative_gap(-35);
  assert(negative_gap.schedule("negative", "a1b2c3:01:1", "N", "", 2, 0, "", 0,
                               displaced, reason));
  raw = negative_gap.next(0, started);
  assert(raw && *raw == "N");
  assert(!negative_gap.next(4, started));
  raw = negative_gap.next(5, started);
  assert(raw && *raw == "N");

  // An oversized preference is clamped to 60 seconds, still far inside the
  // signed serial-arithmetic horizon.
  TargetScheduler oversized_gap(std::numeric_limits<int32_t>::max());
  assert(oversized_gap.schedule("oversized", "a1b2c3:02:1", "O", "", 2, 0, "", 100,
                                displaced, reason));
  raw = oversized_gap.next(100, started);
  assert(raw && *raw == "O");
  assert(!oversized_gap.next(60099, started));
  raw = oversized_gap.next(60100, started);
  assert(raw && *raw == "O");

  // The maximum bounded gap remains correct when its deadline wraps uint32_t.
  TargetScheduler rollover_gap(std::numeric_limits<int32_t>::max());
  const uint32_t wrap_start = std::numeric_limits<uint32_t>::max() - 30000U;
  const uint32_t wrap_due = wrap_start + 60000U;
  assert(wrap_due == 29999U);
  assert(rollover_gap.schedule("rollover", "a1b2c3:03:1", "R", "", 2, 0, "",
                               wrap_start, displaced, reason));
  raw = rollover_gap.next(wrap_start, started);
  assert(raw && *raw == "R");
  assert(!rollover_gap.next(wrap_due - 1U, started));
  raw = rollover_gap.next(wrap_due, started);
  assert(raw && *raw == "R");

  // A displaced STOP has the same safety priority; the replacement ACTION
  // remains discretionary and continues to honor the long user floor.
  TargetScheduler displaced_stop(60000);
  assert(displaced_stop.schedule("old", "a1b2c3:05:1", "OLD", "", 1, 1000, "SAFE", 0,
                                 displaced, reason));
  raw = displaced_stop.next(0, started);
  assert(raw && *raw == "OLD");
  assert(displaced_stop.schedule("new", "a1b2c3:05:1", "NEW", "", 1, 0, "", 1,
                                 displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "old");
  assert(!displaced_stop.next(4, started));
  raw = displaced_stop.next(5, started);
  assert(raw && *raw == "SAFE");
  assert(!displaced_stop.next(60004, started));
  raw = displaced_stop.next(60005, started);
  assert(raw && *raw == "NEW");
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
    subprocess.run([str(binary)], check=True, text=True)


def test_native_scheduler_rejects_first_stop_occupancy_over_budget(tmp_path: Path) -> None:
    """Admission bounds aggregate one-copy fail-safe STOP occupancy."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_stop_budget_test.cpp"
    binary = tmp_path / "scheduler_stop_budget_test"
    source.write_text(
        r"""
#include <cassert>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  // This valid B0 frame occupies 1,059 ms including UART serialization and
  // scheduler margin. Three first STOP copies fit the 4,000 ms safety budget;
  // the fourth would raise aggregate occupancy to 4,236 ms and is rejected.
  const std::string slow_stop = "AAB0050108FFFF0855";
  TargetScheduler scheduler(0);
  std::string reason;
  std::vector<std::string> displaced;
  for (int index = 0; index < 3; index++) {
    const std::string target = "a1b2c3:0" + std::to_string(index + 1) + ":1";
    assert(scheduler.schedule("timed-" + std::to_string(index), target, "A", "", 1,
                              60000, slow_stop, 0, displaced, reason));
  }
  assert(!scheduler.schedule("timed-3", "a1b2c3:04:1", "A", "", 1, 60000,
                             slow_stop, 0, displaced, reason));
  assert(reason == "first-STOP safety budget exceeded");
  uint32_t age = 0;
  assert(scheduler.replay_state("timed-3", 0, age) == 3);
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


def test_native_scheduler_ota_drain_emits_armed_stops_once_and_clears(
    tmp_path: Path,
) -> None:
    """OTA draining snapshots only armed STOPs and leaves no scheduled work."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "scheduler_ota_drain_test.cpp"
    binary = tmp_path / "scheduler_ota_drain_test"
    source.write_text(
        r"""
#include <cassert>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  TargetScheduler scheduler(60000);
  std::string started;
  std::string reason;
  std::vector<std::string> displaced;

  assert(scheduler.schedule("armed", "a1b2c3:01:1", "A", "", 5, 60000, "SA", 0,
                            displaced, reason));
  auto raw = scheduler.next(0, started);
  assert(raw && *raw == "A" && started == "armed");
  // This timed command never reached its first ACTION handoff, so its STOP is
  // not armed and OTA must not synthesize motion by transmitting anything.
  assert(scheduler.schedule("unstarted", "a1b2c3:02:1", "B", "", 5, 60000, "SB", 1,
                            displaced, reason));

  const auto stops = scheduler.drain_armed_stops();
  assert(stops.size() == 1);
  assert(stops[0].raw == "SA");
  assert(stops[0].occupancy_ms == 5);
  assert(scheduler.idle());
  assert(!scheduler.next(60000, started));
  assert(scheduler.drain_armed_stops().empty());
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


def test_generated_ota_begin_pumps_dispatch_until_natural_deadline(tmp_path: Path) -> None:
    """A deadline that lands inside the wait window fires at its real time, not early."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the generated firmware test")
    ota_lambda = _firmware_lambda("    on_begin:", "    on_error:")
    source = tmp_path / "generated_ota_begin_natural_test.cpp"
    binary = tmp_path / "generated_ota_begin_natural_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

#include "rf433_scheduler.h"

struct FakeBridge {
  std::vector<std::string> sent;
  void send_raw(const std::string &raw) { this->sent.push_back(raw); }
} portisch_rf_bridge;

uint32_t fake_now_ms{0};
bool ota_active{false};

uint32_t millis() { return fake_now_ms; }
void delay(uint32_t duration_ms) { fake_now_ms += duration_ms; }

#define id(value) value

void generated_ota_begin() {
"""
        + ota_lambda
        + r"""
}

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


def test_generated_ota_begin_falls_back_to_flush_past_the_wait_window(tmp_path: Path) -> None:
    """A deadline beyond the wait window still gets the early-STOP flush."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the generated firmware test")
    ota_lambda = _firmware_lambda("    on_begin:", "    on_error:")
    source = tmp_path / "generated_ota_begin_fallback_test.cpp"
    binary = tmp_path / "generated_ota_begin_fallback_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

#include "rf433_scheduler.h"

struct FakeBridge {
  std::vector<std::string> sent;
  void send_raw(const std::string &raw) { this->sent.push_back(raw); }
} portisch_rf_bridge;

uint32_t fake_now_ms{0};
bool ota_active{false};

uint32_t millis() { return fake_now_ms; }
void delay(uint32_t duration_ms) { fake_now_ms += duration_ms; }

#define id(value) value

void generated_ota_begin() {
"""
        + ota_lambda
        + r"""
}

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


def test_generated_ota_error_unlatches_tx(tmp_path: Path) -> None:
    """A failed transfer must not leave /tx latched shut until a manual reboot."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the generated firmware test")
    ota_error_lambda = _firmware_lambda("    on_error:", "\n\nmqtt:")
    source = tmp_path / "generated_ota_error_test.cpp"
    binary = tmp_path / "generated_ota_error_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>

bool ota_active{false};

#define id(value) value

void generated_ota_error() {
"""
        + ota_error_lambda
        + r"""
}

int main() {
  ota_active = true;
  generated_ota_error();
  assert(!ota_active);
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


def test_native_lifecycle_outbox_retries_in_order_once_after_reconnect(
    tmp_path: Path,
) -> None:
    """A lost accepted/started pair is delivered FIFO exactly once on reconnect."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "lifecycle_outbox_test.cpp"
    binary = tmp_path / "lifecycle_outbox_test"
    source.write_text(
        r"""
#include <cassert>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::LifecycleEvent;
using rf433::LifecycleKind;
using rf433::LifecycleOutbox;
using rf433::TargetScheduler;

int main() {
  TargetScheduler scheduler(0);
  LifecycleOutbox outbox;
  bool connected = false;
  std::vector<LifecycleKind> delivered;
  auto publish = [&](const LifecycleEvent &event) {
    if (!connected)
      return false;
    delivered.push_back(event.kind);
    return true;
  };

  std::string reason;
  std::string started;
  std::vector<std::string> displaced;
  assert(scheduler.schedule("move-1", "a1b2c3:01:1", "A", "", 1, 1000, "SA", 0,
                            displaced, reason));
  assert(!outbox.publish_or_enqueue(LifecycleEvent::accepted("move-1"), publish));
  auto raw = scheduler.next(0, started);
  assert(raw && *raw == "A" && started == "move-1");
  assert(!outbox.publish_or_enqueue(LifecycleEvent::started("move-1", 0, 0, 42), publish));
  // A duplicate callback for the same lifecycle transition coalesces by id
  // instead of consuming capacity or delivering started twice.
  assert(!outbox.publish_or_enqueue(LifecycleEvent::started("move-1", 5, 5, 42), publish));
  assert(outbox.size() == 2);
  assert(delivered.empty());

  connected = true;
  assert(outbox.flush(publish) == 2);
  assert(outbox.empty());
  assert(delivered.size() == 2);
  assert(delivered[0] == LifecycleKind::ACCEPTED);
  assert(delivered[1] == LifecycleKind::STARTED);
  assert(outbox.flush(publish) == 0);
  assert(delivered.size() == 2);

  // A replayed accepted may arrive while only started is pending. Lifecycle
  // order, not callback arrival order, still governs delivery for that id.
  LifecycleOutbox replay_order;
  connected = false;
  assert(!replay_order.publish_or_enqueue(
      LifecycleEvent::started("move-2", 5, 5, 42), publish));
  assert(!replay_order.publish_or_enqueue(
      LifecycleEvent::accepted("move-2"), publish));
  connected = true;
  delivered.clear();
  assert(replay_order.flush(publish) == 2);
  assert(delivered[0] == LifecycleKind::ACCEPTED);
  assert(delivered[1] == LifecycleKind::STARTED);

  // Capacity is fixed. Sustained failures drop the oldest event and expose
  // the loss through a monotonic counter.
  connected = false;
  for (size_t index = 0; index <= LifecycleOutbox::CAPACITY; index++) {
    assert(!outbox.publish_or_enqueue(
        LifecycleEvent::accepted("overflow-" + std::to_string(index)), publish));
  }
  assert(outbox.size() == LifecycleOutbox::CAPACITY);
  assert(outbox.dropped_count() == 1);
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


def test_generated_tx_and_tick_replay_started_once_after_reconnect(tmp_path: Path) -> None:
    """Execute the shipped admission/tick lambdas across an MQTT disconnect."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the generated firmware test")
    tx_lambda = _firmware_lambda(
        "- topic: rf433/${bridge_id}/tx",
        "\n\n  - topic: rf433/${bridge_id}/cmd",
    )
    tick_lambda = _firmware_lambda(
        "interval:\n  - interval: 5ms",
        "\n  - interval: 10s",
    )
    source = tmp_path / "generated_lifecycle_test.cpp"
    binary = tmp_path / "generated_lifecycle_test"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <cstring>
#include <map>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#include "rf433_rx.h"
#include "rf433_scheduler.h"

struct JsonValueData {
  std::variant<std::monostate, std::string, int, bool, uint32_t> value;
};

struct JsonValue {
  const JsonValueData *data;

  bool isNull() const {
    return this->data == nullptr ||
           std::holds_alternative<std::monostate>(this->data->value);
  }

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

  int operator|(int fallback) const {
    return this->data != nullptr && std::holds_alternative<int>(this->data->value)
               ? std::get<int>(this->data->value)
               : fallback;
  }
};

struct FakeJson {
  std::map<std::string, JsonValueData> values;

  JsonValue operator[](const char *key) const {
    const auto found = this->values.find(key);
    return {found == this->values.end() ? nullptr : &found->second};
  }

  void set_string(const std::string &key, const std::string &value) {
    this->values[key].value = value;
  }

  void set_int(const std::string &key, int value) {
    this->values[key].value = value;
  }

  void set_uint(const std::string &key, uint32_t value) {
    this->values[key].value = value;
  }
};

struct JsonSlot {
  std::map<std::string, std::string> *values;
  std::string key;

  JsonSlot &operator=(const std::string &value) {
    (*this->values)[this->key] = value;
    return *this;
  }

  JsonSlot &operator=(const char *value) {
    (*this->values)[this->key] = value;
    return *this;
  }

  JsonSlot &operator=(uint32_t value) {
    (*this->values)[this->key] = std::to_string(value);
    return *this;
  }

  JsonSlot &operator=(int value) {
    (*this->values)[this->key] = std::to_string(value);
    return *this;
  }

  JsonSlot &operator=(bool value) {
    (*this->values)[this->key] = value ? "true" : "false";
    return *this;
  }
};

struct JsonObject {
  std::map<std::string, std::string> *values;
  JsonSlot operator[](const char *key) { return {this->values, key}; }
};

struct Message {
  std::string topic;
  std::map<std::string, std::string> payload;
  int qos;
  bool retained;
};

struct FakeMqtt {
  bool connected{false};
  size_t attempts{0};
  std::vector<Message> messages;

  bool is_connected() const { return this->connected; }

  template<typename F>
  bool publish_json(const std::string &topic, F &&builder, int qos, bool retained) {
    this->attempts++;
    Message message{topic, {}, qos, retained};
    builder(JsonObject{&message.payload});
    if (!this->connected)
      return false;
    this->messages.push_back(std::move(message));
    return true;
  }
} mqtt_client;

struct FakeBridge {
  std::vector<std::string> sent;
  bool sniffing{false};

  void send_raw(const std::string &raw) { this->sent.push_back(raw); }
  void start_bucket_sniffing() { this->sniffing = true; }
  void stop_advanced_sniffing() { this->sniffing = false; }
  bool receive_idle() const { return true; }
} portisch_rf_bridge;

uint32_t fake_now_ms{0};
uint32_t boot_id{42};
uint32_t next_info_ms{0};
bool ota_active{false};

uint32_t millis() { return fake_now_ms; }

#define ESP_LOGW(...) ((void) 0)
#define id(value) value

void generated_tx_handler(const FakeJson &x) {
"""
        + tx_lambda
        + r"""
}

void generated_tick() {
"""
        + tick_lambda
        + r"""
}

int main() {
  const std::string frame = "AAB005010100010055";

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

  // Production boot_id is random_uint32, so real values exceed INT32_MAX
  // roughly half the time. Prove the boot check accepts a matching value up
  // there too, not just the small literal 42 the rest of this test uses.
  boot_id = 3000000000u;
  FakeJson big_boot;
  big_boot.set_string("command_id", "big-boot-1");
  big_boot.set_string("target", "a1b2c3:01:1");
  big_boot.set_string("raw", frame);
  big_boot.set_uint("boot", 3000000000u);
  generated_tx_handler(big_boot);
  assert(mqtt_client.messages.back().payload.at("status") == "accepted");
  assert(mqtt_client.messages.back().payload.at("command_id") == "big-boot-1");
  rf433::tx_scheduler(35).disarm("big-boot-1");

  FakeJson stale_big_boot;
  stale_big_boot.set_string("command_id", "stale-big-boot-1");
  stale_big_boot.set_string("target", "a1b2c3:01:1");
  stale_big_boot.set_string("raw", frame);
  stale_big_boot.set_uint("boot", 3000000001u);
  generated_tx_handler(stale_big_boot);
  assert(mqtt_client.messages.back().payload.at("status") == "rejected");
  assert(mqtt_client.messages.back().payload.at("reason") == "boot_mismatch");
  assert(rf433::tx_scheduler(35).idle());
  boot_id = 42;

  mqtt_client.messages.clear();
  mqtt_client.connected = false;

  FakeJson command;
  command.set_string("command_id", "move-1");
  command.set_string("target", "a1b2c3:01:1");
  command.set_string("raw", frame);
  command.set_int("repeats", 1);
  command.set_int("stop_after_ms", 1000);
  command.set_string("stop_raw", frame);
  command.set_uint("boot", 42);

  fake_now_ms = 100;
  generated_tx_handler(command);
  assert(mqtt_client.messages.empty());
  assert(rf433::lifecycle_outbox().size() == 1);  // accepted is retained

  generated_tick();
  assert(portisch_rf_bridge.sent == std::vector<std::string>({frame}));
  assert(rf433::lifecycle_outbox().size() == 2);  // accepted, then started
  assert(mqtt_client.messages.empty());

  mqtt_client.connected = true;
  fake_now_ms = 105;
  generated_tick();
  std::vector<Message> statuses;
  for (const Message &message : mqtt_client.messages) {
    if (message.topic == "rf433/test-bridge/status")
      statuses.push_back(message);
  }
  assert(statuses.size() == 2);
  assert(statuses[0].payload.at("status") == "accepted");
  assert(statuses[0].payload.at("command_id") == "move-1");
  assert(statuses[1].payload.at("status") == "started");
  assert(statuses[1].payload.at("command_id") == "move-1");
  assert(statuses[1].payload.at("boot") == "42");
  assert(rf433::lifecycle_outbox().empty());
  bool saw_info = false;
  for (const Message &message : mqtt_client.messages) {
    if (message.topic == "rf433/test-bridge/info") {
      assert(message.payload.at("v") == "3");
      // hw is additive to contract v3: hardware_variant flows through verbatim.
      assert(message.payload.at("hw") == "test-hw");
      saw_info = true;
    }
  }
  assert(saw_info);

  fake_now_ms = 110;
  generated_tick();
  size_t delivered_statuses = 0;
  for (const Message &message : mqtt_client.messages) {
    if (message.topic == "rf433/test-bridge/status")
      delivered_statuses++;
  }
  assert(delivered_statuses == 2);  // no reconnect duplicate
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


def test_esphome_package_uses_lightweight_correlated_started_status() -> None:
    """Firmware reports admission plus the first actual RF ACTION dispatch."""
    package = BRIDGE_YAML.read_text()
    scheduler = SCHEDULER_HEADER.read_text()

    assert "rf433_scheduler.h" in package
    assert "TargetScheduler" in scheduler
    assert "rf433::tx_scheduler" in package
    assert 'x["command_id"]' in package
    # /tx reads command_id and target through the length-bounding helper so an
    # oversized field is rejected before it is materialized as a std::string.
    assert 'bounded_field("command_id", 64)' in package
    assert 'bounded_field("target", 53)' in package
    assert '"stop_raw"' in package
    assert '"trailer_raw"' in package
    assert "stop_raw requires stop_after_ms" in package
    assert 'root["command_id"]' in package
    assert 'root["status"]' in package
    assert 'root["target"]' not in package
    assert "LifecycleEvent::accepted" in package
    assert "LifecycleEvent::rejected" in package
    assert ".schedule(" in package
    assert ".next(" in package
    assert 'publish_status("queued"' not in package
    assert "LifecycleEvent::started" in package
    assert "lifecycle_outbox" in package
    assert "publish_or_enqueue" in package
    assert "outbox.flush(send_status)" in package
    assert "LifecycleEvent::displaced" in package
    assert "displaced_ids" in package
    assert '"sent"' not in package
    assert '"cancelled"' not in package
    assert 'root["queue_depth"]' not in package
    assert 'x["cancel_of"]' not in package
    assert ".stop_and_drain(" not in package
    assert "mode: restart" not in package
    assert "script:" not in package
    assert "Dispatch" not in scheduler
    assert "CancelResult" not in scheduler
    assert "queue_depth" not in scheduler
    assert "stop_and_drain" not in scheduler
    # #6: `displaced` now carries the same clock information as `started` so the
    # consumer can MEASURE its post-displacement flush window from the actual
    # displacement instant instead of budgeting a wider-than-necessary one.
    # The redelivery path only stamps age when the firmware genuinely holds the
    # instant, so it branches on the has_age signal it asks replay_state for.
    assert "replay_state(command_id, replay_ms, replay_age_ms,\n" in package
    assert "&replay_has_age)" in package
    assert "if (replay_has_age) {" in package
    # Two sites emit an age-anchored status through the shared replay variable:
    # the replayed `started` and the replayed `displaced`. Reverting either to a
    # bare, ageless publish drops this count.
    assert package.count("replay_age_ms, replay_ms, id(boot_id)") == 2
    # The fresh-admission path anchors each displacement on the admission millis,
    # mirroring `started`'s `status_ms - dispatch_ms, status_ms` shape.
    assert "displaced_status_ms - admit_ms, displaced_status_ms, id(boot_id)" in package


def test_native_scheduler_stamps_age_on_displaced(tmp_path: Path) -> None:
    """`displaced` carries age since the ORIGINAL displacement, from both the
    live flush queue and the ring, and withholds it when no instant exists."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / "displaced_age.cpp"
    binary = tmp_path / "displaced_age"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

int main() {
  std::string reason;
  std::string started;
  std::vector<std::string> displaced;
  uint32_t age = 0;
  bool has_age = false;

  // A timed command that has started RF still owes a fail-safe STOP; displacing
  // it moves that owed STOP into the live flush queue AND remembers the
  // displacement instant. Query at the instant itself: age is exactly 0 and is
  // flagged as a real measurement, not the old silent 0.
  TargetScheduler owe(35);
  assert(owe.schedule("victim", "a1b2c3:20:1", "V", "", 3, 60000, "SV", 0, displaced, reason));
  auto raw = owe.next(0, started);
  assert(raw && *raw == "V" && started == "victim");
  assert(owe.schedule("usurper", "a1b2c3:20:1", "U", "", 1, 0, "", 100, displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "victim");
  int state = owe.replay_state("victim", 100, age, &has_age);
  assert(state == 4 && has_age && age == 0);  // from flush_stops_, at the instant

  // Still draining in the flush queue 540 ms later: age tracks elapsed time
  // since the displacement (t=100), NOT the query time.
  state = owe.replay_state("victim", 640, age, &has_age);
  assert(state == 4 && has_age && age == 540);

  // Drain every owed STOP copy so `victim` leaves flush_stops_ and is answered
  // from the ring instead. A REDELIVERY long after still reports the age since
  // the original displacement (t=100), proving the ring carries the instant.
  for (uint32_t t = 100; t <= 500; t++) {
    std::string st;
    owe.next(t, st);
  }
  state = owe.replay_state("victim", 5000, age, &has_age);
  assert(state == 4 && has_age && age == 4900);  // from the ring, since t=100

  // A displaced command that owes NO fail-safe STOP never enters flush_stops_,
  // yet the ring still carries its displacement instant.
  TargetScheduler noowe(35);
  assert(noowe.schedule("no-owe", "aabbcc:21:1", "N", "", 3, 0, "", 0, displaced, reason));
  raw = noowe.next(0, started);
  assert(raw && *raw == "N" && started == "no-owe");  // started, more repeats, no STOP owed
  assert(noowe.schedule("replacer", "aabbcc:21:1", "R", "", 1, 0, "", 250, displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "no-owe");
  state = noowe.replay_state("no-owe", 250, age, &has_age);
  assert(state == 4 && has_age && age == 0);
  state = noowe.replay_state("no-owe", 900, age, &has_age);
  assert(state == 4 && has_age && age == 650);

  // A command that reaches the terminal state 4 by DISARM (not displacement)
  // has no displacement instant. replay_state must say so explicitly by
  // clearing has_age rather than reporting a bogus age of 0.
  TargetScheduler dis(35);
  assert(dis.schedule("gone", "aabbcc:22:1", "D", "", 1, 60000, "SD", 0, displaced, reason));
  raw = dis.next(0, started);
  assert(raw && *raw == "D");
  dis.disarm("gone");
  has_age = true;  // must be cleared by replay_state
  age = 12345;     // must be reset by replay_state
  state = dis.replay_state("gone", 300, age, &has_age);
  assert(state == 4 && !has_age && age == 0);

  // The three-argument form still compiles and behaves for callers that do not
  // need the has_age flag (the existing tests use it).
  age = 7;
  state = dis.replay_state("gone", 300, age);
  assert(state == 4 && age == 0);

  // The age-carrying factory mirrors started(): it flags has_age and has_clock
  // so the JSON publisher emits age_ms/t/boot. The bare factory carries none,
  // so no stale age is published.
  auto ev = rf433::LifecycleEvent::displaced("cmd", 43, 123456, 7);
  assert(std::string(ev.status()) == "displaced");
  assert(ev.has_age && ev.age_ms == 43);
  assert(ev.has_clock && ev.timestamp_ms == 123456 && ev.boot_id == 7);
  auto bare = rf433::LifecycleEvent::displaced("cmd");
  assert(std::string(bare.status()) == "displaced");
  assert(!bare.has_age && !bare.has_clock);

  // millis() rollover: a command displaced just before the 2^32 wrap and
  // redelivered just after reports the true small elapsed age via unsigned
  // subtraction, never a huge value or a signed-comparison artifact.
  TargetScheduler wrap(35);
  const uint32_t wrap_start = static_cast<uint32_t>(0xFFFFFFFFu) - 500u;
  assert(wrap.schedule("w-old", "aabbcc:23:1", "W", "", 3, 0, "", wrap_start, displaced, reason));
  raw = wrap.next(wrap_start, started);
  assert(raw && *raw == "W" && started == "w-old");
  const uint32_t disp_at = wrap_start + 300u;  // 0xFFFFFF37, still before the wrap
  assert(wrap.schedule("w-new", "aabbcc:23:1", "Z", "", 1, 0, "", disp_at, displaced, reason));
  assert(displaced.size() == 1 && displaced[0] == "w-old");
  const uint32_t after_wrap = 50u;  // wrapped 50 ms past 0
  state = wrap.replay_state("w-old", after_wrap, age, &has_age);
  assert(state == 4 && has_age);
  assert(age == static_cast<uint32_t>(after_wrap - disp_at));
  assert(age == 251u);  // 201 ms to the wrap + 50 ms after it

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
