"""Concurrency dispatch-ordering study for TargetScheduler (issue #19).

These native tests MEASURE the concrete dispatch timeline when N targets are
queued on one bridge at once. They answer, with executed evidence rather than
inference:

  * ordering: are concurrent targets' repeat trains INTERLEAVED (round-robin
    A,B,C,A,B,C) or dispatched as consecutive whole trains?
  * cadence: what is one target's inter-repeat gap solo vs. sharing the bridge
    with two others?
  * drops: are any repeats dropped (vs. merely delayed) under pure 3-way
    concurrency of three DIFFERENT remotes?
  * STOP preemption: when one target's armed fail-safe STOP comes due mid-run,
    does it truncate the OTHER targets' remaining repeats?

Each test compiles and runs the shipped C++ scheduler on the host compiler,
following the same inline-source convention as tests/test_firmware.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]

# A shared C++ prologue: a 1 ms polling driver that records the exact dispatch
# timeline. Polling every 1 ms is a finer version of the real ESPHome 5 ms
# interval, so the recovered order and cadence are the scheduler's actual
# behavior, not an approximation.
_PROLOGUE = r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

struct Tick { uint32_t t; std::string raw; std::string started; };

[[maybe_unused]] static std::vector<Tick> run(TargetScheduler &s, uint32_t t0, uint32_t t_max) {
  std::vector<Tick> out;
  for (uint32_t t = t0; t <= t_max; t++) {
    std::string started;
    auto raw = s.next(t, started);
    if (raw)
      out.push_back({t, *raw, started});
  }
  return out;
}

// Count how many recorded dispatches carried a given raw frame.
[[maybe_unused]] static size_t count_raw(const std::vector<Tick> &ticks, const std::string &raw) {
  size_t n = 0;
  for (const auto &tk : ticks)
    if (tk.raw == raw)
      n++;
  return n;
}

// One command to admit at a chosen instant, reproducing staggered MQTT
// admission (commands do not all arrive on the same scheduler tick).
struct Pending {
  uint32_t at;
  std::string id, target, raw, trailer, stop;
  int repeats;
  uint32_t stop_after;
  bool done;
};

[[maybe_unused]] static std::vector<Tick> run_staggered(TargetScheduler &s,
                                                        std::vector<Pending> &pend,
                                                        uint32_t t_max) {
  std::vector<Tick> out;
  std::string reason;
  std::vector<std::string> displaced;
  for (uint32_t t = 0; t <= t_max; t++) {
    for (auto &p : pend) {
      if (!p.done && t >= p.at) {
        s.schedule(p.id, p.target, p.raw, p.trailer, p.repeats, p.stop_after, p.stop, t,
                   displaced, reason);
        p.done = true;
      }
    }
    std::string started;
    auto raw = s.next(t, started);
    if (raw)
      out.push_back({t, *raw, started});
  }
  return out;
}

// Three distinct B0 frames of identical airtime (differ only in which equal
// bucket the two data nibbles reference), plus a fourth for a STOP; used so
// every target's individual dispatches are observable while occupancy stays
// uniform.
[[maybe_unused]] static const std::string FX = "AAB0070208FFFFFFFF0055";
[[maybe_unused]] static const std::string FY = "AAB0070208FFFFFFFF0155";
[[maybe_unused]] static const std::string FZ = "AAB0070208FFFFFFFF1155";
[[maybe_unused]] static const std::string FW = "AAB0070208FFFFFFFF1055";
"""


def _compile_and_run(tmp_path: Path, name: str, body: str) -> None:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a C++ compiler is required for the native firmware scheduler test")
    source = tmp_path / f"{name}.cpp"
    binary = tmp_path / name
    source.write_text(_PROLOGUE + "\nint main() {\n" + body + "\n  return 0;\n}\n")
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


