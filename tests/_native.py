"""Shared native (host C++) test harness.

The firmware's contract tests compile the real vendored sources against a small
ESPHome stand-in and run them. Three test modules need the same three pieces, so
they live here rather than being imported out of a sibling test module's
privates.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
RF_BRIDGE_DIR = PROJECT_ROOT / "components" / "rf_bridge"
RF_BRIDGE_CPP = RF_BRIDGE_DIR / "rf_bridge.cpp"


def rf_bridge_member_bodies() -> dict[str, str]:
    """Split rf_bridge.cpp into one source slice per RFBridgeComponent member."""
    source = RF_BRIDGE_CPP.read_text()
    definitions = list(
        re.finditer(r"^\w[\w:<>*& ]*\bRFBridgeComponent::(\w+)\(", source, re.MULTILINE)
    )
    starts = [match.start() for match in definitions] + [len(source)]
    return {
        match.group(1): source[starts[index] : starts[index + 1]]
        for index, match in enumerate(definitions)
    }


def compile_and_run(tmp_path: Path, source_text: str, extra_flags: list[str] | None = None) -> None:
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
            *(extra_flags or []),
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


def write_rf_bridge_stubs(tmp_path: Path) -> None:
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
              // Counted, not ignored: "wrote nothing" and "wrote nothing AND
              // did not block the UART draining it" are different claims, and a
              // refusal path that returned after flushing would satisfy only
              // the first. Tests assert on both.
              void flush() { this->flushes_++; }
              void check_uart_settings(uint32_t) {}

              void feed_uart(const std::vector<uint8_t> &bytes) {
                this->input_.insert(this->input_.end(), bytes.begin(), bytes.end());
              }

              const std::vector<uint8_t> &written_bytes() const { return this->output_; }

              size_t flush_count() const { return this->flushes_; }

             private:
              std::deque<uint8_t> input_;
              std::vector<uint8_t> output_;
              size_t flushes_{0};
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

            #include <string>
            #include <utility>
            #include <vector>

            namespace esphome {

            template<typename... Args>
            void host_test_log(const char *, const char *, Args &&...) {}

            // ESP_LOGW is the firmware's only channel for "this frame no longer
            // encodes your code" and "nothing was sent", so host tests assert on
            // it by behavior rather than by grepping the source for the call.
            // Only the format string is recorded: running it through snprintf
            // here would mean a non-literal format under -Werror for no gain,
            // and the argument values are pinned where they are computed.
            inline std::vector<std::string> &host_test_warnings() {
              static std::vector<std::string> warnings;
              return warnings;
            }

            template<typename... Args>
            void host_test_warn(const char *, const char *format, Args &&...) {
              host_test_warnings().emplace_back(format);
            }

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
              ::esphome::host_test_warn(tag, format, ##__VA_ARGS__)
            #define ESP_LOGCONFIG(tag, format, ...) \
              ::esphome::host_test_log(tag, format, ##__VA_ARGS__)
        """,
    }
    for relative_path, contents in stubs.items():
        stub = tmp_path / relative_path
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(textwrap.dedent(contents))
