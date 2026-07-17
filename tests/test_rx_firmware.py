"""Host-side checks for the Phase-2 receive path and firmware contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
BRIDGE_YAML = PROJECT_ROOT / "rf433-mqtt-bridge.yaml"
RX_HEADER = PROJECT_ROOT / "rf433_rx.h"
RF_BRIDGE_DIR = PROJECT_ROOT / "components" / "rf_bridge"
RF_BRIDGE_PROTOCOL = RF_BRIDGE_DIR / "rf_bridge_protocol.h"


def _compile_and_run(tmp_path: Path, source_text: str) -> None:
    """Compile and execute one dependency-free C++17 firmware unit."""
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("a host C++ compiler is required")
    source = tmp_path / "test.cpp"
    binary = tmp_path / "test"
    source.write_text(textwrap.dedent(source_text))
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(tmp_path),
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


def _write_rf_bridge_stubs(tmp_path: Path) -> None:
    """Provide the small ESPHome surface needed to host-test the vendored parser."""
    stubs = {
        "esphome/core/component.h": r"""
            #pragma once

            #include <functional>
            #include <string>
            #include <utility>
            #include <vector>

            namespace esphome {

            class Component {
             public:
              virtual ~Component() = default;
              virtual void setup() {}
              virtual void loop() {}
              virtual void dump_config() {}
            };

            template<typename Signature> class CallbackManager;

            template<typename... Args> class CallbackManager<void(Args...)> {
             public:
              template<typename F> void add(F &&callback) {
                this->callbacks_.emplace_back(std::forward<F>(callback));
              }

              void call(Args... args) {
                for (auto &callback : this->callbacks_)
                  callback(args...);
              }

             private:
              std::vector<std::function<void(Args...)>> callbacks_;
            };

            }  // namespace esphome
        """,
        "esphome/components/uart/uart.h": r"""
            #pragma once

            #include <algorithm>
            #include <cstddef>
            #include <cstdint>
            #include <deque>
            #include <vector>

            namespace esphome::uart {

            class UARTDevice {
             public:
              size_t available() const { return this->input_.size(); }

              bool read_array(uint8_t *buffer, size_t length) {
                if (length > this->input_.size())
                  return false;
                for (size_t index = 0; index < length; index++) {
                  buffer[index] = this->input_.front();
                  this->input_.pop_front();
                }
                return true;
              }

              void write(uint8_t byte) { this->output_.push_back(byte); }
              void flush() {}
              void check_uart_settings(uint32_t) {}

              void feed_uart(const std::vector<uint8_t> &bytes) {
                this->input_.insert(this->input_.end(), bytes.begin(), bytes.end());
              }

              const std::vector<uint8_t> &written_bytes() const { return this->output_; }

             private:
              std::deque<uint8_t> input_;
              std::vector<uint8_t> output_;
            };

            }  // namespace esphome::uart
        """,
        "esphome/core/automation.h": r"""
            #pragma once

            namespace esphome {

            template<typename... Ts> class Action {
             public:
              virtual ~Action() = default;
            };

            template<typename T> class TemplatableValue {
             public:
              template<typename... Ts> T value(const Ts &...) const { return T{}; }
            };

            }  // namespace esphome

            #define TEMPLATABLE_VALUE(type, name) \
              ::esphome::TemplatableValue<type> name##_;
        """,
        "esphome/core/application.h": r"""
            #pragma once

            #include <cstdint>

            namespace esphome {

            class Application {
             public:
              uint32_t get_loop_component_start_time() const { return this->now_ms_; }
              void set_loop_component_start_time(uint32_t now_ms) { this->now_ms_ = now_ms; }

             private:
              uint32_t now_ms_{0};
            };

            inline Application App;

            }  // namespace esphome
        """,
        "esphome/core/helpers.h": r"""
            #pragma once

            #include <cstddef>
            #include <cstdio>

            namespace esphome {

            template<typename... Args>
            void buf_append_printf(char *buffer, size_t buffer_size, size_t offset,
                                   const char *format, Args... args) {
              if (offset < buffer_size)
                std::snprintf(buffer + offset, buffer_size - offset, format, args...);
            }

            }  // namespace esphome
        """,
        "esphome/core/log.h": r"""
            #pragma once

            #include <utility>

            namespace esphome {

            template<typename... Args>
            void host_test_log(const char *, const char *, Args &&...) {}

            }  // namespace esphome

            #define ESP_LOGD(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
            #define ESP_LOGI(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
            #define ESP_LOGV(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
            #define ESP_LOGVV(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
            #define ESP_LOGW(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
            #define ESP_LOGCONFIG(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
        """,
    }
    for relative_path, contents in stubs.items():
        stub = tmp_path / relative_path
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(textwrap.dedent(contents))


def _firmware_lambda(section_start: str, section_end: str) -> str:
    """Extract an ESPHome lambda body so host tests execute the shipped path."""
    package = BRIDGE_YAML.read_text()
    section = package.split(section_start, maxsplit=1)[1].split(section_end, maxsplit=1)[0]
    body = section.split("- lambda: |-", maxsplit=1)[1]
    substitutions = {
        "${bridge_id}": "test-bridge",
        "${bridge_area}": "test-area",
        "${default_bridge}": "false",
        "${repeat_gap_ms}": "35",
    }
    for key, value in substitutions.items():
        body = body.replace(key, value)
    return textwrap.dedent(body).strip()


def test_generated_rx_callback_publishes_only_during_sniff_without_tx(
    tmp_path: Path,
) -> None:
    """The shipped callback follows physical RX state and cannot enter TX."""
    _write_rf_bridge_stubs(tmp_path)
    rx_lambda = _firmware_lambda("on_bucket_received:", "\n\n# The per-bridge scheduler")
    source = (
        r"""
        #include <algorithm>
        #include <cassert>
        #include <cstddef>
        #include <cstdint>
        #include <map>
        #include <string>
        #include <vector>

        #include "rf433_rx.h"
        #include "components/rf_bridge/rf_bridge.cpp"

        using esphome::rf_bridge::RFBridgeComponent;

        struct JsonSlot {
          std::map<std::string, std::string> *values;
          std::string key;

          JsonSlot &operator=(const std::string &value) {
            (*this->values)[this->key] = value;
            return *this;
          }

          JsonSlot &operator=(uint32_t value) {
            (*this->values)[this->key] = std::to_string(value);
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
          bool enqueue_success{true};
          size_t attempts{0};
          std::vector<Message> messages;

          template<typename F>
          bool publish_json(const std::string &topic, F &&builder, int qos, bool retained) {
            this->attempts++;
            Message message{topic, {}, qos, retained};
            builder(JsonObject{&message.payload});
            if (!this->enqueue_success)
              return false;
            this->messages.push_back(std::move(message));
            return true;
          }
        } mqtt_client;

        uint32_t fake_now_ms{0};
        uint32_t boot_id{424242};
        uint32_t millis() { return fake_now_ms; }

        #define id(value) value

        void generated_on_bucket_received(const std::string &data) {
        """
        + rx_lambda
        + r"""
        }

        static std::vector<uint8_t> aok_frame(bool physical) {
          // TODO(hardware): synthesized from parser assumptions until real OEM
          // captures are available.
          std::vector<uint8_t> frame{
              0xAA, 0xB1, 0x04, 0x14, 0x14, 0x02, 0x6C, 0x01, 0x55, 0x14, 0x14,
              0xB3, 0x38,
          };
          for (size_t bit = 0; bit < 65; bit++)
            frame.push_back(0x1A);
          if (physical) {
            frame[13] = 0x19;
            frame[14] = 0x2A;
          }
          frame.push_back(0x19);
          frame.push_back(0x38);
          frame.push_back(0x55);
          return frame;
        }

        static void capture(RFBridgeComponent &bridge, bool physical) {
          bridge.feed_uart(aok_frame(physical));
          bridge.loop();
        }

        int main() {
          RFBridgeComponent bridge;
          bridge.add_on_bucket_received_callback(
              [](const std::string &data) { generated_on_bucket_received(data); });

          // A capture completed while the physical publish gate is off is
          // parsed and dropped without publishing.
          fake_now_ms = 10;
          capture(bridge, false);
          assert(mqtt_client.attempts == 0);

          rf433::rx_state().start_sniff(60, 0);
          rf433::rx_state().set_radio_sniffing(true);
          fake_now_ms = 100;
          capture(bridge, false);
          capture(bridge, true);
          assert(mqtt_client.attempts == 2);
          assert(mqtt_client.messages.size() == 2);
          assert(mqtt_client.messages[0].payload.at("frame") !=
                 mqtt_client.messages[1].payload.at("frame"));
          // Plain millis() is intentionally allowed for same-loop captures.
          assert(mqtt_client.messages[0].payload.at("t") == "100");
          assert(mqtt_client.messages[1].payload.at("t") == "100");

          mqtt_client.enqueue_success = false;
          fake_now_ms = 200;
          capture(bridge, false);
          mqtt_client.enqueue_success = true;
          capture(bridge, true);
          assert(mqtt_client.attempts == 4);
          assert(mqtt_client.messages.size() == 3);
          assert(mqtt_client.messages.back().payload.at("t") == "200");

          rf433::rx_state().start_sniff(0, 201);
          rf433::rx_state().set_radio_sniffing(false);
          capture(bridge, false);
          assert(mqtt_client.attempts == 4);

          rf433::rx_state().start_sniff(1, 1000);
          rf433::rx_state().set_radio_sniffing(true);
          fake_now_ms = 1999;
          capture(bridge, true);
          assert(mqtt_client.attempts == 5);
          fake_now_ms = 2000;
          assert(rf433::rx_state().tick(2000));
          rf433::rx_state().set_radio_sniffing(false);
          capture(bridge, false);
          assert(mqtt_client.attempts == 5);
          assert(!rf433::rx_state().tick(2001));

          for (const Message &message : mqtt_client.messages) {
            assert(message.topic == "rf433/test-bridge/rx");
            assert(message.payload.at("boot") == "424242");
            assert(message.qos == 1);
            assert(!message.retained);
          }

          // Eight completed B1 frames write nothing back to the coprocessor:
          // no B0 TX, and no ACKs either — an ACK would make Portisch revert
          // to standard sniffing via its stale last_sniffing_command.
          assert(bridge.written_bytes().empty());
          return 0;
        }
        """
    )
    _compile_and_run(tmp_path, source)


def test_generated_cmd_handler_delegates_sniff_and_disarms_without_tx(
    tmp_path: Path,
) -> None:
    """The MQTT handler sets sniff intent and acknowledges RF-silent disarm."""
    cmd_lambda = _firmware_lambda("- topic: rf433/${bridge_id}/cmd", "\nglobals:")
    source = (
        r"""
        #include <algorithm>
        #include <cassert>
        #include <cctype>
        #include <cstdint>
        #include <map>
        #include <string>
        #include <type_traits>
        #include <variant>
        #include <vector>

        #include "rf433_rx.h"

        namespace rf433 {

        inline bool valid_key(const std::string &value) {
          if (value.empty() || value.size() > 64)
            return false;
          return std::all_of(value.begin(), value.end(), [](char character) {
            const auto byte = static_cast<unsigned char>(character);
            return std::isalnum(byte) || character == '-' || character == '_' ||
                   character == '.' || character == ':' || character == ',';
          });
        }

        struct FakeScheduler {
          std::vector<std::string> disarmed_ids;
          void disarm(const std::string &command_id) {
            this->disarmed_ids.push_back(command_id);
          }
        };

        inline FakeScheduler &tx_scheduler(uint32_t) {
          static FakeScheduler scheduler;
          return scheduler;
        }

        }  // namespace rf433

        struct JsonValueData {
          std::variant<std::monostate, std::string, int, bool> value;
        };

        struct JsonValue {
          const JsonValueData *data;

          template<typename T> bool is() const {
            if (this->data == nullptr)
              return false;
            if constexpr (std::is_same_v<T, const char *>)
              return std::holds_alternative<std::string>(this->data->value);
            if constexpr (std::is_same_v<T, int>)
              return std::holds_alternative<int>(this->data->value);
            if constexpr (std::is_same_v<T, bool>)
              return std::holds_alternative<bool>(this->data->value);
            return false;
          }

          template<typename T> T as() const {
            if constexpr (std::is_same_v<T, std::string>)
              return std::get<std::string>(this->data->value);
            if constexpr (std::is_same_v<T, int>)
              return std::get<int>(this->data->value);
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
          void set_bool(const std::string &key, bool value) {
            this->values[key].value = value;
          }
        };

        struct FakeBridge {
          std::vector<std::string> actions;
          void start_bucket_sniffing() {
            this->actions.push_back("B1");
          }
          void stop_advanced_sniffing() {
            this->actions.push_back("A7");
          }
        } portisch_rf_bridge;

        struct JsonSlot {
          std::map<std::string, std::string> *values;
          std::string key;

          JsonSlot &operator=(const std::string &value) {
            (*this->values)[this->key] = value;
            return *this;
          }

          JsonSlot &operator=(uint32_t value) {
            (*this->values)[this->key] = std::to_string(value);
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
          std::vector<Message> messages;

          template<typename F>
          bool publish_json(const std::string &topic, F &&builder, int qos, bool retained) {
            Message message{topic, {}, qos, retained};
            builder(JsonObject{&message.payload});
            this->messages.push_back(std::move(message));
            return true;
          }
        } mqtt_client;

        uint32_t fake_now_ms{0};
        uint32_t boot_id{5150};
        uint32_t millis() { return fake_now_ms; }

        #define ESP_LOGW(...) ((void) 0)
        #define ESP_LOGI(...) ((void) 0)
        #define id(value) value

        void generated_cmd_handler(const FakeJson &x) {
        """
        + cmd_lambda
        + r"""
        }

        static FakeJson sniff_command(const std::string &action, int seconds) {
          FakeJson value;
          value.set_string("action", action);
          value.set_int("seconds", seconds);
          return value;
        }

        static FakeJson disarm_command(const std::string &command_id) {
          FakeJson value;
          value.set_string("action", "disarm");
          value.set_string("command_id", command_id);
          return value;
        }

        int main() {
          fake_now_ms = 1;
          generated_cmd_handler(sniff_command("listen", 30));
          assert(portisch_rf_bridge.actions.empty());
          assert(!rf433::rx_state().should_publish());

          FakeJson missing_seconds;
          missing_seconds.set_string("action", "sniff");
          generated_cmd_handler(missing_seconds);
          FakeJson wrong_type;
          wrong_type.set_string("action", "sniff");
          wrong_type.set_bool("seconds", true);
          generated_cmd_handler(wrong_type);
          generated_cmd_handler(sniff_command("sniff", -1));
          assert(portisch_rf_bridge.actions.empty());

          // The value is hard-capped at 60. The handler records intent only;
          // the interval reconciler owns all physical B1/A7 transitions.
          fake_now_ms = 100;
          generated_cmd_handler(sniff_command("sniff", 61));
          assert(portisch_rf_bridge.actions.empty());
          assert(!rf433::rx_state().should_publish());
          assert(rf433::rx_state().bounded_active(60099));
          assert(!rf433::rx_state().bounded_active(60100));

          fake_now_ms = 101;
          generated_cmd_handler(sniff_command("sniff", 30));
          assert(portisch_rf_bridge.actions.empty());

          // Cancellation wins inside the positive-command rate-limit window.
          fake_now_ms = 102;
          generated_cmd_handler(sniff_command("sniff", 0));
          assert(portisch_rf_bridge.actions.empty());
          assert(!rf433::rx_state().bounded_active(102));
          assert(!rf433::rx_state().should_publish());

          fake_now_ms = 200;
          generated_cmd_handler(sniff_command("sniff", 30));
          assert(!rf433::rx_state().bounded_active(200));
          fake_now_ms = 350;
          generated_cmd_handler(sniff_command("sniff", 30));
          assert(portisch_rf_bridge.actions.empty());
          assert(rf433::rx_state().bounded_active(30349));
          assert(!rf433::rx_state().bounded_active(30350));

          // An accepted shorter extension never shortens the intent window.
          fake_now_ms = 600;
          generated_cmd_handler(sniff_command("sniff", 1));
          assert(portisch_rf_bridge.actions.empty());
          assert(rf433::rx_state().bounded_active(30349));
          assert(!rf433::rx_state().bounded_active(30350));

          FakeJson missing_command_id;
          missing_command_id.set_string("action", "disarm");
          generated_cmd_handler(missing_command_id);
          generated_cmd_handler(disarm_command("invalid key"));
          assert(rf433::tx_scheduler(35).disarmed_ids.empty());
          assert(mqtt_client.messages.empty());

          // Disarm bypasses the sniff rate limiter, delegates to the scheduler,
          // publishes an idempotent acknowledgement, and has no TX surface.
          fake_now_ms = 601;
          generated_cmd_handler(disarm_command("move:42"));
          assert(rf433::tx_scheduler(35).disarmed_ids ==
                 std::vector<std::string>({"move:42"}));
          assert(mqtt_client.messages.size() == 1);
          const Message &ack = mqtt_client.messages.front();
          assert(ack.topic == "rf433/test-bridge/status");
          assert(ack.payload.at("status") == "disarmed");
          assert(ack.payload.at("command_id") == "move:42");
          assert(ack.payload.at("t") == "601");
          assert(ack.payload.at("boot") == "5150");
          assert(ack.qos == 1);
          assert(!ack.retained);
          assert(portisch_rf_bridge.actions.empty());

          generated_cmd_handler(sniff_command("sniff", 0));
          assert(!rf433::rx_state().bounded_active(601));
          assert(portisch_rf_bridge.actions.empty());
          return 0;
        }
        """
    )
    _compile_and_run(tmp_path, source)


def test_b1_parser_uses_aok_envelope_offsets_and_preserves_interior_stop_bytes(
    tmp_path: Path,
) -> None:
    """Only an envelope-valid stop at a declared AOK offset ends a B1 capture."""
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstddef>
        #include <cstdint>
        #include <string>
        #include <vector>

        // Arduino's Print.h defines HEX as a numeric macro. The protocol
        // helper must remain valid in that firmware include environment.
        #define HEX 16
        #include "components/rf_bridge/rf_bridge_protocol.h"

        using esphome::rf_bridge::B1FrameStatus;
        using esphome::rf_bridge::b1_frame_status;
        using esphome::rf_bridge::compact_hex;
        using esphome::rf_bridge::is_aok_bucket_frame;

        static std::vector<uint8_t> aok_frame(bool leading_padding, bool trailing_padding) {
          // Four Portisch buckets: high sync, long bit, short bit, low sync.
          // The short bucket is deliberately 0x0155: its interior 0x55 byte
          // must never be mistaken for the B1 trailer.
          std::vector<uint8_t> frame{
              0xAA, 0xB1, 0x04, 0x14, 0x14, 0x02, 0x6C, 0x01, 0x55, 0x14, 0x14,
          };
          if (leading_padding)
            frame.push_back(0xB3);  // high/low idle pair before the real sync
          frame.push_back(0x38);  // low/high sync
          for (size_t bit = 0; bit < 65; bit++)
            frame.push_back(0x1A);  // payload ones plus OEM trailer bit 1
          frame.push_back(0x19);  // OEM trailer bit 0
          if (trailing_padding)
            frame.push_back(0x38);  // bounded post-frame idle/sync pair
          frame.push_back(0x55);
          return frame;
        }

        int main() {
          const auto nominal = aok_frame(false, false);
          assert(nominal.size() == 79);  // 3 header + 8 buckets + 67 pulses + trailer
          assert(b1_frame_status(nominal) == B1FrameStatus::CANDIDATE);
          assert(is_aok_bucket_frame(nominal));
          assert(compact_hex(nominal).substr(0, 4) == "AAB1");
          assert(compact_hex(nominal).substr(12, 6) == "6C0155");
          assert(compact_hex(nominal).substr(compact_hex(nominal).size() - 2) == "55");
          assert(compact_hex(nominal).find(' ') == std::string::npos);

          // Every prefix through the interior 0x55 bucket byte is incomplete.
          // Stock ESPHome incorrectly completes the frame at that byte.
          for (size_t size = 1; size <= 9; size++) {
            const std::vector<uint8_t> prefix(nominal.begin(), nominal.begin() + size);
            assert(b1_frame_status(prefix) == B1FrameStatus::INCOMPLETE);
          }

          const auto leading = aok_frame(true, false);
          const auto trailing = aok_frame(false, true);
          const auto both = aok_frame(true, true);
          assert(leading.size() == 80 && is_aok_bucket_frame(leading));
          assert(trailing.size() == 80 && is_aok_bucket_frame(trailing));
          assert(both.size() == 81 && is_aok_bucket_frame(both));
          assert(b1_frame_status(leading) == B1FrameStatus::CANDIDATE);
          assert(b1_frame_status(trailing) == B1FrameStatus::CANDIDATE);
          assert(b1_frame_status(both) == B1FrameStatus::COMPLETE);

          // An interior 0x55 in the pulse stream is not a terminal candidate
          // before the minimum 67-byte AOK envelope has arrived.
          auto pulse_stop = nominal;
          pulse_stop[20] = 0x55;
          const std::vector<uint8_t> pulse_prefix(pulse_stop.begin(), pulse_stop.begin() + 21);
          assert(b1_frame_status(pulse_prefix) == B1FrameStatus::INCOMPLETE);
          assert(b1_frame_status(pulse_stop) == B1FrameStatus::INCOMPLETE);

          // A 0x55 at the first valid AOK boundary can itself encode two
          // legitimate sync-duration padding pulses. It remains a candidate;
          // appending the real UART trailer moves the candidate to the exact
          // longer envelope instead of truncating at the interior byte.
          std::vector<uint8_t> boundary{
              0xAA, 0xB1, 0x06,
              0x14, 0x14, 0x02, 0x6C, 0x01, 0x55,
              0x14, 0x14, 0x01, 0x00, 0x14, 0x14,
          };
          boundary.push_back(0x38);
          for (size_t bit = 0; bit < 65; bit++)
            boundary.push_back(0x1A);
          boundary.push_back(0x19);
          boundary.push_back(0x55);  // legal trailing padding via sync bucket 5
          assert(is_aok_bucket_frame(boundary));
          assert(b1_frame_status(boundary) == B1FrameStatus::CANDIDATE);
          boundary.push_back(0x55);  // actual UART trailer
          assert(is_aok_bucket_frame(boundary));
          assert(b1_frame_status(boundary) == B1FrameStatus::CANDIDATE);

          auto bad_count = nominal;
          bad_count[2] = 0x02;
          assert(b1_frame_status(bad_count) == B1FrameStatus::INVALID);
          auto bad_sync = nominal;
          bad_sync[9] = 0x02;
          bad_sync[10] = 0x00;
          assert(b1_frame_status(bad_sync) == B1FrameStatus::INVALID);
          auto missing_trailer = nominal;
          missing_trailer.back() = 0x54;
          assert(b1_frame_status(missing_trailer) == B1FrameStatus::INCOMPLETE);
          missing_trailer.push_back(0x54);
          missing_trailer.push_back(0x54);
          assert(b1_frame_status(missing_trailer) == B1FrameStatus::INVALID);
          return 0;
        }
        """,
    )


def test_b1_parser_accepts_oem_truncated_trailer_capture(tmp_path: Path) -> None:
    """A real OEM capture with 65 bit pairs is accepted end to end.

    The office remote (5cad7c:da) transmits 64 payload bits plus a trailer
    that captures as a single 0-read instead of the nominal [1, 0] — every
    press was rejected until this tolerance (live-captured 2026-07-17, decodes
    to the remote's calibrated ALL/UP command). Payload-only 64-pair frames
    and a lone trailer 1-bit remain rejected: neither occurs on air.
    """
    _write_rf_bridge_stubs(tmp_path)
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstddef>
        #include <cstdint>
        #include <string>
        #include <vector>

        #include "components/rf_bridge/rf_bridge.cpp"

        using esphome::App;
        using esphome::rf_bridge::B1FrameStatus;
        using esphome::rf_bridge::b1_frame_status;
        using esphome::rf_bridge::RFBridgeComponent;
        using esphome::rf_bridge::is_aok_bucket_frame;

        static std::vector<uint8_t> from_hex(const std::string &hex) {
          std::vector<uint8_t> raw;
          for (size_t index = 0; index + 1 < hex.size(); index += 2)
            raw.push_back(static_cast<uint8_t>(
                std::stoul(hex.substr(index, 2), nullptr, 16)));
          return raw;
        }

        int main() {
          // Live OEM capture: office remote 5cad7c:da, ALL channels, UP
          // (cmd f4bb) — 65 bit pairs, truncated trailer.
          const std::string real =
              "AAB10413EC026C012C143C38192A192A1A1A19292A192A192A1A192A192A1A1A"
              "1A1A19292A1A192A1A192A192A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A"
              "192A19292A192A1A1A192A1A1955";
          const std::vector<uint8_t> frame = from_hex(real);
          assert(is_aok_bucket_frame(frame));
          assert(b1_frame_status(frame) == B1FrameStatus::CANDIDATE);

          RFBridgeComponent bridge;
          size_t published = 0;
          std::string last;
          bridge.add_on_bucket_received_callback([&](const std::string &data) {
            published++;
            last = data;
          });
          App.set_loop_component_start_time(10);
          bridge.feed_uart(frame);
          bridge.loop();
          assert(published == 0);  // CANDIDATE resolves only at UART quiet
          App.set_loop_component_start_time(17);
          bridge.loop();
          assert(published == 1);
          assert(last == real);

          // Payload-only (64 pairs) stays rejected.
          std::vector<uint8_t> no_trailer = frame;
          no_trailer.erase(no_trailer.end() - 2);
          assert(!is_aok_bucket_frame(no_trailer));
          App.set_loop_component_start_time(100);
          bridge.feed_uart(no_trailer);
          bridge.loop();
          App.set_loop_component_start_time(400);
          bridge.loop();
          assert(published == 1);

          // A lone trailer 1-bit cannot terminate a capture: rejected.
          std::vector<uint8_t> lone_one = frame;
          lone_one[lone_one.size() - 2] = 0x1A;
          assert(!is_aok_bucket_frame(lone_one));
          return 0;
        }
        """,
    )


def test_vendored_parser_never_acks_received_frames_and_bounds_advanced(tmp_path: Path) -> None:
    """Received frames are never ACKed back to the EFM8BB1.

    Portisch deliveries are fire-and-forget (RF_Bridge_main.c clears
    RF_DATA_STATUS and re-enables the capture interrupt immediately after
    uart_put_RF_buckets), while a host ACK is consumed by its RF_CODE_ACK
    handler as PCA0_DoSniffing(last_sniffing_command) — and B1 arming leaves
    last_sniffing_command at RF_CODE_RFIN, so a single ACKed capture silently
    reverts the radio to standard sniffing and kills listening. Verified live
    on rf433-bridge-office (2026-07-17): the first delivered ambient capture
    plus its ACK ended bucket mode until the next TX cycle re-armed it.
    """
    _write_rf_bridge_stubs(tmp_path)
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstddef>
        #include <cstdint>
        #include <string>
        #include <vector>

        #include "components/rf_bridge/rf_bridge.cpp"

        using esphome::App;
        using esphome::rf_bridge::RFBridgeAdvancedData;
        using esphome::rf_bridge::RFBridgeComponent;

        static std::vector<uint8_t> aok_frame() {
          std::vector<uint8_t> frame{
              0xAA, 0xB1, 0x04, 0x14, 0x14, 0x02, 0x6C, 0x01, 0x55, 0x14, 0x14,
          };
          frame.push_back(0x38);
          for (size_t bit = 0; bit < 65; bit++)
            frame.push_back(0x1A);
          frame.push_back(0x19);
          frame.push_back(0x55);
          return frame;
        }

        int main() {
          // TODO(hardware): Replace/augment the synthesized AOK fixtures in
          // this file with OEM-captured UP/DOWN/STOP vectors after the hardware
          // spike validates identities, channels, and capture jitter.
          RFBridgeComponent startup_bridge;
          size_t startup_callbacks = 0;
          startup_bridge.add_on_bucket_received_callback(
              [&](const std::string &) { startup_callbacks++; });
          App.set_loop_component_start_time(1);
          startup_bridge.feed_uart({0xAA, 0xB1, 0x04, 0x14});
          startup_bridge.loop();
          startup_bridge.feed_uart({0x14, 0x02, 0x6C});
          startup_bridge.setup();
          assert(startup_bridge.written_bytes() ==
                 std::vector<uint8_t>({0xAA, 0xA7, 0x55}));
          App.set_loop_component_start_time(2);
          startup_bridge.feed_uart(aok_frame());
          startup_bridge.loop();
          App.set_loop_component_start_time(8);
          startup_bridge.loop();
          assert(startup_callbacks == 1);
          // The accepted capture wrote nothing beyond the startup A7.
          assert(startup_bridge.written_bytes().size() == 3U);

          RFBridgeComponent bridge;
          size_t bucket_callbacks = 0;
          size_t advanced_callbacks = 0;
          RFBridgeAdvancedData advanced{};
          bridge.add_on_bucket_received_callback(
              [&](const std::string &) { bucket_callbacks++; });
          bridge.add_on_advanced_code_received_callback(
              [&](RFBridgeAdvancedData data) {
                advanced_callbacks++;
                advanced = data;
              });

          // This foreign B1 has a valid bucket table but ends before the AOK
          // minimum. UART quiet completes its transport without publishing it
          // and without writing anything back to the coprocessor.
          App.set_loop_component_start_time(100);
          bridge.feed_uart({
              0xAA, 0xB1, 0x03, 0x00, 0x64, 0x01, 0x2C, 0x04, 0x00, 0x89, 0xAB, 0x55,
          });
          bridge.loop();
          assert(bridge.written_bytes().empty());
          App.set_loop_component_start_time(106);
          bridge.loop();
          assert(bridge.written_bytes().empty());
          assert(bucket_callbacks == 0);

          // INVALID B1 metadata is dropped and reset immediately instead of
          // occupying the parser until its timeout — still no write-back.
          App.set_loop_component_start_time(150);
          bridge.feed_uart({0xAA, 0xB1, 0x02});
          bridge.loop();
          assert(bucket_callbacks == 0);
          bridge.feed_uart({0x00, 0x64, 0x04, 0x00, 0x12, 0x55});
          bridge.loop();
          App.set_loop_component_start_time(156);
          bridge.loop();
          assert(bridge.written_bytes().empty());
          assert(bucket_callbacks == 0);

          // A truncated B1 is flushed by the bounded timeout without an ACK.
          App.set_loop_component_start_time(200);
          bridge.feed_uart({
              0xAA, 0xB1, 0x03, 0x00, 0x64, 0x01, 0x2C, 0x04, 0x00, 0x89, 0xAB,
          });
          bridge.loop();
          App.set_loop_component_start_time(451);
          bridge.loop();
          assert(bridge.written_bytes().empty());
          assert(bucket_callbacks == 0);

          // The first 0x55 is code data: decoding waits for the exact endpoint
          // declared by length byte 0x05 and never reads beyond this prefix.
          App.set_loop_component_start_time(500);
          bridge.feed_uart({0xAA, 0xA6, 0x05, 0x01, 0x55});
          bridge.loop();
          assert(advanced_callbacks == 0);
          bridge.feed_uart({0x32, 0xFA, 0x80, 0x55});
          bridge.loop();
          assert(bridge.written_bytes().empty());
          assert(advanced_callbacks == 1);
          assert(advanced.length == 0x05);
          assert(advanced.protocol == 0x01);
          assert(advanced.code == "5532FA80");

          // A later valid AOK capture is still received, proving rejected B1s
          // leave the parser armed for continued listening.
          App.set_loop_component_start_time(600);
          bridge.feed_uart(aok_frame());
          bridge.loop();
          App.set_loop_component_start_time(606);
          bridge.loop();
          assert(bridge.written_bytes().empty());
          assert(bucket_callbacks == 1);

          // receive_idle() reports transport quiet so the keepalive re-arm
          // never clips a frame that is mid-parse.
          assert(bridge.receive_idle());
          bridge.feed_uart({0xAA, 0xB1});
          bridge.loop();
          assert(!bridge.receive_idle());
          return 0;
        }
        """,
    )


def test_sniff_state_caps_rate_limits_expires_and_wraps(tmp_path: Path) -> None:
    """The bounded state accepts only sniff and emits one expiry transition."""
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstdint>
        #include <string>

        #include "rf433_rx.h"

        using rf433::RxCommandAction;
        using rf433::RxState;

        int main() {
          uint8_t normalized = 0;
          assert(rf433::normalize_sniff_seconds(0, normalized) && normalized == 0);
          assert(!rf433::normalize_sniff_seconds(-1, normalized));
          assert(rf433::normalize_sniff_seconds(1, normalized) && normalized == 1);
          assert(rf433::normalize_sniff_seconds(61, normalized) && normalized == 60);
          assert(rf433::normalize_sniff_seconds(1000000, normalized) && normalized == 60);

          const auto missing = rf433::validate_rx_command("sniff", false, 0);
          assert(missing.action == RxCommandAction::INVALID);
          const auto negative = rf433::validate_rx_command("sniff", true, -1);
          assert(negative.action == RxCommandAction::INVALID);
          const auto cancel = rf433::validate_rx_command("sniff", true, 0);
          assert(cancel.action == RxCommandAction::SNIFF && cancel.seconds == 0);
          const auto capped = rf433::validate_rx_command("sniff", true, 61);
          assert(capped.action == RxCommandAction::SNIFF && capped.seconds == 60);
          const auto listen = rf433::validate_rx_command("listen", true, 30);
          assert(listen.action == RxCommandAction::INVALID);

          RxState rate;
          assert(rate.command_allowed(1000, capped));
          assert(!rate.command_allowed(1001, capped));
          assert(rate.command_allowed(1002, cancel));
          assert(rate.command_allowed(1000 + rf433::CMD_RATE_LIMIT_MS, capped));
          assert(!rate.command_allowed(2000, missing));

          RxState state;
          assert(!state.bounded_active(0));
          state.start_sniff(capped.seconds, 100);
          assert(state.bounded_active(100));
          assert(state.bounded_active(60099));
          assert(!state.bounded_active(60100));

          state.start_sniff(1, 1000);
          assert(state.bounded_active(60099));
          state.start_sniff(60, 2000);
          assert(state.bounded_active(61999));
          assert(!state.bounded_active(62000));
          assert(!state.tick(61999));
          assert(state.tick(62000));
          assert(!state.bounded_active(62000));
          assert(!state.tick(62001));

          state.start_sniff(30, 70000);
          state.start_sniff(0, 70001);
          assert(!state.bounded_active(70001));

          RxState rollover;
          const uint32_t near_wrap = 0xFFFFFF00U;
          rollover.start_sniff(1, near_wrap);
          assert(rollover.bounded_active(near_wrap + 999U));
          assert(!rollover.bounded_active(near_wrap + 1000U));
          assert(rollover.tick(near_wrap + 1000U));
          assert(!rollover.tick(near_wrap + 1001U));
          return 0;
        }
        """,
    )


def test_rx_state_keeps_bounded_deadline_across_radio_preemption(
    tmp_path: Path,
) -> None:
    """Bounded intent expires independently of physical bucket mode."""
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstdint>
        #include <type_traits>

        #include "rf433_rx.h"

        using rf433::RxState;

        static_assert(std::is_same_v<decltype(&RxState::bounded_active),
                                     bool (RxState::*)(uint32_t) const>);
        static_assert(std::is_same_v<decltype(&RxState::wants_sniff),
                                     bool (RxState::*)(uint32_t, bool) const>);
        static_assert(std::is_same_v<decltype(&RxState::radio_sniffing),
                                     bool (RxState::*)() const>);
        static_assert(std::is_same_v<decltype(&RxState::set_radio_sniffing),
                                     void (RxState::*)(bool)>);
        static_assert(std::is_same_v<decltype(&RxState::should_publish),
                                     bool (RxState::*)() const>);

        int main() {
          RxState bounded;
          assert(!bounded.bounded_active(0));
          bounded.start_sniff(2, 1000);
          assert(bounded.bounded_active(1000));
          assert(bounded.bounded_active(2999));
          assert(!bounded.bounded_active(3000));

          RxState extended;
          extended.start_sniff(2, 1000);
          // A shorter accepted request cannot shorten the existing deadline.
          extended.start_sniff(1, 1500);
          assert(extended.bounded_active(2999));
          // A later deadline extends it.
          extended.start_sniff(3, 1500);
          assert(extended.bounded_active(4499));
          assert(!extended.bounded_active(4500));
          assert(!extended.tick(4499));
          assert(extended.tick(4500));
          assert(!extended.tick(4501));

          RxState preempted;
          preempted.start_sniff(30, 1000);
          preempted.set_radio_sniffing(true);
          assert(preempted.radio_sniffing());
          assert(preempted.should_publish());

          // TX yields physical bucket mode without touching bounded intent.
          preempted.set_radio_sniffing(false);
          assert(!preempted.radio_sniffing());
          assert(!preempted.should_publish());
          assert(preempted.bounded_active(30999));

          preempted.set_radio_sniffing(true);
          assert(preempted.bounded_active(30999));
          assert(!preempted.tick(30999));
          assert(!preempted.bounded_active(31000));
          assert(preempted.tick(31000));
          assert(!preempted.tick(31001));
          // Expiry is logical only; the reconciler owns the physical edge.
          assert(preempted.radio_sniffing());
          assert(preempted.should_publish());
          return 0;
        }
        """,
    )


