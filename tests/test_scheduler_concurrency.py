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
    """Three concurrent DIFFERENT-remote targets dispatch round-robin, not as
    consecutive whole trains, and no repeats are dropped."""
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
    """With production-weight airtime frames the per-dispatch slot is the
    frame's physical occupancy, and a shared bridge stretches one target's
    inter-repeat gap to N x that slot. Frames are three distinct-but-equal
    airtime B0 frames so each target's dispatches are individually observable."""
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


def test_armed_stop_preempts_own_train_but_not_peers(tmp_path: Path) -> None:
    """An armed fail-safe STOP that comes due mid-run truncates only its OWN
    command's remaining repeats; peer targets' trains are delayed, not
    dropped. STOP copies dispatch consecutively (physical pacing only),
    bypassing both the round-robin interleave and the user gap floor."""
    _compile_and_run(
        tmp_path,
        "stop_preempt",
        r"""
  std::string reason;
  std::vector<std::string> displaced;

  // A: repeats=5 with an armed fail-safe STOP (stop_after_ms=200); its deadline
  // arms when A first dispatches at t=0 and comes due at t=200, mid-train.
  // B, C: plain repeats=5 on different remotes. Tiny frames, gap=35.
  TargetScheduler s(35);
  assert(s.schedule("cmd-a", "a1b2c3:42:1", "A", "", 5, 200, "SA", 0, displaced, reason));
  assert(s.schedule("cmd-b", "a1b2c3:43:1", "B", "", 5, 0, "", 0, displaced, reason));
  assert(s.schedule("cmd-c", "a1b2c3:44:1", "C", "", 5, 0, "", 0, displaced, reason));
  auto t = run(s, 0, 2000);

  // Peers are NOT starved: B and C each still receive all five repeats.
  assert(count_raw(t, "B") == 5);
  assert(count_raw(t, "C") == 5);

  // A's OWN action train is truncated by its own STOP: only the repeats that
  // fired before the t=200 deadline (at t=0 and t=105) go out; the remaining
  // three action repeats are abandoned in favor of the STOP.
  assert(count_raw(t, "A") == 2);

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
    """Validation against live-hardware ground truth: three commands admitted a
    little apart (as MQTT delivers them) reproduce the on-air heard sequence
    A A B C A B C B C with started stamps at slots 0, 2, 3. The 'A A' opening
    is round-robin with one target alone in the rotation for two slots before
    the others join -- not a deviation from round-robin."""
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


def test_timed_stop_waits_for_inflight_and_truncates_only_its_own_train(
    tmp_path: Path,
) -> None:
    """Validation against live-hardware run 3: a timed command's fail-safe STOP
    is promoted ahead of queued peer ACTION work but still waits out the
    in-flight frame's physical RF occupancy, and it truncates only ITS OWN
    remaining action repeats -- never the peer's. Interleaving can push the
    timed command's later action repeats past its own stop deadline, so it
    delivers FEWER action repeats under concurrency than it would solo."""
    _compile_and_run(
        tmp_path,
        "staggered_run3",
        r"""
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

  // A's OWN action train IS truncated: interleaving delayed A's third action
  // past its 2620 ms deadline, so A delivers only two of three action repeats,
  // then the full three-copy fail-safe STOP.
  assert(count_raw(t, FX) == 2);
  assert(count_raw(t, FW) == 3);

  // A started at slot 0 (t=0), so its deadline is 0 + 2620. The first STOP goes
  // on air only after the in-flight frame (B, dispatched at slot 2 / t=2120)
  // clears at t=3180: promoted ahead of queued work, but gated by physical RF
  // occupancy. Lateness beyond the deadline is exactly that remaining occupancy.
  uint32_t first_stop = 0, second_b = 0;
  int b_seen = 0;
  for (const auto &tk : t) {
    if (tk.raw == FW && first_stop == 0)
      first_stop = tk.t;
    if (tk.raw == FY && ++b_seen == 2)
      second_b = tk.t;
  }
  assert(first_stop == 3180);
  assert(first_stop - 2620 == 560);   // waited out the in-flight frame, not longer

  // STOP priority: the first STOP copy goes on air BEFORE B's second action --
  // the promoted STOP jumps ahead of queued ACTION work.
  assert(second_b > first_stop);
""",
    )
