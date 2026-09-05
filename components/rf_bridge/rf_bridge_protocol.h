#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::rf_bridge {

// Wire framing bytes shared with rf_bridge.h's command set. They live in this
// dependency-free header so the native contract tests and the on-target
// component parse the same values instead of re-typed literals.
static const uint8_t RF_CODE_START = 0xAA;
static const uint8_t RF_CODE_RFIN_BUCKET = 0xB1;
static const uint8_t RF_CODE_STOP = 0x55;

constexpr uint8_t B1_MIN_BUCKETS = 3;
constexpr uint8_t B1_MAX_BUCKETS = 8;
// Shortest accepted capture: preamble byte + 65 bit-pair bytes. OEM remotes
// nominally encode 66 pairs (64 payload + [1, 0] trailer), but field captures
// confirm that some remotes' trailers capture one pair short.
constexpr size_t B1_MIN_PULSE_BYTES = 66;
constexpr size_t B1_MAX_PULSE_BYTES = 69;
constexpr uint32_t B1_CANDIDATE_QUIET_MS = 5;
constexpr uint16_t AOK_SYNC_MIN_US = 1000;
constexpr uint16_t AOK_BIT_MAX_US = 1000;
constexpr uint16_t AOK_SHORT_MAX_US = 450;
constexpr size_t AOK_CAPTURE_PADDING_PULSES = 2;
constexpr size_t AOK_PAYLOAD_BITS = 64;
constexpr size_t AOK_TRAILER_BITS = 2;

// Outbound B0 layout, as validated by rf433_scheduler.h's normalize_b0 before
// any frame reaches the transmitter: bucket count at hex chars 6..7, embedded
// repeat at 8..9, then one 4-hex-char big-endian microsecond duration per
// bucket, then the data nibbles.
constexpr size_t B0_BUCKET_TABLE_START = 10;
// Width of the AAB0 magic. A frame this long can be recognized as a B0 -- and
// so held to B0 rules -- before it is long enough to carry the header those
// rules read.
constexpr size_t B0_MAGIC_CHARS = 4;
// Whitespace a caller's frame may legitimately carry at either end. Reading a
// frame out of a text sensor, a template, or a file hands the lambda a trailing
// newline for free, and before compensation existed write_byte_str_'s
// pair-at-a-time loop simply ignored an odd trailing character. This is the
// ASCII set the scheduler's normalize_b0 strips at admission.
inline constexpr char B0_TRIM_CHARS[] = " \t\n\v\f\r";
// Floor for a compensated bucket duration -- and, since the issue-#18 fix,
// for every bucket a data nibble references: normalize_b0 rejects such frames
// at /tx admission, and send_raw floors referenced sub-floor buckets even at
// the default offset of 0. The OB38S003's Timer-1 ISR
// decrements its remaining-interval counter BEFORE testing it for zero, so a
// bucket that reaches zero wraps to 65,535 intervals -- roughly 659 ms of
// stuck carrier on a shared 433.92 MHz band. This floor sits far above any
// plausible timer quantum and far below the shortest real AOK bucket (280 us).
//
// It has two distinct outcomes, and only one of them is a rescue. A bucket
// LONGER than the offset but landing under 100 us is raised back to 100 us --
// shortened, but not destroyed. A bucket SHORTER than the offset would go
// negative, so it comes out INFLATED to 100 us: longer than the duration that
// was captured, not shorter. Either way the emitted bucket no longer carries
// the captured timing, so b0_with_bucket_offset counts BOTH in its
// clamped_buckets report and send_raw warns on any non-zero count.
constexpr uint16_t B0_MIN_BUCKET_US = 100;

// `inline`, not `static`: two inline functions below odr-use this object, and a
// `static` (internal-linkage) array in a header gives every translation unit its
// own copy, so those definitions would refer to different entities across TUs --
// ill-formed, no diagnostic required ([basic.def.odr]/12).
inline constexpr char HEX_DIGITS[] = "0123456789ABCDEF";

// Parse one hex character, or -1 if it is not one. Use this to ASK whether a
// character is valid hex.
inline int hex_nibble(char value) {
  if (value >= '0' && value <= '9')
    return value - '0';
  if (value >= 'A' && value <= 'F')
    return value - 'A' + 10;
  if (value >= 'a' && value <= 'f')
    return value - 'a' + 10;
  return -1;
}

