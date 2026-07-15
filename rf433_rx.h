#pragma once

#include <algorithm>
#include <cstdint>
#include <string>

namespace rf433 {

constexpr uint8_t MAX_SNIFF_SECONDS = 60;
// Positive sniff commands are cheap, but bounding accepted starts/extensions
// keeps a noisy broker client from monopolizing the 5 ms dispatch loop.
constexpr uint32_t CMD_RATE_LIMIT_MS = 250;

enum class RxCommandAction : uint8_t {
  INVALID,
  SNIFF,
};

struct RxCommand {
  RxCommandAction action;
  uint8_t seconds;
  const char *error;
};

inline bool normalize_sniff_seconds(int seconds, uint8_t &normalized) {
  if (seconds < 0)
    return false;
  normalized = static_cast<uint8_t>(std::min(seconds, static_cast<int>(MAX_SNIFF_SECONDS)));
  return true;
}

inline RxCommand validate_rx_command(const std::string &action, bool seconds_is_integer, int seconds) {
  if (action != "sniff")
    return {RxCommandAction::INVALID, 0, "unknown action"};
  if (!seconds_is_integer)
    return {RxCommandAction::INVALID, 0, "seconds must be an integer"};
  uint8_t normalized = 0;
  if (!normalize_sniff_seconds(seconds, normalized))
    return {RxCommandAction::INVALID, 0, "seconds must be non-negative"};
  return {RxCommandAction::SNIFF, normalized, nullptr};
}

inline bool deadline_reached(uint32_t now_ms, uint32_t deadline_ms) {
  return static_cast<int32_t>(now_ms - deadline_ms) >= 0;
}

class RxState {
 public:
  bool command_allowed(uint32_t now_ms, const RxCommand &command) {
    if (command.action == RxCommandAction::INVALID)
      return false;
    // Cancellation must always win, including immediately after a positive
    // sniff command. Only starts/extensions consume the direction limiter.
    if (command.seconds == 0)
      return true;
    if (this->positive_command_seen_ &&
        now_ms - this->last_positive_command_ms_ < CMD_RATE_LIMIT_MS) {
      return false;
    }
    this->positive_command_seen_ = true;
    this->last_positive_command_ms_ = now_ms;
    return true;
  }

  void start_sniff(uint8_t seconds, uint32_t now_ms) {
    if (seconds == 0) {
      this->bounded_active_ = false;
      this->bounded_until_ms_ = 0;
      return;
    }

    this->expire_bounded_(now_ms);
    const uint32_t candidate = now_ms + static_cast<uint32_t>(seconds) * 1000U;
    if (!this->bounded_active_ ||
        static_cast<int32_t>(candidate - this->bounded_until_ms_) > 0) {
      this->bounded_until_ms_ = candidate;
    }
    this->bounded_active_ = true;
  }

  bool bounded_active(uint32_t now_ms) const {
    return this->bounded_active_ && !deadline_reached(now_ms, this->bounded_until_ms_);
  }

  // Returns true exactly once when a bounded sniff expires. Physical receive
  // mode is reconciled separately and may remain active for idle-listen.
  bool tick(uint32_t now_ms) { return this->expire_bounded_(now_ms); }

  bool wants_sniff(uint32_t now_ms, bool listen_enabled) const {
    return this->bounded_active(now_ms) || listen_enabled;
  }

  bool radio_sniffing() const { return this->radio_sniffing_; }

  void set_radio_sniffing(bool on) { this->radio_sniffing_ = on; }

  bool should_publish() const { return this->radio_sniffing_; }

 private:
  bool expire_bounded_(uint32_t now_ms) {
    if (!this->bounded_active_ || !deadline_reached(now_ms, this->bounded_until_ms_))
      return false;
    this->bounded_active_ = false;
    this->bounded_until_ms_ = 0;
    return true;
  }

  bool bounded_active_{false};
  bool radio_sniffing_{false};
  bool positive_command_seen_{false};
  uint32_t bounded_until_ms_{0};
  uint32_t last_positive_command_ms_{0};
};

inline RxState &rx_state() {
  static RxState state;
  return state;
}

}  // namespace rf433
