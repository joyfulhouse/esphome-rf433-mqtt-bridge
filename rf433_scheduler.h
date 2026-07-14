#pragma once

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace rf433 {

constexpr size_t MAX_B0_FRAME_BYTES = 260;
constexpr size_t MAX_TARGETS = 16;
// Upper bound on the sum of all stored frame strings (raw + trailer + stop
// across every scheduled target). The ESP8285 has roughly 30-40 KB of free
// heap after WiFi/MQTT; without this budget, MAX_TARGETS maximum-size timed
// commands could exhaust it through perfectly valid input.
constexpr size_t MAX_TOTAL_FRAME_BYTES = 16384;
// Recently admitted command IDs, used to drop QoS-1 broker redeliveries and
// same-boot retained replays. The ring lives in RAM, so it cannot suppress a
// retained command replayed after a reboot -- retained tx publishes are
// unsupported (see README). Sized to comfortably span an in-flight burst.
constexpr size_t COMMAND_ID_RING_SIZE = 32;

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
  // Portisch embeds a per-packet hardware repeat count at byte index 4 (hex
  // chars 8..9), after the AAB0 + length + bucket-count header. The controller
  // always sends 08; a crafted value (for example FF) would monopolize the RF
  // coprocessor, so bound it to a sane 1..16.
  const int repeat_high = hex_value(output[8]);
  const int repeat_low = hex_value(output[9]);
  const size_t embedded_repeat = static_cast<size_t>((repeat_high << 4) | repeat_low);
  if (embedded_repeat < 1 || embedded_repeat > 0x10) {
    reason = "frame embedded repeat count out of range";
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

// Canonical HA target: six prefix hex digits, two remote-ID hex digits, then a
// strictly increasing, comma-separated channel set in 1..16. Parses the target
// in one pass into its case-normalized remote identity plus a channel bitmask,
// so validation, equality, and overlap checks share one representation.
inline bool parse_target(const std::string &value, std::string &identity, uint16_t &mask) {
  identity.clear();
  mask = 0;
  if (value.size() < 11 || value.size() > 53 || value[6] != ':' || value[9] != ':')
    return false;
  identity.reserve(9);
  for (size_t index = 0; index < 9; index++) {
    const char upper = static_cast<char>(std::toupper(static_cast<unsigned char>(value[index])));
    if (index == 6) {
      identity.push_back(':');
      continue;
    }
    if (hex_value(upper) < 0)
      return false;
    identity.push_back(upper);
  }

  size_t cursor = 10;
  int previous = 0;
  while (cursor < value.size()) {
    int channel = 0;
    const size_t start = cursor;
    while (cursor < value.size() && value[cursor] >= '0' && value[cursor] <= '9') {
      channel = channel * 10 + (value[cursor] - '0');
      // Bound the accumulator mid-parse: a long digit run (up to 43 digits
      // inside the 53-char cap) would overflow the signed int before the range
      // check below -- undefined behavior. 16 is the largest valid channel.
      if (channel > 16)
        return false;
      cursor++;
    }
    if (cursor == start || channel < 1 || channel > 16 || channel <= previous)
      return false;
    previous = channel;
    mask = static_cast<uint16_t>(mask | (1u << (channel - 1)));
    if (cursor == value.size())
      return true;
    if (value[cursor] != ',')
      return false;
    cursor++;
  }
  return false;
}

inline bool valid_target_key(const std::string &value) {
  std::string identity;
  uint16_t mask = 0;
  return parse_target(value, identity, mask);
}

class TargetScheduler {
 public:
  explicit TargetScheduler(uint32_t repeat_gap_ms) : repeat_gap_ms_(repeat_gap_ms) {}

  // Admits one command. Latest command wins: any already-scheduled target on
  // the same remote whose channels intersect the new target (including the
  // exact same target) is displaced. A displaced command that already started
  // RF with an armed-but-unfinished fail-safe STOP gets its stop frame
  // flushed on air before the new command's first dispatch, and its
  // command_id is appended to displaced_ids so the controller can retire its
  // motion model.
  bool schedule(const std::string &command_id, const std::string &target, const std::string &raw,
                const std::string &trailer_raw, int repeats, uint32_t stop_after_ms,
                const std::string &stop_raw, uint32_t now_ms,
                std::vector<std::string> &displaced_ids, std::string &reason) {
    displaced_ids.clear();
    std::string identity;
    uint16_t mask = 0;
    if (!valid_key(command_id) || !parse_target(target, identity, mask) || raw.empty() ||
        repeats < 1 || repeats > 20) {
      reason = "command_id or canonical target key is invalid";
      return false;
    }
    if (this->seen_recently_(command_id)) {
      reason = "duplicate command_id";
      return false;
    }

    const size_t command_bytes = raw.size() + trailer_raw.size() + stop_raw.size();
    size_t retained_bytes = 0;
    size_t retained_targets = 0;
    for (const auto &item : this->commands_) {
      if (item.second.identity == identity && (item.second.mask & mask) != 0)
        continue;  // displaced below, does not count against the budget
      retained_bytes += item.second.stored_bytes();
      retained_targets++;
    }
    // Fail-safe STOPs already queued for flush hold heap too: displacing many
    // started timed commands can park up to MAX_TARGETS * repeats * 260B on top
    // of commands_, so count them in the admission budget.
    size_t flush_bytes = 0;
    for (const std::string &frame : this->flush_stops_)
      flush_bytes += frame.size();
    if (retained_targets >= MAX_TARGETS) {
      reason = "target scheduler is full";
      return false;
    }
    if (retained_bytes + flush_bytes + command_bytes > MAX_TOTAL_FRAME_BYTES) {
      reason = "scheduler frame storage budget exceeded";
      return false;
    }

    this->displace_overlapping_(identity, mask, displaced_ids);

    Command command;
    command.command_id = command_id;
    command.identity = identity;
    command.mask = mask;
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
    this->order_.push_back(target);
    this->remember_(command_id);
    reason.clear();
    return true;
  }

  std::optional<std::string> next(uint32_t now_ms, std::string &started_command_id) {
    started_command_id.clear();
    // Age-based pacing-gate reset, run every tick regardless of queue state. A
    // stale gate once blocked ALL transmission for up to ~24.9 days after
    // millis() wrapped past the signed comparison horizon (observed: a bridge
    // idle >2^31 ms accepted commands but never transmitted). Resetting only
    // when the gate is more than 60s stale preserves the short-term spacing
    // owed to the just-dispatched frame -- an unconditional drain-reset erased
    // it, letting a command that arrived right after the queue drained transmit
    // with <repeat_gap_ms spacing while the EFM8BB1 was still transmitting. The
    // 5ms interval tick carries the gate through this >60s window long before
    // now-gate could wrap negative at 2^31 ms, so the rollover fix is kept.
    if (this->next_rf_at_.has_value() &&
        static_cast<int32_t>(now_ms - *this->next_rf_at_) > 60000)
      this->next_rf_at_.reset();
    if (this->commands_.empty() && this->flush_stops_.empty())
      return std::nullopt;
    if (this->next_rf_at_.has_value() && !due_(now_ms, *this->next_rf_at_))
      return std::nullopt;

    // Fail-safe STOPs of displaced commands go on air before anything else.
    if (!this->flush_stops_.empty()) {
      const std::string raw = this->flush_stops_.front();
      this->flush_stops_.erase(this->flush_stops_.begin());
      this->next_rf_at_ = now_ms + this->repeat_gap_ms_;
      return raw;
    }

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
    std::string identity;
    uint16_t mask{0};
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

    size_t stored_bytes() const { return raw.size() + trailer_raw.size() + stop_raw.size(); }

    // A displaced command still owes the motor a STOP if its RF already started
    // and its armed fail-safe STOP has not been fully sent: either it has not
    // reached the STOP phase yet, or it is mid-STOP with copies remaining.
    bool owes_stop() const {
      return started && stop_after_ms > 0 && !stop_raw.empty() &&
             (phase != Phase::STOP || remaining > 0);
    }
  };

  void displace_overlapping_(const std::string &identity, uint16_t mask,
                             std::vector<std::string> &displaced_ids) {
    std::vector<std::string> displaced_targets;
    for (const auto &item : this->commands_) {
      if (item.second.identity == identity && (item.second.mask & mask) != 0)
        displaced_targets.push_back(item.first);
    }
    for (const std::string &target : displaced_targets) {
      Command &command = this->commands_.at(target);
      if (command.owes_stop()) {
        // Flush every STOP copy still owed: the full repeat count if the STOP
        // had not begun dispatching, or just the remaining copies if it was
        // displaced mid-STOP. They go on air one per pacing gap.
        const int copies = command.phase == Phase::STOP ? command.remaining : command.repeats;
        for (int index = 0; index < copies; index++)
          this->flush_stops_.push_back(command.stop_raw);
      }
      displaced_ids.push_back(command.command_id);
      this->erase_(target);
    }
  }

  bool seen_recently_(const std::string &command_id) const {
    return std::find(this->recent_ids_.begin(), this->recent_ids_.end(), command_id) !=
           this->recent_ids_.end();
  }

  void remember_(const std::string &command_id) {
    this->recent_ids_[this->recent_cursor_] = command_id;
    this->recent_cursor_ = (this->recent_cursor_ + 1) % COMMAND_ID_RING_SIZE;
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
  std::vector<std::string> flush_stops_;
  std::array<std::string, COMMAND_ID_RING_SIZE> recent_ids_{};
  size_t recent_cursor_{0};
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