def test_concurrent_targets_interleave_round_robin(tmp_path: Path) -> None:
    """Interleave concurrent different-remote targets round-robin, dropping no repeats.

    Three concurrent DIFFERENT-remote targets dispatch round-robin, not as
    consecutive whole trains, and no repeats are dropped.
    """
    _compile_and_run(
        tmp_path,
        "interleave",
        r"""
  std::string reason;
  std::vector<std::string> displaced;

  // Baseline: one target alone, repeats=3, gap-paced tiny frames (gap=35).
  // Solo cadence is one repeat every 35 ms: 0, 35, 70.
  TargetScheduler solo(35);
  assert(solo.schedule("cmd-a", "a1b2c3:42:1", "A", "", 3, 0, "", 0, displaced, reason));
  auto st = run(solo, 0, 500);
  assert(st.size() == 3);
  assert(st[0].t == 0 && st[1].t == 35 && st[2].t == 70);
  const uint32_t solo_gap = st[1].t - st[0].t;   // 35
  assert(solo_gap == 35);

  // Three targets on three DIFFERENT remotes, all admitted at t=0, repeats=3.
  // Different remote IDs mean no displacement is possible between them.
  TargetScheduler multi(35);
  assert(multi.schedule("cmd-a", "a1b2c3:42:1", "A", "", 3, 0, "", 0, displaced, reason));
  assert(multi.schedule("cmd-b", "a1b2c3:43:1", "B", "", 3, 0, "", 0, displaced, reason));
  assert(multi.schedule("cmd-c", "a1b2c3:44:1", "C", "", 3, 0, "", 0, displaced, reason));
  auto mt = run(multi, 0, 500);

  // No drops: every one of the 3x3 repeats is transmitted.
  assert(mt.size() == 9);
  assert(count_raw(mt, "A") == 3);
  assert(count_raw(mt, "B") == 3);
  assert(count_raw(mt, "C") == 3);

  // Ordering is round-robin INTERLEAVED, not consecutive whole trains. The
  // exact observed sequence is A,B,C,A,B,C,A,B,C. (Consecutive trains would
  // read A,A,A,B,B,B,C,C,C and fail this block.)
  const char *expect[] = {"A", "B", "C", "A", "B", "C", "A", "B", "C"};
  const uint32_t when[] = {0, 35, 70, 105, 140, 175, 210, 245, 280};
  for (int i = 0; i < 9; i++) {
    assert(mt[i].raw == expect[i]);
    assert(mt[i].t == when[i]);
  }

  // Target A's own repeats land at 0, 105, 210: its inter-repeat gap is
  // stretched to 3x the solo gap because B and C are interleaved between them.
  assert(mt[0].raw == "A" && mt[3].raw == "A" && mt[6].raw == "A");
  const uint32_t multi_gap = mt[3].t - mt[0].t;  // 105
  assert(multi_gap == 105);
  assert(multi_gap == 3 * solo_gap);
""",
    )


