#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::rf_bridge {

constexpr uint8_t B1_MIN_BUCKETS = 3;
constexpr uint8_t B1_MAX_BUCKETS = 8;
constexpr size_t B1_MIN_PULSE_BYTES = 67;
constexpr size_t B1_MAX_PULSE_BYTES = 69;
constexpr uint32_t B1_CANDIDATE_QUIET_MS = 5;
constexpr uint16_t AOK_SYNC_MIN_US = 1000;
constexpr uint16_t AOK_BIT_MAX_US = 1000;
constexpr uint16_t AOK_SHORT_MAX_US = 450;
constexpr size_t AOK_CAPTURE_PADDING_PULSES = 2;
constexpr size_t AOK_PAYLOAD_BITS = 64;
constexpr size_t AOK_TRAILER_BITS = 2;

enum class B1FrameStatus : uint8_t {
  INCOMPLETE,
  CANDIDATE,
  COMPLETE,
  INVALID,
};

inline size_t b1_data_start(const std::vector<uint8_t> &raw) {
  return 3U + static_cast<size_t>(raw[2]) * 2U;
}

inline uint16_t b1_bucket(const std::vector<uint8_t> &raw, size_t index) {
  const size_t offset = 3U + index * 2U;
  return static_cast<uint16_t>((static_cast<uint16_t>(raw[offset]) << 8) | raw[offset + 1]);
}

inline bool is_aok_bucket_frame(const std::vector<uint8_t> &raw) {
  if (raw.size() < 4 || raw[0] != 0xAA || raw[1] != 0xB1 || raw.back() != 0x55)
    return false;
  const uint8_t bucket_count = raw[2];
  if (bucket_count < B1_MIN_BUCKETS || bucket_count > B1_MAX_BUCKETS)
    return false;
  const size_t data_start = b1_data_start(raw);
  if (raw.size() <= data_start)
    return false;
  const size_t pulse_bytes = raw.size() - data_start - 1U;
  if (pulse_bytes < B1_MIN_PULSE_BYTES || pulse_bytes > B1_MAX_PULSE_BYTES)
    return false;

  uint16_t buckets[B1_MAX_BUCKETS]{};
  for (size_t index = 0; index < bucket_count; index++) {
    buckets[index] = b1_bucket(raw, index);
    if (buckets[index] > 0x7FFF)
      return false;
  }
  // Portisch appends the separately-detected footer/sync timing as the last
  // declared bucket. This cheap check drops most unrelated B1 traffic before
  // walking its pulse stream.
  if (buckets[bucket_count - 1] < AOK_SYNC_MIN_US)
    return false;

  const size_t pulse_count = pulse_bytes * 2U;
  auto pulse_nibble = [&](size_t index) {
    const uint8_t packed = raw[data_start + index / 2U];
    return static_cast<uint8_t>((index % 2U == 0) ? packed >> 4 : packed & 0x0F);
  };
  auto pulse_high = [&](size_t index) { return (pulse_nibble(index) & 0x08) != 0; };
  auto pulse_duration = [&](size_t index) -> uint16_t {
    const uint8_t bucket = pulse_nibble(index) & 0x07;
    return bucket < bucket_count ? buckets[bucket] : 0;
  };
  for (size_t index = 0; index < pulse_count; index++) {
    if ((pulse_nibble(index) & 0x07) >= bucket_count)
      return false;
  }

  size_t sync_index = pulse_count;
  for (size_t index = 0; index + 1U < pulse_count; index++) {
    if (!pulse_high(index) && pulse_high(index + 1U) && pulse_duration(index) >= AOK_SYNC_MIN_US &&
        pulse_duration(index + 1U) >= AOK_SYNC_MIN_US) {
      sync_index = index;
      break;
    }
  }
  if (sync_index == pulse_count || sync_index > AOK_CAPTURE_PADDING_PULSES)
    return false;
  for (size_t index = 0; index < sync_index; index++) {
    if (pulse_duration(index) < AOK_SYNC_MIN_US)
      return false;
  }

  const size_t encoded_start = sync_index + 2U;
  const size_t encoded_pulses = (AOK_PAYLOAD_BITS + AOK_TRAILER_BITS) * 2U;
  if (pulse_count < encoded_start + encoded_pulses)
    return false;
  const size_t trailing_start = encoded_start + encoded_pulses;
  if (pulse_count - trailing_start > AOK_CAPTURE_PADDING_PULSES)
    return false;
  for (size_t index = trailing_start; index < pulse_count; index++) {
    if (pulse_duration(index) < AOK_SYNC_MIN_US)
      return false;
  }

  uint8_t previous = 1;
  for (size_t bit_index = 0; bit_index < AOK_PAYLOAD_BITS + AOK_TRAILER_BITS; bit_index++) {
    const size_t low_index = encoded_start + bit_index * 2U;
    const size_t high_index = low_index + 1U;
    const uint16_t low_duration = pulse_duration(low_index);
    const uint16_t high_duration = pulse_duration(high_index);
    if (pulse_high(low_index) || !pulse_high(high_index) || low_duration >= AOK_BIT_MAX_US ||
        high_duration >= AOK_BIT_MAX_US)
      return false;
    const uint8_t low_previous = low_duration < AOK_SHORT_MAX_US ? 0 : 1;
    if (low_previous != previous)
      return false;
    const uint8_t bit = high_duration < AOK_SHORT_MAX_US ? 1 : 0;
    if (bit_index == AOK_PAYLOAD_BITS && bit != 1)
      return false;
    if (bit_index == AOK_PAYLOAD_BITS + 1U && bit != 0)
      return false;
    previous = bit;
  }
  return true;
}