// The nibble a character actually becomes ON THE WIRE. write_byte_str_ cannot
// signal failure mid-frame, so it coerces anything unparseable to 0; this
// function IS that rule, and it is the only expression of it in the codebase.
//
// Anything deciding what a frame WILL BE must use this, never hex_nibble.
// Judging characters while the UART emits bytes is what let
// `AABZ050108ZZZZ0055` through: hex_nibble('Z') is -1, so it failed the AAB0
// magic and was waved past every check as "not a B0 frame", then serialized to
// AA B0 05 01 08 00 00 00 55 -- a well-formed B0 frame carrying a zero bucket,
// and with it the ~659 ms stuck carrier, from untrusted MQTT input at every
// offset. One shared rule is what keeps the classifier's model of the wire from
// drifting away from the wire again.
inline uint8_t serialized_nibble(char value) {
  const int parsed = hex_nibble(value);
  return parsed < 0 ? 0U : static_cast<uint8_t>(parsed);
}

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
  if (raw.size() < 4 || raw[0] != RF_CODE_START || raw[1] != RF_CODE_RFIN_BUCKET || raw.back() != RF_CODE_STOP)
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
  // OEM remotes nominally encode 64 payload bits plus a [1, 0] trailer, but
  // some truncate the trailer on air so it captures as a single 0-read. A lone
  // 1-read cannot terminate a capture without its paired low. Accept the
  // 66-pair nominal form and the 65-pair truncation, longest first — a full
  // trailer's last pair can never be misread as padding because bit pulses
  // fail the padding's AOK_SYNC_MIN_US floor.
  for (size_t trailer_bits = AOK_TRAILER_BITS; trailer_bits + 1U >= AOK_TRAILER_BITS;
       trailer_bits--) {
    const size_t bit_count = AOK_PAYLOAD_BITS + trailer_bits;
    const size_t trailing_start = encoded_start + bit_count * 2U;
    if (pulse_count < trailing_start)
      continue;
    if (pulse_count - trailing_start > AOK_CAPTURE_PADDING_PULSES)
      continue;
    bool valid = true;
    for (size_t index = trailing_start; valid && index < pulse_count; index++) {
      if (pulse_duration(index) < AOK_SYNC_MIN_US)
        valid = false;
    }
    uint8_t previous = 1;
    for (size_t bit_index = 0; valid && bit_index < bit_count; bit_index++) {
      const size_t low_index = encoded_start + bit_index * 2U;
      const size_t high_index = low_index + 1U;
      const uint16_t low_duration = pulse_duration(low_index);
      const uint16_t high_duration = pulse_duration(high_index);
      if (pulse_high(low_index) || !pulse_high(high_index) || low_duration >= AOK_BIT_MAX_US ||
          high_duration >= AOK_BIT_MAX_US) {
        valid = false;
        break;
      }
      const uint8_t low_previous = low_duration < AOK_SHORT_MAX_US ? 0 : 1;
      if (low_previous != previous) {
        valid = false;
        break;
      }
      const uint8_t bit = high_duration < AOK_SHORT_MAX_US ? 1 : 0;
      // Trailer semantics per form: nominal [1, 0]; truncated single 0-read.
      if (bit_index == AOK_PAYLOAD_BITS &&
          bit != (trailer_bits == AOK_TRAILER_BITS ? 1 : 0)) {
        valid = false;
        break;
      }
      if (bit_index == AOK_PAYLOAD_BITS + 1U && bit != 0) {
        valid = false;
        break;
      }
      previous = bit;
    }
    if (valid)
      return true;
  }
  return false;
}

inline B1FrameStatus b1_frame_status(const std::vector<uint8_t> &raw) {
  if (raw.empty())
    return B1FrameStatus::INCOMPLETE;
  if (raw[0] != RF_CODE_START)
    return B1FrameStatus::INVALID;
  if (raw.size() == 1)
    return B1FrameStatus::INCOMPLETE;
  if (raw[1] != RF_CODE_RFIN_BUCKET)
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
  if (raw.back() == RF_CODE_STOP && is_aok_bucket_frame(raw)) {
    // B1 has no B0-style total-length byte. AOK's 65/66-pair envelope plus
    // bounded capture padding gives four derived candidate offsets. A 0x55
    // at any shorter offset can itself be a legitimate pulse or padding byte,
    // so only the maximum offset is unambiguous without an inter-byte quiet
    // boundary. The component defers shorter candidates until that boundary.
    return raw.size() == max_frame_size ? B1FrameStatus::COMPLETE : B1FrameStatus::CANDIDATE;
  }
  return raw.size() == max_frame_size ? B1FrameStatus::INVALID : B1FrameStatus::INCOMPLETE;
}