def test_rx_state_wants_sniff_truth_table_and_cancel_preserves_radio(
    tmp_path: Path,
) -> None:
    """Idle listen and bounded intent combine without implicit radio edges."""
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstdint>

        #include "rf433_rx.h"

        using rf433::RxState;

        int main() {
          RxState state;
          assert(!state.wants_sniff(100, false));
          assert(state.wants_sniff(100, true));
          assert(!state.should_publish());

          state.start_sniff(1, 100);
          assert(state.bounded_active(1099));
          assert(state.wants_sniff(1099, false));
          assert(state.wants_sniff(1099, true));
          assert(!state.wants_sniff(1100, false));
          assert(state.wants_sniff(1100, true));
          // Logical intent alone does not claim that bucket mode is physical.
          assert(!state.should_publish());

          state.set_radio_sniffing(true);
          assert(state.radio_sniffing());
          assert(state.should_publish());
          state.start_sniff(0, 101);
          assert(!state.bounded_active(101));
          assert(!state.wants_sniff(101, false));
          assert(state.wants_sniff(101, true));
          // Cancel leaves the physical state for the reconciler to change.
          assert(state.radio_sniffing());
          assert(state.should_publish());

          state.set_radio_sniffing(false);
          assert(!state.radio_sniffing());
          assert(!state.should_publish());
          return 0;
        }
        """,
    )


def test_rx_state_keepalive_rearms_only_while_radio_armed(tmp_path: Path) -> None:
    """The B1 keepalive fires on a coarse cadence and only while listening.

    Portisch can silently leave bucket-sniffing mode without telling the host
    (its RF_CODE_ACK handler re-arms last_sniffing_command — which B1 arming
    leaves at RF_CODE_RFIN — and an EFM8 watchdog reset boots into standard
    sniffing). A periodic idempotent B1 bounds that deafness to one keepalive
    period.
    """
    _compile_and_run(
        tmp_path,
        r"""
        #include <cassert>
        #include <cstdint>

        #include "rf433_rx.h"

        using rf433::RxState;

        static_assert(rf433::RX_KEEPALIVE_MS == 5000);

        int main() {
          RxState state;
          // Never due while the radio is off, no matter how stale the stamp.
          assert(!state.keepalive_due(1000000));

          state.set_radio_sniffing(true);
          state.note_radio_armed(1000);
          assert(!state.keepalive_due(1000));
          assert(!state.keepalive_due(5999));
          assert(state.keepalive_due(6000));

          // Re-arming restarts the cadence.
          state.note_radio_armed(6000);
          assert(!state.keepalive_due(10999));
          assert(state.keepalive_due(11000));

          // Disarm gates the keepalive regardless of elapsed time.
          state.set_radio_sniffing(false);
          assert(!state.keepalive_due(1000000));
          state.set_radio_sniffing(true);
          assert(state.keepalive_due(1000000));

          // millis() wraparound keeps the unsigned cadence arithmetic valid.
          state.note_radio_armed(0xFFFFF000u);
          assert(!state.keepalive_due(0xFFFFFFFFu));
          assert(!state.keepalive_due(0x00000387u));
          assert(state.keepalive_due(0x00000388u));
          return 0;
        }
        """,
    )


def _assert_keepalive_wiring(interval_handler: str) -> None:
    """Every physical B1 arm stamps the cadence; the re-arm gates on quiet."""
    b1_arm_sites = 2  # idle-transition arm + periodic keepalive arm
    assert interval_handler.count("rx.note_radio_armed(now_ms);") == b1_arm_sites
    assert "rx.keepalive_due(now_ms)" in interval_handler
    assert ".receive_idle()" in interval_handler


def test_firmware_wires_state_sync_contract_without_rx_to_tx() -> None:
    """YAML wires the state-sync primitives while RX stays observation-only."""
    package = BRIDGE_YAML.read_text()
    rx_header = RX_HEADER.read_text()
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "external_components:" in package
    assert "type: local" in package
    assert "path: components" in package
    assert "on_bucket_received:" in package
    assert "topic: rf433/${bridge_id}/cmd" in package
    assert '"rf433/${bridge_id}/rx"' in package
    assert '"sniff"' in rx_header
    assert "MAX_SNIFF_SECONDS = 60" in rx_header
    assert "CMD_RATE_LIMIT_MS = 250" in rx_header
    assert "validate_rx_command" in package
    assert 'x["seconds"].is<int>()' in package
    assert "command_allowed(now_ms, command)" in package
    assert "command.seconds == 0" in package
    assert 'listen_enabled: "false"' in package
    assert "id(boot_id) = esphome::random_uint32();" in package
    assert 'root["frame"]' in package
    assert 'root["t"]' in package
    assert "last_t" not in package
    assert "esphome compile living-room.yaml" in workflow

    rx_handler = package.split("on_bucket_received:", maxsplit=1)[1].split(
        "\n\n# The per-bridge scheduler", maxsplit=1
    )[0]
    assert "send_raw" not in rx_handler
    assert "tx_scheduler" not in rx_handler
    assert "start_bucket_sniffing" not in rx_handler
    assert "stop_advanced_sniffing" not in rx_handler
    assert "rf433::rx_state().should_publish()" in rx_handler
    assert 'root["boot"] = id(boot_id);' in rx_handler
    assert "}, 1, false)" in rx_handler
    assert package.count(".send_raw(") == 1

    cmd_handler = package.split("- topic: rf433/${bridge_id}/cmd", maxsplit=1)[1].split(
        "\nglobals:", maxsplit=1
    )[0]
    assert "start_bucket_sniffing" not in cmd_handler
    assert "stop_advanced_sniffing" not in cmd_handler
    assert 'action == "disarm"' in cmd_handler
    assert "rf433::valid_key(command_id)" in cmd_handler
    assert ".disarm(command_id)" in cmd_handler
    assert 'root["status"] = "disarmed";' in cmd_handler
    assert 'root["t"] = status_ms;' in cmd_handler
    assert 'root["boot"] = id(boot_id);' in cmd_handler

    interval_handler = package.split("interval:", maxsplit=1)[1]
    assert "rx.tick(now_ms)" in interval_handler
    assert "!sched.idle() || !sched.rf_air_clear(now_ms)" in interval_handler
    assert "rx.wants_sniff(now_ms, ${listen_enabled})" in interval_handler
    assert "rx.set_radio_sniffing(desired)" in interval_handler
    assert "stop_advanced_sniffing" in interval_handler
    assert "start_bucket_sniffing" in interval_handler
    _assert_keepalive_wiring(interval_handler)


def test_firmware_state_sync_payloads_and_deferred_surface() -> None:
    """The started/info payloads are stamped and the unbuilt surface stays absent."""
    package = BRIDGE_YAML.read_text()
    rx_header = RX_HEADER.read_text()
    scheduler = (PROJECT_ROOT / "rf433_scheduler.h").read_text()
    interval_handler = package.split("interval:", maxsplit=1)[1]

    info_publish = interval_handler.split('publish_json("rf433/${bridge_id}/info"', maxsplit=1)[
        1
    ].split("}, 0, true)", maxsplit=1)[0]
    assert 'root["boot"] = id(boot_id);' in info_publish
    assert 'root["listen"] = ${listen_enabled};' in info_publish
    assert 'root["v"] = 2;' in info_publish

    # Both the replay and fresh-dispatch paths publish a measured age and the
    # same bridge-clock/session fields needed to recover the handoff instant.
    started_paths_stamping_age = 2
    assert package.count('root["age_ms"]') == started_paths_stamping_age
    assert 'root["age_ms"] = started_age_ms;' in package
    assert 'root["age_ms"] = status_ms - dispatch_ms;' in interval_handler
    assert 'publish_status("started", started_command_id);' in interval_handler

    # The remaining integration-central surfaces are deliberately unbuilt.
    # RX_KEEPALIVE_MS moved out of this list on 2026-07-17: the hardware spike
    # proved Portisch silently exits bucket mode (ACK-handler re-arm of a stale
    # last_sniffing_command), so the keepalive is now a shipped requirement.
    deferred_symbols = (
        "session_nonce",
        "RxRadioState",
        "record_frame_sent",
        "completed_command_id",
    )
    combined = package + rx_header + scheduler
    for symbol in deferred_symbols:
        assert symbol not in combined
    assert 'action == "listen"' not in combined
    assert "esphome::random_bytes" not in package
    assert 'root["nonce"]' not in package
    assert "switch:" not in package
    assert "button:" not in package


def test_vendored_component_wires_safe_filtered_rx_parsers() -> None:
    """The local component never ACKs received frames and bounds advanced data."""
    component_python = (RF_BRIDGE_DIR / "__init__.py").read_text()
    component_cpp = (RF_BRIDGE_DIR / "rf_bridge.cpp").read_text()
    component_header = (RF_BRIDGE_DIR / "rf_bridge.h").read_text()

    assert 'CONF_ON_BUCKET_RECEIVED = "on_bucket_received"' in component_python
    assert "add_on_bucket_received_callback" in component_python
    bucket_case = component_cpp.split("case RF_CODE_RFIN_BUCKET:", maxsplit=1)[1].split(
        "default:", maxsplit=1
    )[0]
    advanced_case = component_cpp.split("case RF_CODE_LEARN_OK_NEW:", maxsplit=1)[1].split(
        "case RF_CODE_RFIN_BUCKET:", maxsplit=1
    )[0]
    bucket_finish = component_cpp.split(
        "void RFBridgeComponent::finish_bucket_capture_(bool publish)", maxsplit=1
    )[1].split("bool RFBridgeComponent::parse_bridge_byte_", maxsplit=1)[0]
    # A host ACK makes Portisch call PCA0_DoSniffing(last_sniffing_command),
    # which B1 arming leaves at RF_CODE_RFIN — one ACKed capture would revert
    # the radio to standard sniffing and silently end listening.
    assert "this->ack_();" not in component_cpp
    assert "void ack_();" not in component_header
    assert "receive_idle" in component_header
    assert "if (publish)" in bucket_finish
    assert "bucket_data_callback_.call" in bucket_finish
    assert "send_raw" not in bucket_case
    assert "send_raw" not in bucket_finish
    assert "b1_frame_status" in bucket_case
    assert "is_aok_bucket_frame" not in bucket_case
    assert "B1FrameStatus::CANDIDATE" in bucket_case
    assert "B1FrameStatus::INVALID" in bucket_case
    assert "void RFBridgeComponent::setup()" in component_cpp
    assert "this->reset_receive_state_();" in component_cpp
    assert "bucket_transport_candidate" in component_cpp
    assert "if (receiving_bucket)" in component_cpp
    assert "this->finish_bucket_capture_(this->bucket_candidate_);" in component_cpp
    assert "avail == 0 && now - this->last_bridge_byte_ > rx_timeout_ms" in component_cpp
    assert "B1_CANDIDATE_QUIET_MS" in component_cpp
    assert "const size_t stop_at = static_cast<size_t>(length) + 3U;" in component_cpp
    assert "if (at != stop_at || byte != RF_CODE_STOP)" in component_cpp
    assert "if (length == 0 || buffered_size < 5U)" in advanced_case
    assert "index < buffered_size - 1U" in advanced_case
    assert "this->rx_buffer_[index]" in advanced_case


def test_rx_docs_publish_state_sync_contract_and_vendored_version() -> None:
    """Docs cover shipped opt-in state sync and its hardware rollout gate."""
    readme = (PROJECT_ROOT / "README.md").read_text()
    example = (PROJECT_ROOT / "examples" / "living-room.yaml").read_text()
    vendor_notes = (RF_BRIDGE_DIR / "README.md").read_text()

    assert "`rf433/<bridge_id>/rx`" in readme
    assert "`rf433/<bridge_id>/cmd`" in readme
    assert '{"action":"sniff","seconds":30}' in readme
    assert '{"action":"sniff","seconds":0}' in readme
    assert "hard-capped at 60" in readme
    assert "Cancellation is exempt from the rate limiter" in readme
    assert "firmware primitives ship now" in readme
    assert "`listen_enabled`" in readme
    assert 'defaults to `"false"`' in readme
    assert "Continuous `/rx`" in readme
    assert "QoS 1, non-retained" in readme
    assert "`handoff = t - age_ms`" in readme
    assert '"listen":false' in readme
    assert '"v":2' in readme
    assert '{"action":"disarm","command_id":"move:42"}' in readme
    assert '"status":"disarmed"' in readme
    assert "monotonic modulo `2^32`" in readme
    assert "activity stream" in readme
    assert "broker ACL" in readme
    assert "privacy" in readme.lower()
    assert "end-to-end state-sync use remains gated" in readme
    assert "real OEM-captured golden" in readme
    assert "HARDWARE-VALIDATION" in readme
    assert "components/" in readme
    assert "components/" in example
    assert "esphome compile living-room.yaml" in readme
    assert "Only while that sniff is active" not in readme
    assert "planned follow-up work" not in readme

    assert "nonce" not in readme.lower()
    assert '"action":"listen"' not in readme
    assert "strictly monotonic" not in readme
    assert "WAIT_STOP" not in readme
    assert "airtime-aware" not in readme

    assert "ESPHome 2026.6.5" in vendor_notes
    assert "esphome/components/rf_bridge" in vendor_notes
    assert "declared-length B1 framing" in vendor_notes
    assert "CANDIDATE/quiet disambiguation" in vendor_notes
    assert "startup A7 stop-sniff" in vendor_notes
    assert "bounded Learn/onboarding flow" in vendor_notes
    assert "never schedules or triggers TX" in vendor_notes
    assert "Real OEM capture decoding remains deferred" in vendor_notes