def test_inter_repeat_gap_stretches_3x_with_real_airtime(tmp_path: Path) -> None:
    """Stretch a target's inter-repeat gap to N x the real-airtime dispatch slot.

    With production-weight airtime frames the per-dispatch slot is the
    frame's physical occupancy, and a shared bridge stretches one target's
    inter-repeat gap to N x that slot. Frames are three distinct-but-equal
    airtime B0 frames so each target's dispatches are individually observable.
    """
    _compile_and_run(
        tmp_path,
        "airtime_stretch",
        r"""
  std::string reason;
  std::vector<std::string> displaced;

  // Three valid B0 frames with identical on-air time (two 0xFFFF-us pulses at
  // embedded repeat 8) that differ only in which equal-duration bucket the two
  // data nibbles reference -- so occupancy is identical but the raw strings are
  // distinct and each target's repeats are individually identifiable.
  const std::string X = "AAB0070208FFFFFFFF0055";
  const std::string Y = "AAB0070208FFFFFFFF0155";
  const std::string Z = "AAB0070208FFFFFFFF1155";

  // Solo occupancy slot: one repeat every ~1060 ms (UART serialization + air +
  // margin), dwarfing the 35 ms user gap.
  TargetScheduler solo(35);
  assert(solo.schedule("cmd-a", "a1b2c3:42:1", X, "", 3, 0, "", 0, displaced, reason));
  auto st = run(solo, 0, 6000);
  assert(st.size() == 3);
  const uint32_t slot = st[1].t - st[0].t;   // ~1060
  assert(slot > 600);                        // occupancy-bound, not gap-bound
  assert(st[2].t - st[1].t == slot);

  // Three concurrent targets, distinct remotes, real airtime.
  TargetScheduler multi(35);
  assert(multi.schedule("cmd-a", "a1b2c3:42:1", X, "", 3, 0, "", 0, displaced, reason));
  assert(multi.schedule("cmd-b", "a1b2c3:43:1", Y, "", 3, 0, "", 0, displaced, reason));
  assert(multi.schedule("cmd-c", "a1b2c3:44:1", Z, "", 3, 0, "", 0, displaced, reason));
  auto mt = run(multi, 0, 12000);

  // No drops: all nine repeats go on air.
  assert(mt.size() == 9);
  assert(count_raw(mt, X) == 3);
  assert(count_raw(mt, Y) == 3);
  assert(count_raw(mt, Z) == 3);

  // The three FIRST dispatches interleave: started fires for A, then B, then C
  // on consecutive slots BEFORE A's second repeat -- direct evidence of
  // round-robin ordering rather than A finishing its train first.
  assert(mt[0].started == "cmd-a" && mt[0].t == 0);
  assert(mt[1].started == "cmd-b" && mt[1].t == slot);
  assert(mt[2].started == "cmd-c" && mt[2].t == 2 * slot);

  // Target A's repeats: 0, 3*slot, 6*slot. Its inter-repeat gap is 3x the solo
  // slot -- the concrete cadence a motor sharing the bridge actually receives.
  assert(mt[0].raw == X && mt[3].raw == X && mt[6].raw == X);
  assert(mt[3].t - mt[0].t == 3 * slot);
  assert(mt[6].t - mt[3].t == 3 * slot);
""",
    )


def test_timed_train_finishes_before_deadline_without_dropping_peers(
    tmp_path: Path,
) -> None:
    """Finish the timed train first, delaying peers rather than dropping them.

    A started timed train keeps normal dispatch until its repeats finish.
    Peer targets' trains are delayed, not dropped, and STOP copies dispatch
    consecutively (physical pacing only), bypassing the user gap floor.
    """
    _compile_and_run(
        tmp_path,
        "stop_preempt",
        r"""
  std::string reason;
  std::vector<std::string> displaced;

  // A: repeats=5 with an armed fail-safe STOP (stop_after_ms=200). Its train
  // owns the bridge after starting at t=0 and finishes at t=140.
  // B, C: plain repeats=5 on different remotes. Tiny frames, gap=35.
  TargetScheduler s(35);
  assert(s.schedule("cmd-a", "a1b2c3:42:1", "A", "", 5, 200, "SA", 0, displaced, reason));
  assert(s.schedule("cmd-b", "a1b2c3:43:1", "B", "", 5, 0, "", 0, displaced, reason));
  assert(s.schedule("cmd-c", "a1b2c3:44:1", "C", "", 5, 0, "", 0, displaced, reason));
  auto t = run(s, 0, 2000);

  // Peers are NOT starved: B and C each still receive all five repeats.
  assert(count_raw(t, "B") == 5);
  assert(count_raw(t, "C") == 5);

  // A's five copies are consecutive normal-work slots and all land before
  // the unchanged t=200 deadline.
  std::vector<uint32_t> a_times;
  for (const auto &tk : t)
    if (tk.raw == "A")
      a_times.push_back(tk.t);
  const uint32_t expected_a_times[] = {0, 35, 70, 105, 140};
  assert(a_times.size() == 5);
  for (size_t i = 0; i < a_times.size(); i++)
    assert(a_times[i] == expected_a_times[i]);

  // The fail-safe STOP fires all five copies CONSECUTIVELY once due -- 5 ms
  // apart (physical occupancy only), not interleaved with B/C and not held by
  // the 35 ms user gap.
  std::vector<uint32_t> sa_times;
  for (const auto &tk : t)
    if (tk.raw == "SA")
      sa_times.push_back(tk.t);
  assert(sa_times.size() == 5);
  assert(sa_times[0] == 200);
  for (int i = 1; i < 5; i++)
    assert(sa_times[i] - sa_times[i - 1] == 5);   // back-to-back, physical pace

  // While A's STOP burst is on air (200..220) no B or C frame is dispatched;
  // they resume afterward -- delayed, never dropped.
  for (const auto &tk : t)
    if (tk.raw == "B" || tk.raw == "C")
      assert(tk.t < 200 || tk.t > 220);
""",
    )


