#pragma once

#include <cstddef>
#include <cstdint>

// Vendored-mqtt inbound payload guard. The stock on_message fragment
// assembler reserves the broker-declared total before any integration-level
// validation can run; on the ESP8285 a hostile or errant tens-of-KB publish
// can exhaust the heap or stall past an armed fail-safe STOP deadline.
// Everything this bridge legitimately receives fits well under 4 KiB (the
// largest /tx carries three ~1 KiB B0 fields plus small keys).
namespace rf433 {

static constexpr size_t MAX_INBOUND_PAYLOAD = 4096;

inline bool accept_inbound_payload(size_t declared_total) {
  return declared_total <= MAX_INBOUND_PAYLOAD;
}

// Once-per-5 s throttle so a flood of oversized publishes cannot itself
// become log spam. Tracks the timestamp of the last log and compares via
// plain unsigned subtraction: elapsed = now_ms - last_log_ms wraps modulo
// 2^32 exactly like millis() itself, so a rollover between calls still
// yields the true (small) elapsed duration. This is deliberately not the
// signed-difference-against-a-deadline idiom used in rf433_scheduler.h;
// that idiom compares two absolute timestamps and only holds for gaps
// under ~24.8 days (2^31 ms), which a months-long uptime can exceed.
inline bool inbound_drop_log_due(uint32_t now_ms) {
  static bool logged_once = false;
  static uint32_t last_log_ms = 0;
  if (logged_once && (now_ms - last_log_ms) < 5000)
    return false;
  logged_once = true;
  last_log_ms = now_ms;
  return true;
}

}  // namespace rf433
