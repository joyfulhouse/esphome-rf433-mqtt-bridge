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
// Upper bound on the RF airtime one B0 frame may request (bucket durations are
// 16-bit microseconds and every data nibble spends one bucket, multiplied by
// the embedded hardware repeat). A structurally valid frame could otherwise
// request minutes of exclusive EFM8BB1 time, starving every queued fail-safe
// STOP. A real AOK frame runs ~550 ms at the controller's embedded repeat of
// 8; two seconds admits the full legal repeat range with margin.
constexpr uint64_t MAX_FRAME_AIRTIME_US = 2000000;
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
  if (data_start == body_end) {
    // Structurally valid but transmits nothing; scheduling it would only
    // burn dispatch slots on empty UART handoffs.
    reason = "frame contains no pulse data";
    return false;
  }
  std::array<uint32_t, 8> bucket_us{};
  for (size_t bucket = 0; bucket < bucket_count; bucket++) {
    for (size_t nibble = 0; nibble < 4; nibble++) {
      bucket_us[bucket] = (bucket_us[bucket] << 4) |
                          static_cast<uint32_t>(hex_value(output[10 + bucket * 4 + nibble]));
    }
  }
  uint64_t airtime_us = 0;
  for (size_t index = data_start; index < body_end; index++) {
    const size_t bucket = static_cast<size_t>(hex_value(output[index]) & 0x07);
    if (bucket >= bucket_count) {
      reason = "frame references an undefined bucket";
      return false;
    }
    airtime_us += bucket_us[bucket];
  }
  if (airtime_us * embedded_repeat > MAX_FRAME_AIRTIME_US) {
    reason = "frame requested airtime exceeds limit";
    return false;
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
    // A command still active (scheduled, or displaced and draining STOPs) or
    // recently completed (ring) is a duplicate. Checking live state as well
    // as the ring means a redelivery of a still-active command whose ring
    // slot was evicted cannot be re-admitted and physically re-run.
    if (this->seen_recently_(command_id) || this->is_active_(command_id)) {
      reason = "duplicate command_id";
      return false;
    }

    const size_t command_bytes = raw.size() + trailer_raw.size() + stop_raw.size();
    size_t retained_bytes = 0;
    size_t retained_targets = 0;
    for (const auto &item : this->commands_) {
      if (item.second.identity == identity && (item.second.mask & mask) != 0) {
        // Displaced below: its command storage is released, but if it still
        // owes a fail-safe STOP that frame moves to the flush queue and keeps
        // holding heap, so it stays in the admission budget.
        if (item.second.owes_stop())
          retained_bytes += item.second.stop_raw.size();
        continue;
      }
      retained_bytes += item.second.stored_bytes();
      retained_targets++;
    }
    // Fail-safe STOPs already queued for flush hold heap too. Each entry
    // stores its frame once with a send count, so this stays bounded by the
    // number of displacements rather than displaced_repeats * frame size.
    size_t flush_bytes = 0;
    for (const FlushStop &entry : this->flush_stops_)
      flush_bytes += entry.raw.size();
    if (retained_targets >= MAX_TARGETS) {
      // State-dependent rejection: remembered so a QoS-1 redelivery of this
      // command_id cannot be silently admitted after capacity drains.
      this->remember_(command_id, 3);
      reason = "target scheduler is full";
      return false;
    }
    if (retained_bytes + flush_bytes + command_bytes > MAX_TOTAL_FRAME_BYTES) {
      this->remember_(command_id, 3);
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
    this->remember_(command_id, 1);
    reason.clear();
    return true;
  }

  // Remembered lifecycle of a recent command_id, for answering QoS-1 broker
  // redeliveries idempotently: 0 = unknown (not a duplicate), 1 = admitted
  // but RF not yet started, 2 = admitted and RF started (started_age_ms is
  // set to how long ago), 3 = rejected by a STATE-DEPENDENT check (scheduler
  // full / storage budget) whose outcome must not silently flip on a later
  // redelivery, 4 = displaced after admission (replaying accepted would let
  // the controller rebuild a retired motion). Deterministic validation
  // rejections are not remembered -- a redelivery re-validates identically,
  // which is already idempotent.
  int replay_state(const std::string &command_id, uint32_t now_ms,
                   uint32_t &started_age_ms) const {
    started_age_ms = 0;
    // Live scheduler state is authoritative for a still-active command and
    // survives ring eviction: a currently scheduled command answers from
    // commands_, and a displaced command still draining its fail-safe STOPs
    // answers "displaced" from flush_stops_. Only once a command has fully
    // left the scheduler does the RAM dedup ring supply its remembered
    // outcome. This is why ring eviction of an active id's stale slot is
    // harmless -- the ring is never the source of truth for an active id.
    for (const auto &item : this->commands_) {
      if (item.second.command_id == command_id) {
        if (item.second.started) {
          started_age_ms = now_ms - item.second.started_at_ms;
          return 2;
        }
        return 1;
      }
    }
    for (const FlushStop &entry : this->flush_stops_) {
      if (entry.command_id == command_id)
        return 4;
    }
    for (const RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        if (recent.state == 2)
          started_age_ms = now_ms - recent.started_at_ms;
        return recent.state;
      }
    }
    return 0;
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

    // Arm due fail-safe STOPs before the flush branch so displaced-STOP
    // flushing can alternate fairly with them.
    for (const std::string &target : this->order_) {
      Command &command = this->commands_.at(target);
      if (command.deadline_armed && command.phase != Phase::STOP &&
          due_(now_ms, command.deadline_at)) {
        command.phase = Phase::STOP;
        command.remaining = command.repeats;
        command.next_at = now_ms;
      }
    }

    // Fail-safe STOPs of displaced commands go on air ahead of actions, but
    // ROTATE among flush entries and ALTERNATE with due scheduled STOPs: one
    // displaced command's full repeat train must not delay every other
    // motor's FIRST stop copy (with N owed stops, each first copy lands
    // within ~N pacing gaps instead of repeats * gaps).
    if (!this->flush_stops_.empty()) {
      bool scheduled_stop_due = false;
      for (const auto &item : this->commands_) {
        if (item.second.phase == Phase::STOP && due_(now_ms, item.second.next_at)) {
          scheduled_stop_due = true;
          break;
        }
      }
      if (!(this->flush_last_ && scheduled_stop_due)) {
        FlushStop entry = std::move(this->flush_stops_.front());
        this->flush_stops_.erase(this->flush_stops_.begin());
        const std::string raw = entry.raw;
        if (--entry.remaining > 0)
          this->flush_stops_.push_back(std::move(entry));
        this->next_rf_at_ = now_ms + this->repeat_gap_ms_;
        this->flush_last_ = true;
        return raw;
      }
      this->flush_last_ = false;
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
          command.started_at_ms = now_ms;
          started_command_id = command.command_id;
          this->mark_started_(command.command_id, now_ms);
          if (command.stop_after_ms > 0) {
            command.deadline_armed = true;
            command.deadline_at = now_ms + command.stop_after_ms;
          }
        }
        command.remaining--;
        const bool complete = command.remaining == 0 && this->advance_(command);
        if (command.remaining > 0)
          command.next_at = now_ms + this->repeat_gap_ms_;

        this->next_rf_at_ = now_ms + this->repeat_gap_ms_;
        this->flush_last_ = false;
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

  // One displaced command's owed fail-safe STOP: the frame stored once plus
  // how many copies still need to go on air.
  struct FlushStop {
    std::string raw;
    int remaining{0};
    std::string command_id;
  };

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
    uint32_t started_at_ms{0};
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
        // displaced mid-STOP. They go on air one per pacing gap; the frame is
        // stored once with its send count.
        const int copies = command.phase == Phase::STOP ? command.remaining : command.repeats;
        this->flush_stops_.push_back(FlushStop{command.stop_raw, copies, command.command_id});
      }
      this->mark_displaced_(command.command_id);
      displaced_ids.push_back(command.command_id);
      this->erase_(target);
    }
  }

  bool seen_recently_(const std::string &command_id) const {
    for (const RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id)
        return true;
    }
    return false;
  }

  bool is_active_(const std::string &command_id) const {
    if (command_id.empty())
      return false;
    for (const auto &item : this->commands_) {
      if (item.second.command_id == command_id)
        return true;
    }
    // A displaced command still draining its fail-safe STOPs is active too:
    // evicting its memory would let a duplicate re-run it mid-flush.
    for (const FlushStop &entry : this->flush_stops_) {
      if (entry.command_id == command_id)
        return true;
    }
    return false;
  }

  void remember_(const std::string &command_id, uint8_t state) {
    // Never evict the memory of a command still scheduled (a timed command
    // can sit in WAIT_STOP up to an hour): forgetting it would let a QoS-1
    // redelivery displace and physically re-run its own live command. The
    // ring (32) is larger than MAX_TARGETS (16), so a free slot always
    // exists within one sweep; the fallback is unreachable but safe.
    for (size_t probe = 0; probe < COMMAND_ID_RING_SIZE; probe++) {
      RecentCommand &slot = this->recent_ids_[this->recent_cursor_];
      this->recent_cursor_ = (this->recent_cursor_ + 1) % COMMAND_ID_RING_SIZE;
      if (!this->is_active_(slot.command_id)) {
        slot = RecentCommand{command_id, state, 0};
        return;
      }
    }
    this->recent_ids_[this->recent_cursor_] = RecentCommand{command_id, state, 0};
    this->recent_cursor_ = (this->recent_cursor_ + 1) % COMMAND_ID_RING_SIZE;
  }

  void mark_displaced_(const std::string &command_id) {
    for (RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        recent.state = 4;
        return;
      }
    }
  }

  void mark_started_(const std::string &command_id, uint32_t now_ms) {
    for (RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        recent.state = 2;
        recent.started_at_ms = now_ms;
        return;
      }
    }
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

  // Phase transitions leave next_at alone: the caller re-arms pacing for any
  // command with remaining repeats, and WAIT_STOP is gated purely by its
  // armed deadline (the dispatch loop skips the phase regardless of next_at).
  bool advance_(Command &command) {
    if (command.phase == Phase::ACTION && !command.trailer_raw.empty()) {
      command.phase = Phase::TRAILER;
      command.remaining = command.repeats;
      return false;
    }
    if ((command.phase == Phase::ACTION || command.phase == Phase::TRAILER) && command.stop_after_ms > 0) {
      command.phase = Phase::WAIT_STOP;
      command.remaining = command.repeats;
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

  // One recent command's remembered lifecycle, used to answer QoS-1 broker
  // redeliveries idempotently instead of rejecting (or re-running) the
  // duplicate. state: 1 admitted, 2 started (at started_at_ms), 3 rejected
  // by a state-dependent admission check.
  struct RecentCommand {
    std::string command_id;
    uint8_t state{0};
    uint32_t started_at_ms{0};
  };

  uint32_t repeat_gap_ms_;
  std::optional<uint32_t> next_rf_at_;
  size_t cursor_{0};
  bool flush_last_{false};
  std::map<std::string, Command> commands_;
  std::vector<std::string> order_;
  std::vector<FlushStop> flush_stops_;
  std::array<RecentCommand, COMMAND_ID_RING_SIZE> recent_ids_{};
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