def test_staggered_admission_reproduces_on_air_run1(tmp_path: Path) -> None:
    """Reproduce the live-hardware run-1 on-air sequence from staggered admission.

    Validation against live-hardware ground truth: three commands admitted a
    little apart (as MQTT delivers them) reproduce the on-air heard sequence
    A A B C A B C B C with started stamps at slots 0, 2, 3. The 'A A' opening
    is round-robin with one target alone in the rotation for two slots before
    the others join -- not a deviation from round-robin.
    """
    _compile_and_run(
        tmp_path,
        "staggered_run1",
        r"""
  TargetScheduler s(35);
  // A admitted first, B and C join across the first two ~1060 ms slots.
  std::vector<Pending> pend = {
      {0,    "cmd-a", "a1b2c3:42:1", FX, "", "", 3, 0, false},
      {500,  "cmd-b", "a1b2c3:43:1", FY, "", "", 3, 0, false},
      {1500, "cmd-c", "a1b2c3:44:1", FZ, "", "", 3, 0, false},
  };
  auto t = run_staggered(s, pend, 12000);

  // Nine dispatches, none dropped.
  assert(t.size() == 9);
  assert(count_raw(t, FX) == 3 && count_raw(t, FY) == 3 && count_raw(t, FZ) == 3);

  // Exact on-air reconstruction: A A B C A B C B C.
  const std::string seq[] = {FX, FX, FY, FZ, FX, FY, FZ, FY, FZ};
  for (int i = 0; i < 9; i++)
    assert(t[i].raw == seq[i]);

  // started stamps land where the six idle peers reported them: A at slot 0,
  // B at slot 2, C at slot 3.
  assert(t[0].started == "cmd-a");
  assert(t[2].started == "cmd-b");
  assert(t[3].started == "cmd-c");
  assert(t[1].started.empty());  // slot 1 is A's second repeat, not a new start
""",
    )