inline B1FrameStatus b1_frame_status(const std::vector<uint8_t> &raw) {
  if (raw.empty())
    return B1FrameStatus::INCOMPLETE;
  if (raw[0] != 0xAA)
    return B1FrameStatus::INVALID;
  if (raw.size() == 1)
    return B1FrameStatus::INCOMPLETE;
  if (raw[1] != 0xB1)
    return B1FrameStatus::INVALID;
  if (raw.size() == 2)
    return B1FrameStatus::INCOMPLETE;
  const uint8_t bucket_count = raw[2];
  if (bucket_count < B1_MIN_BUCKETS || bucket_count > B1_MAX_BUCKETS)
    return B1FrameStatus::INVALID;

  const size_t data_start = b1_data_start(raw);
  if (raw.size() < data_start)
    return B1FrameStatus::INCOMPLETE;
  for (size_t index = 0; index < bucket_count; index++) {
    if (b1_bucket(raw, index) > 0x7FFF)
      return B1FrameStatus::INVALID;
  }
  if (b1_bucket(raw, bucket_count - 1U) < AOK_SYNC_MIN_US)
    return B1FrameStatus::INVALID;

  const size_t min_frame_size = data_start + B1_MIN_PULSE_BYTES + 1U;
  const size_t max_frame_size = data_start + B1_MAX_PULSE_BYTES + 1U;
  if (raw.size() < min_frame_size)
    return B1FrameStatus::INCOMPLETE;
  if (raw.size() > max_frame_size)
    return B1FrameStatus::INVALID;
  if (raw.back() == 0x55 && is_aok_bucket_frame(raw)) {
    // B1 has no B0-style total-length byte. AOK's fixed 64-bit envelope plus
    // bounded capture padding gives three derived candidate offsets. A 0x55
    // at either shorter offset can itself be a legitimate padding pulse byte,
    // so only the maximum offset is unambiguous without an inter-byte quiet
    // boundary. The component defers shorter candidates until that boundary.
    return raw.size() == max_frame_size ? B1FrameStatus::COMPLETE : B1FrameStatus::CANDIDATE;
  }
  return raw.size() == max_frame_size ? B1FrameStatus::INVALID : B1FrameStatus::INCOMPLETE;
}

inline std::string compact_hex(const std::vector<uint8_t> &raw) {
  static constexpr char HEX_DIGITS[] = "0123456789ABCDEF";
  std::string output;
  output.reserve(raw.size() * 2U);
  for (const uint8_t byte : raw) {
    output.push_back(HEX_DIGITS[byte >> 4]);
    output.push_back(HEX_DIGITS[byte & 0x0F]);
  }
  return output;
}

}  // namespace esphome::rf_bridge
