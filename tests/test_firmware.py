"""Native scheduler and ESPHome package contract tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SCHEDULER_HEADER = PROJECT_ROOT / "rf433_scheduler.h"
BRIDGE_YAML = PROJECT_ROOT / "rf433-mqtt-bridge.yaml"


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


def test_esphome_package_uses_lightweight_correlated_started_status() -> None:
    """Firmware reports admission plus the first actual RF ACTION dispatch."""
    package = BRIDGE_YAML.read_text()
    scheduler = SCHEDULER_HEADER.read_text()

    assert "rf433_scheduler.h" in package
    assert "TargetScheduler" in scheduler
    assert "rf433::tx_scheduler" in package
    assert 'x["command_id"]' in package
    assert 'x["target"]' in package
    assert '"stop_raw"' in package
    assert '"trailer_raw"' in package
    assert "stop_raw requires stop_after_ms" in package
    assert 'root["command_id"]' in package
    assert 'root["status"]' in package
    assert 'root["target"]' not in package
    assert 'publish_status("accepted"' in package
    assert '"rejected"' in package
    assert ".schedule(" in package
    assert ".next(" in package
    assert 'publish_status("queued"' not in package
    assert 'publish_status("started"' in package
    assert '"displaced"' in package
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