inline std::string compact_hex(const std::vector<uint8_t> &raw) {
  std::string output;
  output.reserve(raw.size() * 2U);
  for (const uint8_t byte : raw) {
    output.push_back(HEX_DIGITS[byte >> 4]);
    output.push_back(HEX_DIGITS[byte & 0x0F]);
  }
  return output;
}

// What the transmit path may do with a candidate outbound frame.
enum class B0FrameStatus : uint8_t {
  // Serialize the caller's characters verbatim. Either the frame is not an
  // outbound bucket frame at all (A5/A8 command strings, sniff arming) -- not
  // ours to judge -- or it is an AAB0 frame whose declared shape does not add
  // up, which cannot be compensated but is still lossless on the wire.
  PASSTHROUGH,
  // An AAB0 frame whose characters cannot survive serialization: write_byte_str_
  // coerces an unparseable nibble to 0 and drops a trailing odd nibble, so this
  // frame would reach the coprocessor as something the caller never WROTE.
  // Must not reach the UART at all.
  //
  // The line this draws is authorship, not safety: it stops the serializer
  // INVENTING nibbles, which is why `ZZZZ` (which would be invented as the 0
  // bucket, and its 659 ms stuck carrier) is refused. It is deliberately NOT a
  // ban on sub-100 us buckets. A literal `0000` is valid hex, self-consistent,
  // and exactly what its author wrote, so it classifies COMPENSABLE and is
  // handled downstream instead: the scheduler's normalize_b0 rejects a frame
  // whose DATA nibbles reference a bucket shorter than B0_MIN_BUCKET_US, and
  // send_raw floors any REFERENCED sub-floor bucket to B0_MIN_BUCKET_US at
  // every offset, including the default 0. Unreferenced table entries are
  // never rewritten and never reach the air, because a declared bucket need
  // not be referenced by any data nibble (zero-filled padding in an over-sized
  // table is legal). See HARDWARE.md caveat 2a.
  MALFORMED,
  // A self-consistent, fully-hex B0 bucket frame: lossless to serialize, and
  // eligible for bucket compensation.
  COMPENSABLE,
};

// Classify an outbound frame. Allocation-free, so the default (offset 0)
// transmit path can screen every frame without paying for a copy it will
// never rewrite.
inline B0FrameStatus b0_frame_status(const std::string &frame) {
  // The magic, and ONLY the magic, decides whether a frame is ours to judge --
  // length must not, or a truncated `AAB0Z` would skip every check below and
  // reach the UART as a fragment.
  //
  // It is matched on SERIALIZED nibbles, not characters: the question is not
  // "does this look like AAB0?" but "will the coprocessor receive AA B0?", and
  // only the bytes write_byte_str_ actually emits can answer that. This is why
  // lowercase `aab0...` is judged (hex is case-insensitive) and, less
  // obviously, why `AABZ...` is too -- the serializer coerces Z to 0, so that
  // frame IS a B0 frame once it reaches the wire.
  if (frame.size() < B0_MAGIC_CHARS || serialized_nibble(frame[0]) != 0xA ||
      serialized_nibble(frame[1]) != 0xA || serialized_nibble(frame[2]) != 0xB ||
      serialized_nibble(frame[3]) != 0x0)
    return B0FrameStatus::PASSTHROUGH;
  // From here the frame claims to be a B0, so its characters are held to what
  // write_byte_str_ can serialize without silently altering them: a frame too
  // short to carry the header it claims would serialize as a bare fragment, and
  // an odd length loses its last nibble.
  if (frame.size() < B0_BUCKET_TABLE_START || frame.size() % 2U != 0)
    return B0FrameStatus::MALFORMED;
  for (const char value : frame) {
    if (hex_nibble(value) < 0)
      return B0FrameStatus::MALFORMED;
  }
  const size_t body_length = static_cast<size_t>((hex_nibble(frame[4]) << 4) | hex_nibble(frame[5]));
  const size_t bucket_count = static_cast<size_t>((hex_nibble(frame[6]) << 4) | hex_nibble(frame[7]));
  // The declared bucket count and the declared body length must agree with each
  // other and with the frame, exactly as the scheduler's normalize_b0 requires
  // before admission. send_raw is a public ESPHome action, so a frame reaching
  // here need not have come from the normalizer: without this check an
  // over-declaring count would rewrite real data nibbles as bucket durations.
  const size_t body_end = 6U + body_length * 2U;
  if (frame.size() != 8U + body_length * 2U || B0_BUCKET_TABLE_START + bucket_count * 4U > body_end)
    return B0FrameStatus::PASSTHROUGH;
  return B0FrameStatus::COMPENSABLE;
}

