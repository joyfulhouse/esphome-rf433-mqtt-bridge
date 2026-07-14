#pragma once

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace rf433 {

constexpr size_t MAX_B0_FRAME_BYTES = 260;
constexpr size_t MAX_TARGETS = 32;

inline int hex_value(char value) {
  if (value >= '0' && value <= '9')
    return value - '0';
  if (value >= 'A' && value <= 'F')
    return value - 'A' + 10;
  return -1;
}

inline bool normalize_b0(const std::string &input, std::string &output, std::string &reason) {
  output.clear();
  output.reserve(input.size());
  for (const char value : input) {
    if (std::isspace(static_cast<unsigned char>(value)))
      continue;
    const char normalized = static_cast<char>(std::toupper(static_cast<unsigned char>(value)));
    if (hex_value(normalized) < 0) {
      reason = "frame must contain only hex bytes";
      return false;
    }
    output.push_back(normalized);
  }
  if (output.size() % 2 != 0) {
    reason = "frame must contain complete hex bytes";
    return false;
  }
  if (output.size() / 2 > MAX_B0_FRAME_BYTES) {
    reason = "frame exceeds maximum size";
    return false;
  }
  if (output.size() < 10 || output.compare(0, 4, "AAB0") != 0) {
    reason = "frame must start with AAB0";
    return false;
  }
  if (output.compare(output.size() - 2, 2, "55") != 0) {
    reason = "frame trailer is invalid";
    return false;
  }
  const int length_high = hex_value(output[4]);
  const int length_low = hex_value(output[5]);
  const size_t body_length = static_cast<size_t>((length_high << 4) | length_low);
  if (body_length < 2 || output.size() != 8 + body_length * 2) {
    reason = "frame declared length is invalid";
    return false;
  }
  const size_t body_end = 6 + body_length * 2;
  const int count_high = hex_value(output[6]);
  const int count_low = hex_value(output[7]);
  const size_t bucket_count = static_cast<size_t>((count_high << 4) | count_low);
  if (bucket_count < 1 || bucket_count > 8) {
    reason = "bucket count must be in the range 1..8";
    return false;
  }
  const size_t data_start = 10 + bucket_count * 4;
  if (data_start > body_end) {
    reason = "frame bucket table is truncated";
    return false;
  }
  for (size_t index = data_start; index < body_end; index++) {
    if (static_cast<size_t>(hex_value(output[index]) & 0x07) >= bucket_count) {
      reason = "frame references an undefined bucket";
      return false;
    }
  }
  reason.clear();
  return true;
}

inline bool valid_key(const std::string &value) {
  if (value.empty() || value.size() > 64)
    return false;
  return std::all_of(value.begin(), value.end(), [](const char character) {
    const auto byte = static_cast<unsigned char>(character);
    return std::isalnum(byte) || character == '-' || character == '_' || character == '.' || character == ':' ||
           character == ',';
  });
}

inline bool valid_target_key(const std::string &value) {
  // Canonical HA target: six prefix hex digits, two remote-ID hex digits,
  // then a strictly increasing, comma-separated channel set in 1..16.
  if (value.size() < 11 || value.size() > 53 || value[6] != ':' || value[9] != ':')
    return false;
  for (size_t index = 0; index < 9; index++) {
    if (index == 6)
      continue;
    if (hex_value(static_cast<char>(std::toupper(static_cast<unsigned char>(value[index])))) < 0)
      return false;
  }

  size_t cursor = 10;
  int previous = 0;
  while (cursor < value.size()) {
    int channel = 0;
    const size_t start = cursor;
    while (cursor < value.size() && value[cursor] >= '0' && value[cursor] <= '9') {
      channel = channel * 10 + (value[cursor] - '0');
      cursor++;
    }
    if (cursor == start || channel < 1 || channel > 16 || channel <= previous)
      return false;
    previous = channel;
    if (cursor == value.size())
      return true;
    if (value[cursor] != ',')
      return false;
    cursor++;
  }
  return false;
}

class TargetScheduler {
 public:
  explicit TargetScheduler(uint32_t repeat_gap_ms) : repeat_gap_ms_(repeat_gap_ms) {}

  bool schedule(const std::string &command_id, const std::string &target, const std::string &raw,
                const std::string &trailer_raw, int repeats, uint32_t stop_after_ms,
                const std::string &stop_raw, uint32_t now_ms) {
    if (!valid_key(command_id) || !valid_target_key(target) || raw.empty() || repeats < 1 || repeats > 20)
      return false;
    const bool new_target = this->commands_.find(target) == this->commands_.end();
    if (new_target && this->commands_.size() >= MAX_TARGETS)
      return false;
    if (new_target && this->overlaps_existing_(target))
      return false;

    Command command;
    command.command_id = command_id;
    command.raw = raw;
    command.trailer_raw = trailer_raw;
    command.stop_raw = stop_raw;
    command.repeats = repeats;
    command.remaining = repeats;
    command.stop_after_ms = stop_after_ms;
    command.deadline_at = 0;
    command.next_at = now_ms;
    command.phase = Phase::ACTION;
    this->commands_[target] = std::move(command);
    if (new_target)
      this->order_.push_back(target);
    return true;
  }

  std::optional<std::string> next(uint32_t now_ms, std::string &started_command_id) {
    started_command_id.clear();
    if (this->commands_.empty() ||
        (this->next_rf_at_.has_value() && !due_(now_ms, *this->next_rf_at_)))
      return std::nullopt;

    for (const std::string &target : this->order_) {
      Command &command = this->commands_.at(target);
      if (command.deadline_armed && command.phase != Phase::STOP &&
          due_(now_ms, command.deadline_at)) {
        command.phase = Phase::STOP;
        command.remaining = command.repeats;
        command.next_at = now_ms;
      }
    }

    const size_t count = this->order_.size();
    for (int stop_priority = 1; stop_priority >= 0; stop_priority--) {
      for (size_t offset = 0; offset < count; offset++) {
        const size_t index = (this->cursor_ + offset) % count;
        const std::string target = this->order_[index];
        Command &command = this->commands_.at(target);
        const bool is_stop = command.phase == Phase::STOP;
        if (is_stop != (stop_priority == 1) || command.phase == Phase::WAIT_STOP ||
            !due_(now_ms, command.next_at))
          continue;

        const std::string raw = this->phase_raw_(command);
        if (command.phase == Phase::ACTION && !command.started) {
          command.started = true;
          started_command_id = command.command_id;
          if (command.stop_after_ms > 0) {
            command.deadline_armed = true;
            command.deadline_at = now_ms + command.stop_after_ms;
          }
        }
        command.remaining--;
        const bool complete = command.remaining == 0 && this->advance_(command, now_ms);
        if (command.remaining > 0)
          command.next_at = now_ms + this->repeat_gap_ms_;

        this->next_rf_at_ = now_ms + this->repeat_gap_ms_;
        this->cursor_ = (index + 1) % count;
        if (complete)
          this->erase_(target);
        return raw;
      }
    }
    return std::nullopt;
  }

 protected:
  enum class Phase { ACTION, TRAILER, WAIT_STOP, STOP };

  struct Command {
    std::string command_id;
    std::string raw;
    std::string trailer_raw;
    std::string stop_raw;
    int repeats{1};
    int remaining{1};
    uint32_t stop_after_ms{0};
    uint32_t deadline_at{0};
    uint32_t next_at{0};
    Phase phase{Phase::ACTION};
    bool started{false};
    bool deadline_armed{false};
  };

  static std::vector<int> channels_(const std::string &target) {
    std::vector<int> channels;
    size_t cursor = 10;
    while (cursor < target.size()) {
      int channel = 0;
      while (cursor < target.size() && target[cursor] >= '0' && target[cursor] <= '9') {
        channel = channel * 10 + (target[cursor] - '0');
        cursor++;
      }
      channels.push_back(channel);
      if (cursor < target.size())
        cursor++;
    }
    return channels;
  }

  static bool overlaps_(const std::string &left, const std::string &right) {
    for (size_t index = 0; index < 10; index++) {
      const auto left_byte = static_cast<unsigned char>(left[index]);
      const auto right_byte = static_cast<unsigned char>(right[index]);
      if (std::toupper(left_byte) != std::toupper(right_byte))
        return false;
    }
    const std::vector<int> left_channels = channels_(left);
    const std::vector<int> right_channels = channels_(right);
    for (const int channel : left_channels) {
      if (std::find(right_channels.begin(), right_channels.end(), channel) != right_channels.end())
        return true;
    }
    return false;
  }

  bool overlaps_existing_(const std::string &target) const {
    return std::any_of(this->commands_.begin(), this->commands_.end(), [&](const auto &item) {
      return overlaps_(target, item.first);
    });
  }

  static bool due_(uint32_t now_ms, uint32_t deadline_ms) {
    return static_cast<int32_t>(now_ms - deadline_ms) >= 0;
  }

  const std::string &phase_raw_(const Command &command) const {
    if (command.phase == Phase::TRAILER)
      return command.trailer_raw;
    if (command.phase == Phase::STOP)
      return command.stop_raw;
    return command.raw;
  }

  bool advance_(Command &command, uint32_t now_ms) {
    if (command.phase == Phase::ACTION && !command.trailer_raw.empty()) {
      command.phase = Phase::TRAILER;
      command.remaining = command.repeats;
      command.next_at = now_ms + this->repeat_gap_ms_;
      return false;
    }
    if ((command.phase == Phase::ACTION || command.phase == Phase::TRAILER) && command.stop_after_ms > 0) {
      command.phase = Phase::WAIT_STOP;
      command.remaining = command.repeats;
      command.next_at = command.deadline_at;
      return false;
    }
    return true;
  }

  void erase_(const std::string &target) {
    this->commands_.erase(target);
    const auto found = std::find(this->order_.begin(), this->order_.end(), target);
    if (found == this->order_.end())
      return;
    const size_t removed = static_cast<size_t>(std::distance(this->order_.begin(), found));
    this->order_.erase(found);
    if (this->order_.empty()) {
      this->cursor_ = 0;
    } else {
      if (removed < this->cursor_ && this->cursor_ > 0)
        this->cursor_--;
      this->cursor_ %= this->order_.size();
    }
  }

  uint32_t repeat_gap_ms_;
  std::optional<uint32_t> next_rf_at_;
  size_t cursor_{0};
  std::map<std::string, Command> commands_;
  std::vector<std::string> order_;
};

// ESPHome emits the globals pstorage that references TargetScheduler BEFORE it
// emits the `includes:` header block, so a `globals:` entry of this custom type
// fails to compile ("'rf433' was not declared in this scope"). Expose the
// single per-bridge instance as a function-local static instead; the beacon
// lambdas run after the include and call this accessor. The gap argument is
// honored only on first construction.
inline TargetScheduler &tx_scheduler(uint32_t repeat_gap_ms) {
  static TargetScheduler instance(repeat_gap_ms);
  return instance;
}

}  // namespace rf433