def test_timed_repeats_finish_with_solo_equal_stop_lateness(
    tmp_path: Path,
) -> None:
    """Pack timed repeats earlier while retaining the solo STOP-lateness bound.

    Validation against live-hardware run 3: a timed command's fail-safe STOP
    is promoted ahead of queued peer ACTION work and still waits out physical
    RF occupancy. Consecutive scheduling preserves all timed repeats without
    adding concurrency delay beyond the owning frame's solo occupancy.
    """
    _compile_and_run(
        tmp_path,
        "staggered_run3",
        r"""
  // Pin the complete pre-fix solo timeline: dispatch selection must not alter
  // a timed command when no peer is competing for the bridge.
  std::string reason;
  std::vector<std::string> displaced;
  TargetScheduler solo(35);
  assert(solo.schedule("solo", "a1b2c3:42:1", FX, "", 3, 2620, FW, 0,
                       displaced, reason));
  auto solo_t = run(solo, 0, 8000);
  const std::string solo_frames[] = {FX, FX, FX, FW, FW, FW};
  const uint32_t solo_times[] = {0, 1060, 2120, 3180, 4240, 5300};
  assert(solo_t.size() == 6);
  for (size_t i = 0; i < solo_t.size(); i++) {
    assert(solo_t[i].raw == solo_frames[i]);
    assert(solo_t[i].t == solo_times[i]);
  }

  // An untimed solo train retains the same pre-fix frame timeline too.
  TargetScheduler untimed_solo(35);
  assert(untimed_solo.schedule("untimed-solo", "a1b2c3:42:1", FX, "", 3, 0, "", 0,
                               displaced, reason));
  auto untimed_solo_t = run(untimed_solo, 0, 5000);
  const uint32_t untimed_solo_times[] = {0, 1060, 2120};
  assert(untimed_solo_t.size() == 3);
  for (size_t i = 0; i < untimed_solo_t.size(); i++) {
    assert(untimed_solo_t[i].raw == FX);
    assert(untimed_solo_t[i].t == untimed_solo_times[i]);
  }

  TargetScheduler s(35);
  // Timed A (repeats=3, STOP frame FW, deadline arms at first dispatch and
  // comes due at 2620 ms -- mid-train) concurrent with plain B (repeats=3).
  std::vector<Pending> pend = {
      {0,   "cmd-a", "a1b2c3:42:1", FX, "", FW, 3, 2620, false},
      {500, "cmd-b", "a1b2c3:43:1", FY, "", "", 3, 0,    false},
  };
  auto t = run_staggered(s, pend, 12000);

  // The PEER is not truncated: B still gets all three action repeats.
  assert(count_raw(t, FY) == 3);

  // A keeps the bridge for its train, so all three action repeats land before
  // the deadline and the full three-copy fail-safe STOP still follows.
  assert(count_raw(t, FX) == 3);
  assert(count_raw(t, FW) == 3);
  assert(t[0].raw == FX && t[0].t == 0);
  assert(t[1].raw == FX && t[1].t == 1060);
  assert(t[2].raw == FX && t[2].t == 2120);

  // A started at t=0, so deadline_at is 2620. The first STOP waits only for
  // A's in-flight third ACTION to clear at t=3180, matching the solo bound.
  uint32_t first_stop = 0, second_b = 0;
  int b_seen = 0;
  for (const auto &tk : t) {
    if (tk.raw == FW && first_stop == 0)
      first_stop = tk.t;
    if (tk.raw == FY && ++b_seen == 2)
      second_b = tk.t;
  }
  constexpr uint32_t armed_deadline = 2620;
  constexpr uint32_t pre_fix_first_stop = 3180;
  assert(first_stop == pre_fix_first_stop);
  assert(first_stop - armed_deadline == 560);  // waited out the in-flight frame, not longer

  // STOP priority: the first STOP copy goes on air BEFORE B's second action --
  // the promoted STOP jumps ahead of queued ACTION work.
  assert(second_b > first_stop);

  // With asymmetric airtimes, the packed owner -- not the tiny peer -- may be
  // in flight at deadline_at. Its STOP lateness remains exactly the same as
  // solo: only the owning frame's remaining physical occupancy is charged.
  TargetScheduler asymmetric_solo(35);
  assert(asymmetric_solo.schedule("big-solo", "a1b2c3:42:1", FX, "", 2, 1061, FW, 0,
                                  displaced, reason));
  auto asymmetric_solo_t = run(asymmetric_solo, 0, 5000);

  TargetScheduler asymmetric_shared(35);
  assert(asymmetric_shared.schedule("big-shared", "a1b2c3:42:1", FX, "", 2, 1061, FW, 0,
                                    displaced, reason));
  assert(asymmetric_shared.schedule("tiny-peer", "a1b2c3:43:1", "P", "", 3, 0, "", 0,
                                    displaced, reason));
  auto asymmetric_shared_t = run(asymmetric_shared, 0, 6000);
  assert(count_raw(asymmetric_shared_t, FX) == 2);
  assert(count_raw(asymmetric_shared_t, "P") == 3);

  uint32_t solo_first_stop = 0, shared_first_stop = 0, first_peer = 0;
  for (const auto &tk : asymmetric_solo_t)
    if (tk.raw == FW && solo_first_stop == 0)
      solo_first_stop = tk.t;
  for (const auto &tk : asymmetric_shared_t) {
    if (tk.raw == FW && shared_first_stop == 0)
      shared_first_stop = tk.t;
    if (tk.raw == "P" && first_peer == 0)
      first_peer = tk.t;
  }
  constexpr uint32_t asymmetric_deadline = 1061;
  assert(asymmetric_shared_t[1].raw == FX);
  const uint32_t owner_frame_dispatch = asymmetric_shared_t[1].t;
  const uint32_t owner_frame_occupancy = asymmetric_solo_t[1].t - asymmetric_solo_t[0].t;
  const uint32_t owner_frame_clear = owner_frame_dispatch + owner_frame_occupancy;
  assert(shared_first_stop == owner_frame_clear);
  assert(shared_first_stop - asymmetric_deadline == solo_first_stop - asymmetric_deadline);
  assert(first_peer > shared_first_stop);
""",
    )