// Subtract a fixed per-bucket microsecond offset from an outbound B0 frame.
//
// Sonoff R2 V2.2 boards run the vendored mightymos OB38S003 port, whose B0
// transmitter holds every bucket LONGER than commanded (upstream
// mightymos/RF-Bridge-OB38S003#27): the port dropped Portisch's startup-delay
// compensation, performs a 16-bit division after asserting the RF edge, and
// reloads Timer-1 one tick long. The error is additive rather than proportional
// to bucket length, but it is NOT the same on every edge: against a calibrated
// RTL-SDR it measured ~+90 us on pulses and ~+56 us on gaps.
//
// A B0 bucket index is referenced as BOTH a pulse and a gap within one frame,
// and each index carries a single 16-bit duration, so one value cannot hold two
// corrections. Subtracting a constant therefore nulls only the MEAN of the two
// errors and leaves roughly +/-17 us on every edge; no single host-side value
// can remove that residual. The knob narrows the timing error, it does not
// cancel it.
//
// This is applied at the UART boundary and NOWHERE else. The scheduler's
// airtime and RF-pacing math deliberately keeps using the UNcompensated
// durations: compensating at frame admission would shrink the computed airtime
// of a production AOK frame by ~96 ms at a 90 us offset -- against a 5 ms
// margin -- and reopen the UART-ring corruption fixed in field testing.
// Over-reserving air is safe; under-reserving is not.
//
// Returns `frame` unchanged when `offset_us` is 0 -- the shipped default -- and
// whenever b0_frame_status says the input is not COMPENSABLE. This rewrite is
// therefore a byte-for-byte no-op on a default build; note that send_raw still
// DROPS a MALFORMED frame at every offset, so "unchanged here" is not the same
// as "transmitted". Only the 4-hex-char bucket
// table is rewritten, in the same zero-padded uppercase hex the normalizer
// produces; the length byte, embedded repeat, data nibbles, and trailer are
// copied verbatim.
//
// An unchanged return is NOT a verdict that the frame is safe to transmit: a
// MALFORMED frame also comes back unchanged, and send_raw drops those before
// serialization rather than letting write_byte_str_ zero their bad nibbles.
// Callers screen with b0_frame_status; this function only rewrites.
//
// Every emitted bucket is floored at B0_MIN_BUCKET_US and can never reach 0 --
// see that constant for the 659 ms stuck-carrier hazard it exists to prevent,
// and for the two ways the floor engages. Either way the emitted bucket stops
// carrying the captured timing, so when `clamped_buckets` is non-null it
// receives how many buckets the floor caught and the caller can say so out loud.
inline std::string b0_with_bucket_offset(const std::string &frame, uint16_t offset_us,
                                         size_t *clamped_buckets = nullptr) {
  if (clamped_buckets != nullptr)
    *clamped_buckets = 0;
  if (offset_us == 0 || b0_frame_status(frame) != B0FrameStatus::COMPENSABLE)
    return frame;
  const size_t bucket_count = static_cast<size_t>((hex_nibble(frame[6]) << 4) | hex_nibble(frame[7]));
  std::string output = frame;
  size_t clamped = 0;
  for (size_t bucket = 0; bucket < bucket_count; bucket++) {
    const size_t start = B0_BUCKET_TABLE_START + bucket * 4U;
    uint32_t duration_us = 0;
    for (size_t index = 0; index < 4U; index++)
      duration_us = (duration_us << 4) | static_cast<uint32_t>(hex_nibble(frame[start + index]));
    const bool clear_of_floor = duration_us >= static_cast<uint32_t>(offset_us) + B0_MIN_BUCKET_US;
    const uint16_t emitted =
        clear_of_floor ? static_cast<uint16_t>(duration_us - offset_us) : B0_MIN_BUCKET_US;
    clamped += clear_of_floor ? 0U : 1U;
    for (size_t index = 0; index < 4U; index++)
      output[start + index] = HEX_DIGITS[(emitted >> (12U - index * 4U)) & 0x0F];
  }
  if (clamped_buckets != nullptr)
    *clamped_buckets = clamped;
  return output;
}

