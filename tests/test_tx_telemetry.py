"""Host coverage for command-correlated TX telemetry."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_rx_firmware import _compile_and_run

PROJECT_ROOT = Path(__file__).parents[1]
BRIDGE_YAML = PROJECT_ROOT / "rf433-mqtt-bridge.yaml"
WIRE_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "issue_19_wire_contract.json"


def test_native_tx_tallies_retention_and_observation_only(tmp_path: Path) -> None:
    """Exercise descriptors, conservation, retention, and the safety invariant."""
    _compile_and_run(
        tmp_path,
        r"""
        #include <array>
        #include <cassert>
        #include <cstdint>
        #include <string>
        #include <vector>

        #include "rf433_scheduler.h"

        using rf433::Dispatch;
        using rf433::DispatchPhase;
        using rf433::TargetScheduler;
        using rf433::TxSummary;

        struct SeenDispatch {
          std::string command_id;
          DispatchPhase phase;
          uint8_t copy_ordinal;
          std::string raw;
          std::string started;

          bool operator==(const SeenDispatch &other) const {
            return command_id == other.command_id && phase == other.phase &&
                   copy_ordinal == other.copy_ordinal && raw == other.raw &&
                   started == other.started;
          }
        };

        static std::vector<SeenDispatch> drain(TargetScheduler &scheduler, uint32_t start_ms) {
          std::vector<SeenDispatch> seen;
          for (uint32_t now_ms = start_ms; now_ms < start_ms + 20000; now_ms++) {
            std::string started;
            const auto dispatch = scheduler.next(now_ms, started);
            if (dispatch.has_value()) {
              seen.push_back({dispatch->command_id, dispatch->phase, dispatch->copy_ordinal,
                              dispatch->raw, started});
              assert(scheduler.record_handoff(*dispatch, now_ms));
            }
            if (scheduler.idle())
              break;
          }
          assert(scheduler.idle());
          return seen;
        }

        static void assert_conserved(const TxSummary &summary) {
          assert(summary.action_requested ==
                 summary.action_handoffs + summary.action_not_handed_off);
          assert(summary.trailer_requested ==
                 summary.trailer_handoffs + summary.trailer_not_handed_off);
          assert(summary.stop_requested ==
                 summary.stop_handoffs + summary.stop_not_handed_off);
          assert(!summary.has_coprocessor_outcomes);
          assert(summary.coprocessor_completions == 0);
          assert(summary.coprocessor_unknown == 0);
        }

        int main() {
          const std::array<int, 5> repeat_counts{{1, 2, 3, 7, 16}};
          for (const int repeats : repeat_counts) {
            TargetScheduler scheduler(0);
            scheduler.start_boot_session(100 + static_cast<uint32_t>(repeats));
            std::vector<std::string> displaced;
            std::string reason;
            const std::string command_id = "copies-" + std::to_string(repeats);
            assert(scheduler.schedule(command_id, "a1b2c3:42:1", "A", "T", repeats, 0,
                                      "", 0, displaced, reason));
            const auto seen = drain(scheduler, 0);
            assert(seen.size() == static_cast<size_t>(repeats * 2));
            const auto summary = scheduler.summary_for(command_id);
            assert(summary.has_value());
            assert(summary->boot_id == 100 + static_cast<uint32_t>(repeats));
            assert(summary->action_requested == repeats);
            assert(summary->action_handoffs == repeats);
            assert(summary->trailer_requested == repeats);
            assert(summary->trailer_handoffs == repeats);
            assert(summary->stop_requested == 0);
            assert(std::string(summary->terminal()) == "completed");
            assert(summary->truncation() == nullptr);
            assert_conserved(*summary);
          }

          // The tally survives removal from commands_ until the post-UART handoff.
          TargetScheduler final_handoff(0);
          final_handoff.start_boot_session(200);
          std::vector<std::string> displaced;
          std::string reason;
          std::string started;
          assert(final_handoff.schedule("last-copy", "a1b2c3:42:1", "A", "", 1, 0, "",
                                        0, displaced, reason));
          const auto last = final_handoff.next(0, started);
          assert(last.has_value() && final_handoff.idle());
          assert(!final_handoff.summary_for("last-copy").has_value());
          assert(final_handoff.record_handoff(*last, 1));
          const auto final_summary = final_handoff.summary_for("last-copy");
          assert(final_summary.has_value());
          assert(final_summary->action_handoffs == 1);
          assert(final_summary->timestamp_ms == 1);

          // Staggered admission retains per-command ordinals through round robin.
          TargetScheduler staggered(0);
          staggered.start_boot_session(201);
          assert(staggered.schedule("round-a", "a1b2c3:42:1", "A", "", 3, 0, "", 0,
                                    displaced, reason));
          auto dispatch = staggered.next(0, started);
          assert(dispatch.has_value());
          std::vector<SeenDispatch> staggered_seen{{dispatch->command_id, dispatch->phase,
                                                    dispatch->copy_ordinal, dispatch->raw,
                                                    started}};
          assert(staggered.record_handoff(*dispatch, 0));
          assert(staggered.schedule("round-b", "a1b2c3:43:1", "B", "", 3, 0, "", 1,
                                    displaced, reason));
          assert(staggered.schedule("round-c", "a1b2c3:44:1", "C", "", 3, 0, "", 2,
                                    displaced, reason));
          const auto staggered_tail = drain(staggered, 1);
          staggered_seen.insert(staggered_seen.end(), staggered_tail.begin(),
                                staggered_tail.end());
          const char *expected_ids[] = {"round-a", "round-a", "round-b", "round-c", "round-a",
                                        "round-b", "round-c", "round-b", "round-c"};
          const uint8_t expected_ordinals[] = {0, 1, 0, 0, 2, 1, 1, 2, 2};
          assert(staggered_seen.size() == 9);
          for (size_t index = 0; index < staggered_seen.size(); index++) {
            assert(staggered_seen[index].command_id == expected_ids[index]);
            assert(staggered_seen[index].copy_ordinal == expected_ordinals[index]);
            assert(staggered_seen[index].phase == DispatchPhase::ACTION);
          }

          // A deadline records the unscheduled ACTION remainder instead of replacing it.
          TargetScheduler deadline(0);
          deadline.start_boot_session(202);
          assert(deadline.schedule("deadline", "a1b2c3:42:1", "A", "", 3, 6, "S", 0,
                                   displaced, reason));
          drain(deadline, 0);
          const auto deadline_summary = deadline.summary_for("deadline");
          assert(deadline_summary.has_value());
          assert(deadline_summary->action_requested == 3);
          assert(deadline_summary->action_handoffs == 2);
          assert(deadline_summary->action_not_handed_off == 1);
          assert(deadline_summary->stop_requested == 3);
          assert(deadline_summary->stop_handoffs == 3);
          assert(std::string(deadline_summary->terminal()) == "completed");
          assert(std::string(deadline_summary->truncation()) == "deadline");
          assert_conserved(*deadline_summary);

          // Displacement drains every owed STOP before terminalizing its tally.
          TargetScheduler displaced_scheduler(0);
          displaced_scheduler.start_boot_session(203);
          assert(displaced_scheduler.schedule("old", "a1b2c3:42:1", "A", "", 3, 1000, "S",
                                              0, displaced, reason));
          dispatch = displaced_scheduler.next(0, started);
          assert(dispatch.has_value());
          assert(displaced_scheduler.record_handoff(*dispatch, 0));
          assert(displaced_scheduler.schedule("new", "a1b2c3:42:1", "N", "", 1, 0, "", 1,
                                              displaced, reason));
          assert(displaced == std::vector<std::string>({"old"}));
          drain(displaced_scheduler, 1);
          const auto displaced_summary = displaced_scheduler.summary_for("old");
          assert(displaced_summary.has_value());
          assert(displaced_summary->action_handoffs == 1);
          assert(displaced_summary->action_not_handed_off == 2);
          assert(displaced_summary->stop_handoffs == 3);
          assert(std::string(displaced_summary->terminal()) == "displaced");
          assert(std::string(displaced_summary->truncation()) == "displaced");
          assert_conserved(*displaced_summary);

          TargetScheduler disarmed(0);
          disarmed.start_boot_session(204);
          assert(disarmed.schedule("disarmed", "a1b2c3:42:1", "A", "T", 3, 1000, "S", 0,
                                   displaced, reason));
          dispatch = disarmed.next(0, started);
          assert(dispatch.has_value());
          assert(disarmed.record_handoff(*dispatch, 0));
          disarmed.disarm("disarmed", 2);
          const auto disarmed_summary = disarmed.summary_for("disarmed");
          assert(disarmed_summary.has_value());
          assert(disarmed_summary->action_handoffs == 1);
          assert(disarmed_summary->trailer_handoffs == 0);
          assert(disarmed_summary->stop_handoffs == 0);
          assert(std::string(disarmed_summary->terminal()) == "disarmed");
          assert(std::string(disarmed_summary->truncation()) == "disarmed");
          assert_conserved(*disarmed_summary);

          // The OTA fallback clears scheduler containers before its emergency
          // STOP write; the detached descriptor still completes the tally.
          TargetScheduler ota_drain(0);
          ota_drain.start_boot_session(205);
          assert(ota_drain.schedule("ota", "a1b2c3:42:1", "A", "", 3, 1000, "S", 0,
                                    displaced, reason));
          dispatch = ota_drain.next(0, started);
          assert(dispatch.has_value());
          assert(ota_drain.record_handoff(*dispatch, 0));
          const auto emergency_stops = ota_drain.drain_armed_stops(10);
          assert(emergency_stops.size() == 1);
          assert(ota_drain.idle());
          assert(!ota_drain.summary_for("ota").has_value());
          assert(ota_drain.record_handoff(emergency_stops[0], 11));
          const auto ota_summary = ota_drain.summary_for("ota");
          assert(ota_summary.has_value());
          assert(ota_summary->action_handoffs == 1);
          assert(ota_summary->action_not_handed_off == 2);
          assert(ota_summary->stop_handoffs == 1);
          assert(ota_summary->stop_not_handed_off == 2);
          assert(std::string(ota_summary->terminal()) == "disarmed");
          assert_conserved(*ota_summary);

          // Sixteen terminal truths survive both an MQTT outage and enough unrelated
          // lifecycle churn to evict their primary dedup-ring slots.
          TargetScheduler outage(0);
          outage.start_boot_session(300);
          const char *hex = "0123456789ABCDEF";
          for (int index = 0; index < 16; index++) {
            const std::string target = "abcde" + std::string(1, hex[index]) + ":40:1";
            assert(outage.schedule("burst-" + std::to_string(index), target, "A", "", 1, 0,
                                   "", 0, displaced, reason));
          }
          assert(drain(outage, 0).size() == 16);
          assert(outage.pending_summary_count() == 16);
          assert(outage.flush_summaries([](const TxSummary &) { return false; }) == 0);

          for (int index = 0; index < 16; index++) {
            const std::string target = "bbcde" + std::string(1, hex[index]) + ":50:1";
            assert(outage.schedule("active-" + std::to_string(index), target, "A", "", 1, 0,
                                   "", 100, displaced, reason));
          }
          for (int index = 0; index < 64; index++) {
            assert(!outage.schedule("rejected-" + std::to_string(index), "cccccc:60:1", "A",
                                    "", 1, 0, "", 101, displaced, reason));
            assert(reason == "target scheduler is full");
          }
          assert(outage.pending_summary_count() == 16);
          assert(outage.summary_dropped_count() == 0);
          assert(outage.diagnostics().summary_cache_drops == 0);
          uint32_t retained_replay_age_ms = 0;
          bool retained_replay_has_age = false;
          assert(outage.replay_state("burst-0", 500, retained_replay_age_ms,
                                     &retained_replay_has_age) == 2);
          assert(retained_replay_has_age);
          assert(retained_replay_age_ms == 500);
          assert(!outage.schedule("burst-0", "dddddd:70:1", "A", "", 1, 0, "", 500,
                                  displaced, reason));
          assert(reason == "duplicate command_id");

          std::vector<TxSummary> published;
          assert(outage.flush_summaries([&](const TxSummary &summary) {
                   published.push_back(summary);
                   return true;
                 }) == 16);
          assert(published.size() == 16);
          assert(outage.pending_summary_count() == 0);
          const auto immutable = outage.summary_for("burst-0");
          assert(immutable.has_value());
          const uint32_t summarized_before_replay = outage.diagnostics().summarized;
          assert(outage.replay_summary("burst-0", [&](const TxSummary &summary) {
            published.push_back(summary);
            return true;
          }));
          assert(published.back().command_id == immutable->command_id);
          assert(published.back().action_handoffs == immutable->action_handoffs);
          assert(outage.diagnostics().summarized == summarized_before_replay);

          // Boot-scoped counters and both summary stores reset together.
          outage.start_boot_session(301);
          assert(outage.diagnostics().boot_id == 301);
          assert(outage.diagnostics().admitted == 0);
          assert(outage.diagnostics().started == 0);
          assert(outage.diagnostics().summarized == 0);
          assert(outage.cached_summary_count() == 0);
          assert(outage.pending_summary_count() == 0);

          rf433::DiagnosticsPublishGate diagnostics_gate;
          assert(diagnostics_gate.due(0));
          diagnostics_gate.note_published(100);
          assert(!diagnostics_gate.due(60099));
          assert(diagnostics_gate.due(60100));
          diagnostics_gate.note_published(UINT32_MAX - 10U);
          assert(!diagnostics_gate.due(UINT32_MAX - 9U));
          assert(diagnostics_gate.due((UINT32_MAX - 10U) + rf433::DIAGNOSTICS_INTERVAL_MS));

          // The fixed telemetry footprint preserves at least a 27 KiB projected
          // watermark against the measured 30 KiB low end on ESP8285.
          constexpr size_t measured_free_heap_floor = 30 * 1024;
          static_assert(rf433::TX_TELEMETRY_FIXED_STORAGE_BYTES <= 3 * 1024);
          static_assert(measured_free_heap_floor - rf433::TX_TELEMETRY_FIXED_STORAGE_BYTES >=
                        27 * 1024);

          // HARD SAFETY INVARIANT: publishing telemetry versus retaining it changes
          // no dispatch, phase, ordinal, started marker, repeat count, or ordering.
          TargetScheduler emitting(0);
          TargetScheduler retaining(0);
          emitting.start_boot_session(400);
          retaining.start_boot_session(400);
          assert(emitting.schedule("safe-a", "d1d1d1:42:1", "A", "", 3, 12, "S", 0,
                                   displaced, reason));
          assert(retaining.schedule("safe-a", "d1d1d1:42:1", "A", "", 3, 12, "S", 0,
                                    displaced, reason));
          assert(emitting.schedule("safe-b", "d1d1d1:43:1", "B", "T", 2, 0, "", 0,
                                   displaced, reason));
          assert(retaining.schedule("safe-b", "d1d1d1:43:1", "B", "T", 2, 0, "", 0,
                                    displaced, reason));
          std::vector<SeenDispatch> emitting_seen;
          std::vector<SeenDispatch> retaining_seen;
          size_t emitted_summaries = 0;
          for (uint32_t now_ms = 0; now_ms < 1000; now_ms++) {
            std::string emitting_started;
            std::string retaining_started;
            const auto emitted_dispatch = emitting.next(now_ms, emitting_started);
            const auto retained_dispatch = retaining.next(now_ms, retaining_started);
            assert(emitted_dispatch.has_value() == retained_dispatch.has_value());
            if (emitted_dispatch.has_value()) {
              emitting_seen.push_back({emitted_dispatch->command_id, emitted_dispatch->phase,
                                       emitted_dispatch->copy_ordinal, emitted_dispatch->raw,
                                       emitting_started});
              retaining_seen.push_back({retained_dispatch->command_id, retained_dispatch->phase,
                                        retained_dispatch->copy_ordinal, retained_dispatch->raw,
                                        retaining_started});
              assert(emitting.record_handoff(*emitted_dispatch, now_ms));
              assert(retaining.record_handoff(*retained_dispatch, now_ms));
            }
            emitting.flush_summaries([&](const TxSummary &) {
              emitted_summaries++;
              return true;
            });
            retaining.flush_summaries([](const TxSummary &) { return false; });
            if (emitting.idle() && retaining.idle())
              break;
          }
          assert(emitted_summaries == 2);
          assert(emitting_seen == retaining_seen);
          assert(emitting.diagnostics().action_handoffs == retaining.diagnostics().action_handoffs);
          assert(emitting.diagnostics().trailer_handoffs ==
                 retaining.diagnostics().trailer_handoffs);
          assert(emitting.diagnostics().stop_handoffs == retaining.diagnostics().stop_handoffs);
          return 0;
        }
        """,
    )


def test_wire_fixture_and_package_pin_stage_1a_contract() -> None:
    """Pin the additive payloads, capability levels, and existing status topic."""
    fixture = json.loads(WIRE_FIXTURE.read_text())
    info = fixture["info"]
    assert info["topic"] == "rf433/fixture-bridge/info"
    assert info["retain"] is True
    assert type(info["payload"]["caps"]["tx_summary"]) is int
    assert info["payload"]["caps"]["tx_summary"] == 1
    assert type(info["payload"]["caps"]["a0_complete"]) is int
    assert info["payload"]["caps"]["a0_complete"] == 0

    for event in fixture["tx_summaries"]:
        assert event["topic"] == "rf433/fixture-bridge/status"
        assert event["payload"]["status"] == "tx_summary"
        for phase in ("action", "trailer", "stop"):
            assert event["payload"][f"{phase}_requested"] == (
                event["payload"][f"{phase}_handoffs"] + event["payload"][f"{phase}_not_handed_off"]
            )
        assert "coprocessor_completions" not in event["payload"]
        assert "coprocessor_unknown" not in event["payload"]

    deadline = fixture["tx_summaries"][1]["payload"]
    expected_requested = 3
    expected_handoffs = 2
    assert deadline["action_requested"] == expected_requested
    assert deadline["action_handoffs"] == expected_handoffs
    assert deadline["action_not_handed_off"] == 1
    assert deadline["truncation"] == "deadline"

    diagnostics = fixture["diagnostics"]
    assert diagnostics["topic"] == "rf433/fixture-bridge/diagnostics"
    assert diagnostics["retain"] is True
    expected_boot = 123
    assert diagnostics["payload"]["boot"] == expected_boot
    assert diagnostics["payload"]["coprocessor_completions"] == 0
    assert diagnostics["payload"]["coprocessor_unknown"] == 0

    package = BRIDGE_YAML.read_text()
    assert 'root["caps"]["tx_summary"] = 1;' in package
    assert 'root["caps"]["a0_complete"] = 0;' in package
    expected_status_publishers = 4
    assert package.count('"rf433/${bridge_id}/status"') >= expected_status_publishers
    assert 'root["status"] = "tx_summary";' in package
    assert '"rf433/${bridge_id}/diagnostics"' in package
    assert "diagnostics_gate.note_published(now_ms);" in package
    assert "DIAGNOSTICS_INTERVAL_MS = 60000" in (PROJECT_ROOT / "rf433_scheduler.h").read_text()