def test_first_timed_train_owns_actions_and_trailers_before_second_starts(
    tmp_path: Path,
) -> None:
    """Let the first timed train finish ACTION/TRAILER work before the second starts."""
    _compile_and_run(
        tmp_path,
        "two_timed_train_ownership",
        r"""
  std::string reason;
  std::vector<std::string> displaced;
  TargetScheduler s(35);
  assert(s.schedule("first", "a1b2c3:42:1", "A", "TA", 2, 500, "SA", 0,
                    displaced, reason));
  assert(s.schedule("second", "a1b2c3:43:1", "B", "", 2, 500, "SB", 0,
                    displaced, reason));
  auto t = run(s, 0, 1000);

  // The first started timed command owns all normal ACTION and TRAILER slots.
  const char *expected_train[] = {"A", "A", "TA", "TA", "B", "B"};
  const uint32_t expected_times[] = {0, 35, 70, 105, 140, 175};
  for (size_t i = 0; i < 6; i++) {
    assert(t[i].raw == expected_train[i]);
    assert(t[i].t == expected_times[i]);
  }
  assert(t[0].started == "first");
  assert(t[4].started == "second");
  assert(count_raw(t, "A") == 2 && count_raw(t, "TA") == 2);
  assert(count_raw(t, "B") == 2);
  assert(count_raw(t, "SA") == 2 && count_raw(t, "SB") == 2);
""",
    )