// Floor the REFERENCED sub-floor buckets of an outbound B0 frame. This is the
// default (offset 0) transmit path's counterpart to b0_with_bucket_offset:
// send_raw is a public action that bypasses the scheduler's normalize_b0 (the
// admission check that rejects such frames), so a frame whose data nibbles
// reference a bucket shorter than B0_MIN_BUCKET_US can arrive here
// unvalidated, and on both coprocessor variants such a bucket is a
// stuck-carrier hazard (see that constant).
//
// This is deliberately NOT "run b0_with_bucket_offset at offset 0": that pass
// rewrites the WHOLE bucket table through uppercase HEX_DIGITS and floors
// unreferenced entries too, which would break byte-identity for lowercase
// input and for legal zero-padded bucket tables. Here only the 4 hex chars of
// each referenced sub-floor bucket are rewritten (uppercase, to
// B0_MIN_BUCKET_US); the header, data nibbles, trailer, and every
// unreferenced bucket stay byte-verbatim.
//
// Returns false -- leaving `output` untouched, so the caller can emit the
// original string with zero allocation -- when the frame is not COMPENSABLE
// or no referenced bucket is below the floor. On true, `output` is the
// floored copy and `clamped_buckets`, when non-null, receives how many
// buckets were floored. A data nibble can in theory reference an undefined
// bucket here precisely because send_raw never ran normalize_b0; indices >=
// bucket_count are left alone (normalize_b0 rejects them separately).
inline bool b0_floor_referenced_buckets(const std::string &frame, std::string &output,
                                        size_t *clamped_buckets = nullptr) {
  if (clamped_buckets != nullptr)
    *clamped_buckets = 0;
  if (b0_frame_status(frame) != B0FrameStatus::COMPENSABLE)
    return false;
  const size_t body_length = static_cast<size_t>((hex_nibble(frame[4]) << 4) | hex_nibble(frame[5]));
  const size_t bucket_count = static_cast<size_t>((hex_nibble(frame[6]) << 4) | hex_nibble(frame[7]));
  const size_t body_end = 6U + body_length * 2U;
  // A bucket index is 3 bits, so the referenced sub-floor set is a bitmask.
  uint8_t below_floor = 0;
  for (size_t index = B0_BUCKET_TABLE_START + bucket_count * 4U; index < body_end; index++) {
    const size_t bucket = static_cast<size_t>(hex_nibble(frame[index]) & 0x07);
    if (bucket >= bucket_count)
      continue;
    const size_t start = B0_BUCKET_TABLE_START + bucket * 4U;
    uint32_t duration_us = 0;
    for (size_t nibble = 0; nibble < 4U; nibble++)
      duration_us = (duration_us << 4) | static_cast<uint32_t>(hex_nibble(frame[start + nibble]));
    if (duration_us < B0_MIN_BUCKET_US)
      below_floor = static_cast<uint8_t>(below_floor | (1U << bucket));
  }
  if (below_floor == 0)
    return false;
  output = frame;
  size_t clamped = 0;
  for (size_t bucket = 0; bucket < bucket_count; bucket++) {
    if ((below_floor & (1U << bucket)) == 0)
      continue;
    const size_t start = B0_BUCKET_TABLE_START + bucket * 4U;
    for (size_t index = 0; index < 4U; index++)
      output[start + index] = HEX_DIGITS[(B0_MIN_BUCKET_US >> (12U - index * 4U)) & 0x0F];
    clamped++;
  }
  if (clamped_buckets != nullptr)
    *clamped_buckets = clamped;
  return true;
}

}  // namespace esphome::rf_bridge
