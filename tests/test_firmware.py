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
#include "rf433_scheduler.h"

using rf433::TargetScheduler;

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
  assert(rf433::valid_target_key("a1b2c3:42:1,2,16"));
  assert(!rf433::valid_target_key("target-a"));
  assert(!rf433::valid_target_key("a1b2c3:42:2,1"));
  assert(!rf433::valid_target_key("a1b2c3:42:0"));
  assert(!rf433::valid_target_key("a1b2c3:42:17"));

  TargetScheduler scheduler(35);
  const std::string target_a = "a1b2c3:42:1";
  const std::string target_b = "a1b2c3:42:2";
  const std::string target_c = "a1b2c3:42:3";
  const std::string target_d = "a1b2c3:42:4";
  std::string started;
  assert(scheduler.schedule("command-a", target_a, "A", "TA", 1, 100, "SA", 100));
  auto raw = scheduler.next(100, started);
  assert(raw && *raw == "A");
  assert(started == "command-a");

  // Commands for other targets may arrive after HA receives "accepted", but
  // they must not replace A's pending timed STOP.
  assert(scheduler.schedule("command-b", target_b, "B", "", 1, 0, "", 105));
  raw = scheduler.next(135, started);
  assert(raw && *raw == "TA");
  assert(started.empty());
  raw = scheduler.next(170, started);
  assert(raw && *raw == "B");
  assert(started == "command-b");
  assert(scheduler.schedule("command-c", target_c, "C", "", 1, 0, "", 175));
  assert(!scheduler.next(199, started));
  raw = scheduler.next(205, started);
  assert(raw && *raw == "SA");
  assert(started.empty());
  raw = scheduler.next(240, started);
  assert(raw && *raw == "C");
  assert(started == "command-c");

  // A due fail-safe STOP preempts unfinished action/trailer repeats.
  assert(scheduler.schedule("command-d", target_d, "D", "TD", 2, 10, "SD", 245));
  raw = scheduler.next(275, started);
  assert(raw && *raw == "D");
  assert(started == "command-d");
  raw = scheduler.next(310, started);
  assert(raw && *raw == "SD");
  assert(started.empty());
  raw = scheduler.next(345, started);
  assert(raw && *raw == "SD");
  assert(started.empty());

  assert(scheduler.schedule("command-d2", target_d, "D2", "", 1, 0, "", 380));
  assert(!scheduler.schedule("overlap", "A1B2C3:42:4,5", "OD", "", 1, 0, "", 380));
  raw = scheduler.next(380, started);
  assert(raw && *raw == "D2");
  assert(started == "command-d2");
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
    assert 'x["stop_raw"]' in package
    assert 'x["trailer_raw"]' in package
    assert 'root["command_id"]' in package
    assert 'root["status"]' in package
    assert 'root["target"]' not in package
    assert 'publish_status("accepted"' in package
    assert '"rejected"' in package
    assert ".schedule(" in package
    assert ".next(" in package
    assert 'publish_status("queued"' not in package
    assert 'publish_status("started"' in package
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