def test_completion_telemetry_reports_delivered_action_repeats(tmp_path: Path) -> None:
    """Report ACTION-only counts across solo and concurrent command shapes."""
    _compile_and_run(
        tmp_path,
        "completion_telemetry",
        r"""
  std::string reason;
  std::string started;
  std::vector<std::string> displaced;
  rf433::LifecycleEvent completed;

  TargetScheduler solo(35);
  assert(solo.schedule("solo", "a1b2c3:42:1", FX, "", 3, 2620, FW, 0,
                       displaced, reason));
  bool saw_solo = false;
  for (uint32_t t = 0; t <= 8000; t++) {
    auto raw = solo.next(t, started, &completed);
    if (!raw || completed.command_id.empty())
      continue;
    assert(completed.status() == std::string("completed"));
    assert(completed.has_action_repeats);
    assert(completed.action_repeats_delivered == 3);
    assert(completed.action_repeats_configured == 3);
    saw_solo = true;
  }
  assert(saw_solo);

  TargetScheduler concurrent(35);
  assert(concurrent.schedule("timed", "a1b2c3:42:1", FX, "", 3, 2620, FW, 0,
                             displaced, reason));
  bool peer_admitted = false;
  bool saw_timed = false;
  for (uint32_t t = 0; t <= 12000; t++) {
    if (!peer_admitted && t >= 500) {
      assert(concurrent.schedule("peer", "a1b2c3:43:1", FY, "", 3, 0, "", t,
                                 displaced, reason));
      peer_admitted = true;
    }
    auto raw = concurrent.next(t, started, &completed);
    if (!raw || completed.command_id != "timed")
      continue;
    assert(completed.has_action_repeats);
    assert(completed.action_repeats_delivered == 3);
    assert(completed.action_repeats_configured == 3);
    saw_timed = true;
  }
  assert(saw_timed);

  TargetScheduler truncated(35);
  assert(truncated.schedule("truncated", "a1b2c3:42:1", "A", "", 5, 50, "SA", 0,
                            displaced, reason));
  assert(truncated.schedule("truncated-peer", "a1b2c3:43:1", "B", "", 3, 0, "", 0,
                            displaced, reason));
  int truncated_actions = 0;
  int stop_frames = 0;
  int peer_actions = 0;
  uint32_t last_stop = 0;
  uint32_t first_peer = 0;
  bool saw_truncated = false;
  for (uint32_t t = 0; t <= 500; t++) {
    auto raw = truncated.next(t, started, &completed);
    if (raw && *raw == "A")
      truncated_actions++;
    if (raw && *raw == "SA") {
      stop_frames++;
      last_stop = t;
    }
    if (raw && *raw == "B") {
      peer_actions++;
      if (first_peer == 0) {
        first_peer = t;
        assert(started == "truncated-peer");
      }
    }
    if (completed.command_id == "truncated") {
      assert(completed.has_action_repeats);
      assert(completed.action_repeats_delivered == 2);
      assert(completed.action_repeats_configured == 5);
      saw_truncated = true;
    }
  }
  assert(truncated_actions == 2 && stop_frames == 5 && saw_truncated);
  assert(peer_actions == 3);
  assert(first_peer > last_stop);

  TargetScheduler with_trailer(35);
  assert(with_trailer.schedule("trailer", "a1b2c3:42:1", "ACTION", "TRAILER", 3, 0, "", 0,
                               displaced, reason));
  int action_frames = 0;
  int trailer_frames = 0;
  bool saw_trailer = false;
  for (uint32_t t = 0; t <= 500; t++) {
    auto raw = with_trailer.next(t, started, &completed);
    if (raw && *raw == "ACTION")
      action_frames++;
    if (raw && *raw == "TRAILER")
      trailer_frames++;
    if (completed.command_id == "trailer") {
      assert(completed.action_repeats_delivered == 3);
      assert(completed.action_repeats_configured == 3);
      saw_trailer = true;
    }
  }
  assert(action_frames == 3 && trailer_frames == 3 && saw_trailer);

  TargetScheduler single(35);
  assert(single.schedule("single", "a1b2c3:42:1", "ONE", "", 1, 0, "", 123,
                         displaced, reason));
  auto single_raw = single.next(123, started, &completed);
  assert(single_raw && *single_raw == "ONE");
  assert(started == "single" && completed.command_id == "single");
  assert(completed.action_repeats_delivered == 1);
  assert(completed.action_repeats_configured == 1);
""",
    )


def test_completion_telemetry_preserves_dispatch_timelines(tmp_path: Path) -> None:
    """Keep every frame and dispatch timestamp identical with telemetry enabled."""
    _compile_and_run(
        tmp_path,
        "completion_timeline_equivalence",
        r"""
  auto scenario = [](bool telemetry, bool concurrent, bool pressure) {
    TargetScheduler scheduler(35);
    std::string reason;
    std::string started;
    std::vector<std::string> displaced;
    assert(scheduler.schedule("timed", "a1b2c3:42:1", FX, "", 3, 2620, FW, 0,
                              displaced, reason));

    rf433::LifecycleOutbox outbox;
    auto unavailable = [](const rf433::LifecycleEvent &) { return false; };
    if (pressure) {
      for (size_t index = 0; index < rf433::LifecycleOutbox::CAPACITY; index++) {
        assert(!outbox.publish_or_enqueue(
            rf433::LifecycleEvent::accepted("queued-" + std::to_string(index)), unavailable));
      }
    }

    bool peer_admitted = false;
    std::vector<Tick> timeline;
    for (uint32_t t = 0; t <= 12000; t++) {
      if (concurrent && !peer_admitted && t >= 500) {
        assert(scheduler.schedule("peer", "a1b2c3:43:1", FY, "", 3, 0, "", t,
                                  displaced, reason));
        peer_admitted = true;
      }
      rf433::LifecycleEvent completed;
      auto raw = telemetry ? scheduler.next(t, started, &completed) : scheduler.next(t, started);
      if (raw)
        timeline.push_back({t, *raw, started});
      if (telemetry && !completed.command_id.empty())
        assert(!outbox.publish_or_enqueue(completed, unavailable));
    }
    return timeline;
  };
  auto assert_same = [](const std::vector<Tick> &without_telemetry,
                        const std::vector<Tick> &with_telemetry) {
    assert(without_telemetry.size() == with_telemetry.size());
    for (size_t index = 0; index < without_telemetry.size(); index++) {
      assert(without_telemetry[index].t == with_telemetry[index].t);
      assert(without_telemetry[index].raw == with_telemetry[index].raw);
      assert(without_telemetry[index].started == with_telemetry[index].started);
    }
  };

  assert_same(scenario(false, false, false), scenario(true, false, false));
  const auto concurrent_timeline = scenario(true, true, false);
  assert(count_raw(concurrent_timeline, FX) == 3);
  assert_same(scenario(false, true, false), concurrent_timeline);
  assert_same(scenario(false, true, true), scenario(true, true, true));
""",
    )


