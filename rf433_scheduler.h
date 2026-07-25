#pragma once

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace rf433 {

constexpr size_t MAX_B0_FRAME_BYTES = 260;
// A valid frame is at most MAX_B0_FRAME_BYTES*2 hex chars (no whitespace); this
// generous 4x input cap tolerates hand-entered spacing while rejecting a hostile
// whitespace-padded /tx field BEFORE normalize_b0 reserves its length, so it
// cannot force a multi-kilobyte transient heap allocation on the ESP8285.
constexpr size_t MAX_B0_INPUT_CHARS = MAX_B0_FRAME_BYTES * 4;
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
// A user pacing preference is never allowed to approach the 2^31 ms signed
// serial-arithmetic horizon used by due_(). Sixty seconds is already far above
// any useful repeat cadence while leaving >35,000x headroom for rollover-safe
// comparisons. Invalid substitutions are clamped at construction, before a
// negative value can survive as a huge unsigned delay.
constexpr uint32_t MAX_REPEAT_GAP_MS = 60000;
// Reserve no more than four seconds of aggregate physical occupancy for one
// fail-safe STOP copy per concurrent timed obligation. Together with at most
// one already in-flight legal frame (~2.14 s including UART and margin), this
// bounds a newly due first STOP far below the former ~33 s 16-target case while
// admitting several normal ~0.56 s AOK STOP frames.
constexpr uint32_t MAX_FIRST_STOP_OCCUPANCY_MS = 4000;
// Recently admitted command IDs, used to drop QoS-1 broker redeliveries and
// same-boot retained replays. The ring lives in RAM, so it cannot suppress a
// retained command replayed after a reboot -- retained tx publishes are
// unsupported (see README). Sized well above the maximum simultaneously
// active id count (MAX_TARGETS scheduled + up to MAX_TARGETS displaced STOPs
// draining in flush_stops_ = 32) so remember_ always finds an inactive slot
// and never overwrites a live id, leaving a full dedup window for completed
// ids. Re-run protection does not depend on this bound (replay_state and
// schedule() dedup consult live scheduler state), but the headroom keeps the
// completed-id window intact even at peak occupancy.
constexpr size_t COMMAND_ID_RING_SIZE = 64;

enum class LifecycleKind : uint8_t {
  ACCEPTED,
  REJECTED,
  STARTED,
  DISPLACED,
  DISARMED,
};

struct LifecycleEvent {
  LifecycleKind kind{LifecycleKind::ACCEPTED};
  std::string command_id;
  std::string reason;
  uint32_t age_ms{0};
  uint32_t timestamp_ms{0};
  uint32_t boot_id{0};
  bool has_age{false};
  bool has_clock{false};

  static LifecycleEvent accepted(const std::string &command_id) {
    return make_(LifecycleKind::ACCEPTED, command_id);
  }

  static LifecycleEvent rejected(const std::string &command_id, const std::string &reason) {
    LifecycleEvent event = make_(LifecycleKind::REJECTED, command_id);
    event.reason = reason;
    return event;
  }

  static LifecycleEvent started(const std::string &command_id, uint32_t age_ms,
                                uint32_t timestamp_ms, uint32_t boot_id) {
    LifecycleEvent event = make_(LifecycleKind::STARTED, command_id);
    event.age_ms = age_ms;
    event.timestamp_ms = timestamp_ms;
    event.boot_id = boot_id;
    event.has_age = true;
    event.has_clock = true;
    return event;
  }

  static LifecycleEvent displaced(const std::string &command_id) {
    return make_(LifecycleKind::DISPLACED, command_id);
  }

  // A displacement whose original instant is known. age_ms is how long ago the
  // firmware performed the displacement; timestamp_ms/boot_id anchor it on the
  // firmware clock exactly as started() does, so the controller can budget its
  // post-displacement flush window from the instant itself instead of inferring
  // it from other measurements.
  static LifecycleEvent displaced(const std::string &command_id, uint32_t age_ms,
                                  uint32_t timestamp_ms, uint32_t boot_id) {
    LifecycleEvent event = make_(LifecycleKind::DISPLACED, command_id);
    event.age_ms = age_ms;
    event.timestamp_ms = timestamp_ms;
    event.boot_id = boot_id;
    event.has_age = true;
    event.has_clock = true;
    return event;
  }

  static LifecycleEvent disarmed(const std::string &command_id, uint32_t timestamp_ms,
                                 uint32_t boot_id) {
    LifecycleEvent event = make_(LifecycleKind::DISARMED, command_id);
    event.timestamp_ms = timestamp_ms;
    event.boot_id = boot_id;
    event.has_clock = true;
    return event;
  }

  const char *status() const {
    switch (this->kind) {
      case LifecycleKind::ACCEPTED:
        return "accepted";
      case LifecycleKind::REJECTED:
        return "rejected";
      case LifecycleKind::STARTED:
        return "started";
      case LifecycleKind::DISPLACED:
        return "displaced";
      case LifecycleKind::DISARMED:
        return "disarmed";
    }
    return "rejected";
  }

 protected:
  static LifecycleEvent make_(LifecycleKind kind, const std::string &command_id) {
    LifecycleEvent event;
    event.kind = kind;
    event.command_id = command_id;
    return event;
  }
};

// ESPHome's MQTT client retries a failed QoS enqueue only once immediately.
// Keep lifecycle truth across longer disconnects in a fixed FIFO. Repeated
// callbacks for the same command transition coalesce in place; transitions
// retain lifecycle order per command (accepted before started) even if a
// broker replay arrives after a later phase was queued. Sustained overload
// drops the oldest event and increments dropped_count_ instead of growing heap
// without bound.
class LifecycleOutbox {
 public:
  static constexpr size_t CAPACITY = 32;

  bool empty() const { return this->size_ == 0; }
  size_t size() const { return this->size_; }
  uint32_t dropped_count() const { return this->dropped_count_; }

  template<typename Publisher>
  bool publish_or_enqueue(const LifecycleEvent &event, Publisher &&publish) {
    if (this->empty() && publish(event))
      return true;
    this->enqueue_(event);
    return false;
  }

  template<typename Publisher> size_t flush(Publisher &&publish) {
    size_t published = 0;
    while (!this->empty()) {
      if (!publish(this->events_[0]))
        break;
      this->pop_front_();
      published++;
    }
    return published;
  }

 protected:
  void enqueue_(const LifecycleEvent &event) {
    for (size_t index = 0; index < this->size_; index++) {
      LifecycleEvent &queued = this->events_[index];
      if (queued.command_id == event.command_id && queued.kind == event.kind) {
        queued = event;
        return;
      }
    }
    size_t insertion = this->size_;
    for (size_t index = 0; index < this->size_; index++) {
      const LifecycleEvent &queued = this->events_[index];
      if (queued.command_id == event.command_id &&
          lifecycle_rank_(event.kind) < lifecycle_rank_(queued.kind)) {
        insertion = index;
        break;
      }
    }
    if (this->size_ == CAPACITY) {
      this->pop_front_();
      this->dropped_count_++;
      if (insertion > 0)
        insertion--;
    }
    for (size_t index = this->size_; index > insertion; index--)
      this->events_[index] = std::move(this->events_[index - 1]);
    this->events_[insertion] = event;
    this->size_++;
  }

  static uint8_t lifecycle_rank_(LifecycleKind kind) {
    switch (kind) {
      case LifecycleKind::ACCEPTED:
      case LifecycleKind::REJECTED:
        return 0;
      case LifecycleKind::STARTED:
        return 1;
      case LifecycleKind::DISPLACED:
      case LifecycleKind::DISARMED:
        return 2;
    }
    return 0;
  }

  void pop_front_() {
    if (this->empty())
      return;
    for (size_t index = 1; index < this->size_; index++)
      this->events_[index - 1] = std::move(this->events_[index]);
    this->events_[--this->size_] = LifecycleEvent{};
  }

  std::array<LifecycleEvent, CAPACITY> events_{};
  size_t size_{0};
  uint32_t dropped_count_{0};
};

inline int hex_value(char value) {
  if (value >= '0' && value <= '9')
    return value - '0';
  if (value >= 'A' && value <= 'F')
    return value - 'A' + 10;
  return -1;
}

inline bool normalize_b0_with_airtime(const std::string &input, std::string &output,
                                      std::string &reason, uint64_t &frame_airtime_us) {
  output.clear();
  frame_airtime_us = 0;
  if (input.size() > MAX_B0_INPUT_CHARS) {
    // Reject before reserving: the normalized frame can only be shorter, so a
    // valid frame never trips this, but a padded hostile input is bounded here.
    reason = "frame exceeds maximum size";
    return false;
  }
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
  const uint64_t total_airtime_us = airtime_us * embedded_repeat;
  if (total_airtime_us > MAX_FRAME_AIRTIME_US) {
    reason = "frame requested airtime exceeds limit";
    return false;
  }
  frame_airtime_us = total_airtime_us;
  reason.clear();
  return true;
}

inline bool normalize_b0(const std::string &input, std::string &output, std::string &reason) {
  uint64_t frame_airtime_us = 0;
  return normalize_b0_with_airtime(input, output, reason, frame_airtime_us);
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
  struct EmergencyStop {
    std::string raw;
    uint32_t occupancy_ms{0};
  };

  explicit TargetScheduler(int64_t repeat_gap_ms)
      : repeat_gap_ms_(static_cast<uint32_t>(
            std::clamp<int64_t>(repeat_gap_ms, 0, MAX_REPEAT_GAP_MS))) {}

  bool idle() const { return this->commands_.empty() && this->flush_stops_.empty(); }

  bool rf_air_clear(uint32_t now_ms) const {
    if (!this->rf_dispatched_ || due_(now_ms, this->rf_busy_until_))
      return true;
    // A legal physical hold is <2.2 s. If serial arithmetic says "before"
    // while the unsigned forward distance is over 60 s, the observation is
    // actually more than 2^31 ms after a stale deadline, not before a current
    // one. This preserves idle recovery across the full millis() rollover.
    return static_cast<uint32_t>(this->rf_busy_until_ - now_ms) > MAX_REPEAT_GAP_MS;
  }

  // OTA begin cannot be vetoed safely on every supported transport, so convert
  // every already-armed timed obligation into one immediate STOP handoff. One
  // B0 STOP already contains its embedded RF repeats; scheduler-level copies
  // improve reliability during normal operation but must not hold up the
  // blocking update. Unstarted commands have no armed STOP and are discarded
  // without emitting their ACTION. The caller waits occupancy_ms after each
  // synchronous UART write, preserving the physical RF constraint.
  std::vector<EmergencyStop> drain_armed_stops() {
    std::vector<EmergencyStop> stops;
    stops.reserve(this->flush_stops_.size() + this->commands_.size());
    for (const FlushStop &entry : this->flush_stops_) {
      stops.push_back(
          EmergencyStop{entry.raw, frame_occupancy_ms_(entry.airtime_ms, entry.raw.size())});
    }
    for (const std::string &target : this->order_) {
      const Command &command = this->commands_.at(target);
      if (command.owes_stop()) {
        stops.push_back(EmergencyStop{
            command.stop_raw,
            frame_occupancy_ms_(command.stop_airtime_ms, command.stop_raw.size()),
        });
      }
    }
    this->commands_.clear();
    this->order_.clear();
    this->flush_stops_.clear();
    this->cursor_ = 0;
    this->flush_last_ = false;
    this->next_user_at_.reset();
    return stops;
  }

  // Abort every future frame for this id and retain the same terminal state
  // used by displacement so a reordered original command cannot be admitted.
  void disarm(const std::string &command_id) {
    std::string target;
    for (const auto &item : this->commands_) {
      if (item.second.command_id == command_id) {
        target = item.first;
        break;
      }
    }
    if (!target.empty())
      this->erase_(target);
    this->flush_stops_.erase(
        std::remove_if(this->flush_stops_.begin(), this->flush_stops_.end(),
                       [&command_id](const FlushStop &entry) { return entry.command_id == command_id; }),
        this->flush_stops_.end());
    this->remember_(command_id, 4);
  }

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

    uint64_t first_stop_occupancy_ms = 0;
    for (const auto &item : this->commands_) {
      const Command &command = item.second;
      if (command.identity == identity && (command.mask & mask) != 0) {
        // A started overlapping command moves into flush_stops_ below and
        // keeps one STOP reservation. An unstarted command is simply replaced
        // and has no armed fail-safe obligation.
        if (command.owes_stop()) {
          first_stop_occupancy_ms +=
              frame_occupancy_ms_(command.stop_airtime_ms, command.stop_raw.size());
        }
      } else if (command.stop_after_ms > 0 && !command.stop_raw.empty()) {
        first_stop_occupancy_ms +=
            frame_occupancy_ms_(command.stop_airtime_ms, command.stop_raw.size());
      }
    }
    for (const FlushStop &entry : this->flush_stops_) {
      first_stop_occupancy_ms +=
          frame_occupancy_ms_(entry.airtime_ms, entry.raw.size());
    }
    if (stop_after_ms > 0 && !stop_raw.empty()) {
      const uint32_t stop_airtime_ms = frame_airtime_ms_(stop_raw);
      first_stop_occupancy_ms +=
          frame_occupancy_ms_(stop_airtime_ms, stop_raw.size());
    }
    if (first_stop_occupancy_ms > MAX_FIRST_STOP_OCCUPANCY_MS) {
      this->remember_(command_id, 3);
      reason = "first-STOP safety budget exceeded";
      return false;
    }

    this->displace_overlapping_(identity, mask, now_ms, displaced_ids);

    Command command;
    command.command_id = command_id;
    command.identity = identity;
    command.mask = mask;
    command.raw = raw;
    command.trailer_raw = trailer_raw;
    command.stop_raw = stop_raw;
    command.raw_airtime_ms = frame_airtime_ms_(raw);
    command.trailer_airtime_ms = frame_airtime_ms_(trailer_raw);
    command.stop_airtime_ms = frame_airtime_ms_(stop_raw);
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
  // full / storage / first-STOP safety budget) whose outcome must not silently
  // flip on a later redelivery, 4 = displaced after admission (replaying
  // accepted would let the controller rebuild a retired motion).
  // Deterministic validation rejections are not remembered -- a redelivery
  // re-validates identically, which is already idempotent.
  // age_ms is set to how long ago the reported instant occurred: the RF handoff
  // for a started command (state 2) or the displacement for a displaced one
  // (state 4). has_age, when supplied, reports whether age_ms is a real
  // measurement rather than a silent 0 -- a command displaced by displacement
  // carries a timestamp, but one that reached state 4 by disarm (or was
  // remembered without one) does not, and the caller must not publish a bogus
  // age_ms=0 for it. All timestamp deltas are unsigned millis() subtractions,
  // correct across the 2^32 rollover.
  int replay_state(const std::string &command_id, uint32_t now_ms,
                   uint32_t &age_ms, bool *has_age = nullptr) const {
    age_ms = 0;
    if (has_age != nullptr)
      *has_age = false;
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
          age_ms = now_ms - item.second.started_at_ms;
          if (has_age != nullptr)
            *has_age = true;
          return 2;
        }
        return 1;
      }
    }
    for (const FlushStop &entry : this->flush_stops_) {
      if (entry.command_id == command_id) {
        // A still-draining displaced command carries its displacement instant.
        age_ms = now_ms - entry.displaced_at_ms;
        if (has_age != nullptr)
          *has_age = true;
        return 4;
      }
    }
    for (const RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        // State 2 always recorded an RF-start instant; state 4 recorded a
        // displacement instant only when it reached state 4 by displacement.
        if ((recent.state == 2 || recent.state == 4) && recent.has_timestamp) {
          age_ms = now_ms - recent.started_at_ms;
          if (has_age != nullptr)
            *has_age = true;
        }
        return recent.state;
      }
    }
    return 0;
  }

  std::optional<std::string> next(uint32_t now_ms, std::string &started_command_id) {
    started_command_id.clear();
    // Age-based reset applies only to the discretionary user floor. Physical
    // occupancy has its own short, bounded rf_busy_until_ horizon and is never
    // bypassed. Resetting only when the floor is >60s stale preserves spacing
    // owed to a just-dispatched frame while preventing an idle gate from
    // surviving long enough to cross the signed comparison horizon.
    if (this->next_user_at_.has_value() &&
        static_cast<int32_t>(now_ms - *this->next_user_at_) > 60000)
      this->next_user_at_.reset();
    if (this->commands_.empty() && this->flush_stops_.empty())
      return std::nullopt;

    // Arm due fail-safe STOPs before consulting either gate. In particular,
    // the discretionary repeat gap must never hide that a STOP is due.
    for (const std::string &target : this->order_) {
      Command &command = this->commands_.at(target);
      if (command.deadline_armed && command.phase != Phase::STOP &&
          due_(now_ms, command.deadline_at)) {
        command.phase = Phase::STOP;
        command.remaining = command.repeats;
        command.next_at = now_ms;
      }
    }
    // UART serialization + RF airtime + margin is a hardware constraint for
    // every frame, including urgent STOPs.
    if (!this->rf_air_clear(now_ms))
      return std::nullopt;

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
        const uint32_t airtime_ms = entry.airtime_ms;
        if (--entry.remaining > 0)
          this->flush_stops_.push_back(std::move(entry));
        this->flush_last_ = true;
        this->record_dispatch_(now_ms, airtime_ms, uart_ms_(raw.size()));
        return raw;
      }
      this->flush_last_ = false;
    }

    const size_t count = this->order_.size();
    for (int stop_priority = 1; stop_priority >= 0; stop_priority--) {
      // Normal ACTION/TRAILER work honors the user preference globally.
      // Armed STOP work bypasses this floor after physical RF occupancy clears.
      if (stop_priority == 0 && this->next_user_at_.has_value() &&
          !due_(now_ms, *this->next_user_at_))
        continue;
      for (size_t offset = 0; offset < count; offset++) {
        const size_t index = (this->cursor_ + offset) % count;
        const std::string target = this->order_[index];
        Command &command = this->commands_.at(target);
        const bool is_stop = command.phase == Phase::STOP;
        if (is_stop != (stop_priority == 1) || command.phase == Phase::WAIT_STOP ||
            !due_(now_ms, command.next_at))
          continue;

        const std::string raw = this->phase_raw_(command);
        const uint32_t airtime_ms = this->phase_airtime_ms_(command);
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
        if (command.remaining > 0) {
          command.next_at =
              command.phase == Phase::STOP ? now_ms : now_ms + this->repeat_gap_ms_;
        }

        this->flush_last_ = false;
        this->cursor_ = (index + 1) % count;
        if (complete)
          this->erase_(target);
        this->record_dispatch_(now_ms, airtime_ms, uart_ms_(raw.size()));
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
    uint32_t airtime_ms{0};
    // millis() when the owning command was displaced, so a redelivery arriving
    // while the STOP still drains reports its age since that instant, not 0.
    uint32_t displaced_at_ms{0};
  };

  struct Command {
    std::string command_id;
    std::string identity;
    uint16_t mask{0};
    std::string raw;
    std::string trailer_raw;
    std::string stop_raw;
    uint32_t raw_airtime_ms{0};
    uint32_t trailer_airtime_ms{0};
    uint32_t stop_airtime_ms{0};
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

  void displace_overlapping_(const std::string &identity, uint16_t mask, uint32_t now_ms,
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
        this->flush_stops_.push_back(FlushStop{command.stop_raw, copies, command.command_id,
                                               command.stop_airtime_ms, now_ms});
      }
      this->mark_displaced_(command.command_id, now_ms);
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
    // Prefer to evict an inactive slot. If flush_stops_ ever pushes the
    // active-id count past the ring size, the fallback overwrites the oldest
    // slot even if it is active -- which is safe because re-run protection no
    // longer depends on the ring: replay_state() and schedule()'s dedup both
    // consult live scheduler state (commands_/flush_stops_) authoritatively,
    // so an evicted active id is still recognized as a duplicate. The ring
    // only shortens the dedup window for already-completed command ids.
    for (RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        recent = RecentCommand{command_id, state, false, 0};
        return;
      }
    }
    for (size_t probe = 0; probe < COMMAND_ID_RING_SIZE; probe++) {
      RecentCommand &slot = this->recent_ids_[this->recent_cursor_];
      this->recent_cursor_ = (this->recent_cursor_ + 1) % COMMAND_ID_RING_SIZE;
      if (!this->is_active_(slot.command_id)) {
        slot = RecentCommand{command_id, state, false, 0};
        return;
      }
    }
    this->recent_ids_[this->recent_cursor_] = RecentCommand{command_id, state, false, 0};
    this->recent_cursor_ = (this->recent_cursor_ + 1) % COMMAND_ID_RING_SIZE;
  }

  // Update an existing ring slot's remembered lifecycle in place. timestamp_ms
  // is the RF-start instant for state 2 and the displacement instant for state
  // 4; has_timestamp records whether it is a real instant so a later redelivery
  // does not report a bogus age of 0. remember_() writes has_timestamp=false,
  // which is why a state-4 slot created by disarm carries no age.
  void mark_recent_(const std::string &command_id, uint8_t state, bool has_timestamp,
                    uint32_t timestamp_ms) {
    for (RecentCommand &recent : this->recent_ids_) {
      if (recent.command_id == command_id) {
        recent.state = state;
        recent.has_timestamp = has_timestamp;
        recent.started_at_ms = timestamp_ms;
        return;
      }
    }
  }

  void mark_displaced_(const std::string &command_id, uint32_t now_ms) {
    this->mark_recent_(command_id, 4, true, now_ms);
  }

  void mark_started_(const std::string &command_id, uint32_t now_ms) {
    this->mark_recent_(command_id, 2, true, now_ms);
  }

  static bool due_(uint32_t now_ms, uint32_t deadline_ms) {
    return static_cast<int32_t>(now_ms - deadline_ms) >= 0;
  }

  static uint32_t frame_airtime_ms_(const std::string &raw) {
    // A string that is not even B0-shaped is never transmitted by the
    // EFM8BB1 (its command parser ignores the bytes), so it occupies no air.
    const bool b0_shaped = raw.size() >= 10 &&
                           std::toupper(static_cast<unsigned char>(raw[0])) == 'A' &&
                           std::toupper(static_cast<unsigned char>(raw[1])) == 'A' &&
                           std::toupper(static_cast<unsigned char>(raw[2])) == 'B' && raw[3] == '0';
    if (!b0_shaped)
      return 0;
    std::string normalized;
    std::string reason;
    uint64_t airtime_us = 0;
    // MQTT admission supplies validated frames. Keep direct callers that
    // bypass it conservative without duplicating the B0 bucket parser: a
    // B0-shaped frame with an underivable airtime holds both the receive
    // re-arm and the next dispatch for the full ceiling.
    if (!normalize_b0_with_airtime(raw, normalized, reason, airtime_us))
      airtime_us = MAX_FRAME_AIRTIME_US;
    return static_cast<uint32_t>((airtime_us + 999U) / 1000U);
  }

  // UART serialization time for a hex-encoded frame at the fixed 19200-baud
  // EFM8BB1 link (hex_chars/2 bytes x 10 bits, ceiled to ms). The dispatch
  // timestamp is taken BEFORE the ESP serializes the frame over UART, and the
  // coprocessor only starts RF once the trailer byte arrives, so the frame's
  // real occupancy is serialization + airtime measured from that timestamp.
  // An airtime-only hold under-covered the tail by the serialization time
  // (~45 ms for a production AOK frame, ~135 ms at MAX_B0_FRAME_BYTES) and
  // could let the next handoff reach the EFM8's ~64-byte UART ring while RF
  // was still on air.
  static uint32_t uart_ms_(size_t hex_chars) {
    return static_cast<uint32_t>((static_cast<uint64_t>(hex_chars) * 5000U + 19199U) / 19200U);
  }

  static uint32_t frame_occupancy_ms_(uint32_t airtime_ms, size_t hex_chars) {
    return airtime_ms > 0 ? uart_ms_(hex_chars) + airtime_ms + RF_AIRTIME_MARGIN_MS
                          : RF_AIRTIME_MARGIN_MS;
  }

  // Single owner of the dispatch-timing invariant: one call records both the
  // pacing hold for the next UART handoff and the RF-air-busy horizon used by
  // rf_air_clear(). The EFM8BB1 transmits a B0 frame blocking (embedded
  // repeats included) behind a ~64-byte UART ring, so a frame handed off
  // mid-transmission corrupts in that ring instead of pacing (field-observed:
  // 10 rapid dispatches, one completion ACK). Zero/unknown airtimes keep the
  // plain repeat gap.
  void record_dispatch_(uint32_t now_ms, uint32_t airtime_ms, uint32_t serialize_ms) {
    // Serialization is charged only when the frame will actually key RF
    // (airtime > 0). A non-B0 frame's bytes drain through the EFM8's ring
    // concurrently (its parser discards them without transmitting), so
    // charging its UART time would only slow fail-safe STOP flushing.
    const uint32_t occupancy_ms =
        airtime_ms > 0 ? serialize_ms + airtime_ms + RF_AIRTIME_MARGIN_MS
                       : RF_AIRTIME_MARGIN_MS;
    this->rf_busy_until_ = now_ms + occupancy_ms;
    this->rf_dispatched_ = true;
    this->next_user_at_ = now_ms + this->repeat_gap_ms_;
  }

  const std::string &phase_raw_(const Command &command) const {
    if (command.phase == Phase::TRAILER)
      return command.trailer_raw;
    if (command.phase == Phase::STOP)
      return command.stop_raw;
    return command.raw;
  }

  uint32_t phase_airtime_ms_(const Command &command) const {
    if (command.phase == Phase::TRAILER)
      return command.trailer_airtime_ms;
    if (command.phase == Phase::STOP)
      return command.stop_airtime_ms;
    return command.raw_airtime_ms;
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
  // by a state-dependent admission check, 4 displaced or disarmed (terminal).
  // started_at_ms holds the RF-start instant (state 2) or the displacement
  // instant (state 4 reached by displacement); has_timestamp is false when no
  // real instant was recorded (state 4 reached by disarm, or any remember_()
  // slot) so a redelivery reports no age instead of a bogus 0. has_timestamp
  // occupies padding after state, so RecentCommand does not grow.
  struct RecentCommand {
    std::string command_id;
    uint8_t state{0};
    bool has_timestamp{false};
    uint32_t started_at_ms{0};
  };

  // Keep one 5 ms scheduler tick beyond the ceiled frame airtime before RX.
  static constexpr uint32_t RF_AIRTIME_MARGIN_MS = 5;
  uint32_t repeat_gap_ms_;
  std::optional<uint32_t> next_user_at_;
  uint32_t rf_busy_until_{0};
  size_t cursor_{0};
  bool rf_dispatched_{false};
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
inline TargetScheduler &tx_scheduler(int64_t repeat_gap_ms) {
  static TargetScheduler instance(repeat_gap_ms);
  return instance;
}

inline LifecycleOutbox &lifecycle_outbox() {
  static LifecycleOutbox instance;
  return instance;
}

}  // namespace rf433