def test_completion_telemetry_yaml_source_presence_smoke() -> None:
    """Smoke-check MQTT wiring presence; native simulations prove behavior."""
    package = (PROJECT_ROOT / "rf433-mqtt-bridge.yaml").read_text()
    required_once = (
        "outbox.publish_or_enqueue(completed_event, send_status)",
        'root["action_repeats_delivered"]',
        'root["action_repeats_configured"]',
        "event.has_action_repeats",
        "&completed_event",
    )
    for token in required_once:
        assert package.count(token) == 1, token


def test_completed_outbox_is_best_effort_under_saturation(tmp_path: Path) -> None:
    """Never evict or reorder existing lifecycle kinds for completion telemetry."""
    _compile_and_run(
        tmp_path,
        "completion_outbox_priority",
        r"""
  auto unavailable = [](const rf433::LifecycleEvent &) { return false; };
  auto existing_event = [](size_t index) {
    const std::string id = "existing-" + std::to_string(index);
    switch (index % 5) {
      case 0:
        return rf433::LifecycleEvent::accepted(id);
      case 1:
        return rf433::LifecycleEvent::rejected(id, "reason");
      case 2:
        return rf433::LifecycleEvent::started(id, 1, 2, 3);
      case 3:
        return rf433::LifecycleEvent::displaced(id, 1, 2, 3);
      default:
        return rf433::LifecycleEvent::disarmed(id, 2, 3);
    }
  };

  rf433::LifecycleOutbox full_existing;
  std::vector<rf433::LifecycleEvent> expected;
  for (size_t index = 0; index < rf433::LifecycleOutbox::CAPACITY; index++) {
    auto event = existing_event(index);
    expected.push_back(event);
    assert(!full_existing.publish_or_enqueue(event, unavailable));
  }
  assert(!full_existing.publish_or_enqueue(
      rf433::LifecycleEvent::completed("best-effort", 2, 3), unavailable));
  assert(full_existing.size() == rf433::LifecycleOutbox::CAPACITY);
  assert(full_existing.dropped_count() == 0);

  std::vector<rf433::LifecycleEvent> delivered;
  auto collect = [&](const rf433::LifecycleEvent &event) {
    delivered.push_back(event);
    return true;
  };
  assert(full_existing.flush(collect) == expected.size());
  for (size_t index = 0; index < expected.size(); index++) {
    assert(delivered[index].kind == expected[index].kind);
    assert(delivered[index].command_id == expected[index].command_id);
  }

  rf433::LifecycleOutbox replace_completion;
  assert(!replace_completion.publish_or_enqueue(
      rf433::LifecycleEvent::completed("replace-me", 1, 3), unavailable));
  for (size_t index = 0; index + 1 < rf433::LifecycleOutbox::CAPACITY; index++)
    assert(!replace_completion.publish_or_enqueue(existing_event(index), unavailable));
  assert(!replace_completion.publish_or_enqueue(
      existing_event(rf433::LifecycleOutbox::CAPACITY - 1), unavailable));

  delivered.clear();
  assert(replace_completion.flush(collect) == expected.size());
  for (size_t index = 0; index < expected.size(); index++) {
    assert(delivered[index].kind == expected[index].kind);
    assert(delivered[index].command_id == expected[index].command_id);
  }
""",
    )
