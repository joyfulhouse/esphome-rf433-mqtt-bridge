#include "rf_bridge.h"
#include "rf_bridge_protocol.h"
#include "esphome/core/application.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <cinttypes>
#include <cstring>

namespace esphome::rf_bridge {

static const char *const TAG = "rf_bridge";

// Quiet window for the floored-bucket warning after its first occurrence. The
// condition never changes within a build, so repeating it per repeat, per
// trailer, and per fail-safe STOP adds no information -- only UART time inside
// the dispatch loop and MQTT-republished log traffic.
static constexpr uint32_t CLAMP_LOG_INTERVAL_MS = 60000;

void RFBridgeComponent::finish_bucket_capture_(bool publish) {
  // Never ACK a delivery. Portisch's capture path is fire-and-forget (it
  // clears RF_DATA_STATUS and re-enables the receive interrupt immediately
  // after uart_put_RF_buckets), while a host ACK triggers its RF_CODE_ACK
  // handler: PCA0_DoSniffing(last_sniffing_command) — and the B1 command
  // handler leaves last_sniffing_command at RF_CODE_RFIN, so one ACKed
  // capture silently reverts the radio to standard sniffing and ends
  // listening (observed live on rf433-bridge-office, 2026-07-17).
  if (publish) {
    const std::string str = compact_hex(this->rx_buffer_);
    ESP_LOGD(TAG, "Received RFBridge Bucket: %s", str.c_str());
    this->bucket_data_callback_.call(str);
  } else {
    // Log the rejected capture so a real remote whose on-air shape trips the
    // AOK envelope can be diagnosed from the log alone (frames never reach
    // /rx from this path, so this is the only place the evidence exists).
    ESP_LOGD(TAG, "Rejected non-AOK RFBridge Bucket frame: %s", compact_hex(this->rx_buffer_).c_str());
  }
  this->bucket_candidate_ = false;
}

void RFBridgeComponent::reset_receive_state_() {
  this->rx_buffer_.clear();
  this->bucket_candidate_ = false;

  // Discard bytes already queued from a capture that was in flight before an
  // ESP-only restart or an explicit stop. Otherwise its tail could be parsed as
  // the beginning of a new frame after the software state has been reset.
  size_t remaining = this->available();
  while (remaining > 0) {
    uint8_t discarded[64];
    const size_t to_read = std::min(remaining, sizeof(discarded));
    if (!this->read_array(discarded, to_read))
      break;
    remaining -= to_read;
  }
  this->last_bridge_byte_ = App.get_loop_component_start_time();
}

void RFBridgeComponent::setup() {
  // The EFM8BB1 keeps running across an ESP-only restart. Establish the
  // documented receive-off boot boundary before MQTT can deliver commands.
  this->stop_advanced_sniffing();
}

bool RFBridgeComponent::parse_bridge_byte_(uint8_t byte) {
  if (this->bucket_candidate_ && byte == RF_CODE_START) {
    // A queued next frame proves the candidate ending really terminated
    // this capture: Portisch builds pulse entries from alternating signal
    // edges, so 0xAA (two high-level nibbles) cannot occur inside pulse
    // data. Publishing here splits back-to-back deliveries even when
    // loop() never observes a quiet gap between them (ported from the
    // upstream PR esphome/esphome#17683 review rounds).
    this->finish_bucket_capture_(true);
    this->rx_buffer_.clear();
  }
  size_t at = this->rx_buffer_.size();
  this->rx_buffer_.push_back(byte);
  const uint8_t *raw = &this->rx_buffer_[0];

  ESP_LOGVV(TAG, "Processing byte: 0x%02X", byte);

  // Byte 0: Start
  if (at == 0)
    return byte == RF_CODE_START;

  // Byte 1: Action
  if (at == 1)
    return byte >= RF_CODE_ACK && byte <= RF_CODE_RFIN_BUCKET;
  uint8_t action = raw[1];

  switch (action) {
    case RF_CODE_ACK:
      ESP_LOGD(TAG, "Action OK");
      break;
    case RF_CODE_LEARN_KO:
      ESP_LOGD(TAG, "Learning timeout");
      break;
    case RF_CODE_LEARN_OK:
    case RF_CODE_RFIN: {
      if (byte != RF_CODE_STOP || at < RF_MESSAGE_SIZE + 2)
        return true;

      RFBridgeData data;
      data.sync = (raw[2] << 8) | raw[3];
      data.low = (raw[4] << 8) | raw[5];
      data.high = (raw[6] << 8) | raw[7];
      data.code = (raw[8] << 16) | (raw[9] << 8) | raw[10];

      if (action == RF_CODE_LEARN_OK) {
        ESP_LOGD(TAG, "Learning success");
      }

      ESP_LOGI(TAG,
               "Received RFBridge Code: sync=0x%04" PRIX16 " low=0x%04" PRIX16 " high=0x%04" PRIX16
               " code=0x%06" PRIX32,
               data.sync, data.low, data.high, data.code);
      this->data_callback_.call(data);
      break;
    }
    case RF_CODE_LEARN_OK_NEW:
    case RF_CODE_ADVANCED_RFIN: {
      const size_t buffered_size = this->rx_buffer_.size();
      if (buffered_size < 3U)
        return true;
      const uint8_t length = this->rx_buffer_[2];
      const size_t stop_at = static_cast<size_t>(length) + 3U;
      if (at < stop_at)
        return true;
      if (at != stop_at || byte != RF_CODE_STOP)
        return false;
      if (length == 0 || buffered_size < 5U) {
        ESP_LOGW(TAG, "Rejected malformed RFBridge Advanced frame");
        break;
      }

      RFBridgeAdvancedData data{};

      data.length = length;
      data.protocol = this->rx_buffer_[3];
      char next_byte[3];  // 2 hex chars + null
      for (size_t index = 4U; index < buffered_size - 1U; index++) {
        buf_append_printf(next_byte, sizeof(next_byte), 0, "%02X", this->rx_buffer_[index]);
        data.code += next_byte;
      }

      ESP_LOGI(TAG, "Received RFBridge Advanced Code: length=0x%02X protocol=0x%02X code=0x%s", data.length,
               data.protocol, data.code.c_str());
      this->advanced_data_callback_.call(data);
      break;
    }
    case RF_CODE_RFIN_BUCKET: {
      const B1FrameStatus status = b1_frame_status(this->rx_buffer_);
      if (status == B1FrameStatus::INCOMPLETE) {
        this->bucket_candidate_ = false;
        return true;
      }
      if (status == B1FrameStatus::CANDIDATE) {
        // A shorter valid ending remains ambiguous until UART quiet: its 0x55
        // can be a legal pulse byte followed by a later, true B1 trailer.
        this->bucket_candidate_ = true;
        return true;
      }
      if (status == B1FrameStatus::INVALID) {
        this->finish_bucket_capture_(false);
        return false;
      }

      // COMPLETE is already the AOK-valid terminal state; b1_frame_status()
      // performed the single envelope check needed for this capture.
      this->finish_bucket_capture_(true);
      return false;
    }
    default:
      ESP_LOGW(TAG, "Unknown action: 0x%02X", action);
      break;
  }

  ESP_LOGVV(TAG, "Parsed: 0x%02X", byte);

  // Upstream ACKs every completed non-ACK frame here — a leftover from the
  // stock Itead firmware protocol. On Portisch no delivery waits for a host
  // ACK, and any ACK sent while bucket sniffing is armed reverts the radio
  // to standard mode via its stale last_sniffing_command (see
  // finish_bucket_capture_), so this fork never writes ACKs at all.

  // return false to reset buffer
  return false;
}

void RFBridgeComponent::write_byte_str_(const std::string &codes) {
  // Callers supply validated hex (the scheduler normalizes /tx frames; the
  // advanced-code action is config-authored). Convert nibbles in place -- the
  // previous substr+strtol form heap-allocated a temporary string per byte,
  // ~130 allocations for a production frame on every repeat of every dispatch.
  // serialized_nibble, not a local copy of its rule: b0_frame_status matches the
  // AAB0 magic through the same function, so the classifier's model of the wire
  // and the wire cannot drift apart. See its comment for the frame that got
  // through when they did.
  const size_t size = codes.length();
  for (size_t i = 0; i + 1 < size; i += 2)
    this->write(
        static_cast<uint8_t>((serialized_nibble(codes[i]) << 4) | serialized_nibble(codes[i + 1])));
}

void RFBridgeComponent::loop() {
  const uint32_t now = App.get_loop_component_start_time();
  size_t avail = this->available();
  // A maximum AOK B1 capture can span several UART reads. Preserve an
  // in-progress AOK-derived envelope across the stock 50 ms timeout;
  // malformed/stalled input is still bounded by MAX_RX_BUFFER_SIZE and 250 ms.
  // Any possible B1 trailer needs only several UART byte-times of true quiet,
  // and is never finalized while continuation bytes are already buffered.
  const bool receiving_bucket = this->rx_buffer_.size() >= 2 && this->rx_buffer_[1] == RF_CODE_RFIN_BUCKET;
  const bool bucket_transport_candidate =
      receiving_bucket && !this->rx_buffer_.empty() && this->rx_buffer_.back() == RF_CODE_STOP;
  const uint32_t rx_timeout_ms = bucket_transport_candidate ? B1_CANDIDATE_QUIET_MS : receiving_bucket ? 250 : 50;
  if (avail == 0 && now - this->last_bridge_byte_ > rx_timeout_ms) {
    if (receiving_bucket)
      this->finish_bucket_capture_(this->bucket_candidate_);
    this->rx_buffer_.clear();
    this->bucket_candidate_ = false;
    this->last_bridge_byte_ = now;
  }

  while (avail > 0) {
    uint8_t buf[64];
    size_t to_read = std::min(avail, sizeof(buf));
    if (!this->read_array(buf, to_read)) {
      break;
    }
    avail -= to_read;
    for (size_t i = 0; i < to_read; i++) {
      if (this->rx_buffer_.size() > MAX_RX_BUFFER_SIZE) {
        if (this->rx_buffer_.size() >= 2 && this->rx_buffer_[1] == RF_CODE_RFIN_BUCKET)
          this->finish_bucket_capture_(false);
        this->rx_buffer_.clear();
        this->bucket_candidate_ = false;
      }
      if (this->parse_bridge_byte_(buf[i])) {
        ESP_LOGVV(TAG, "Parsed: 0x%02X", buf[i]);
        this->last_bridge_byte_ = now;
      } else {
        this->rx_buffer_.clear();
        this->bucket_candidate_ = false;
      }
    }
  }
}

void RFBridgeComponent::send_code(RFBridgeData data) {
  ESP_LOGD(TAG, "Sending code: sync=0x%04" PRIX16 " low=0x%04" PRIX16 " high=0x%04" PRIX16 " code=0x%06" PRIX32,
           data.sync, data.low, data.high, data.code);
  this->write(RF_CODE_START);
  this->write(RF_CODE_RFOUT);
  this->write((data.sync >> 8) & 0xFF);
  this->write(data.sync & 0xFF);
  this->write((data.low >> 8) & 0xFF);
  this->write(data.low & 0xFF);
  this->write((data.high >> 8) & 0xFF);
  this->write(data.high & 0xFF);
  this->write((data.code >> 16) & 0xFF);
  this->write((data.code >> 8) & 0xFF);
  this->write(data.code & 0xFF);
  this->write(RF_CODE_STOP);
  this->flush();
}

void RFBridgeComponent::send_advanced_code(const RFBridgeAdvancedData &data) {
  ESP_LOGD(TAG, "Sending advanced code: length=0x%02X protocol=0x%02X code=0x%s", data.length, data.protocol,
           data.code.c_str());
  this->write(RF_CODE_START);
  this->write(RF_CODE_RFOUT_NEW);
  this->write(data.length & 0xFF);
  this->write(data.protocol & 0xFF);
  this->write_byte_str_(data.code);
  this->write(RF_CODE_STOP);
  this->flush();
}

void RFBridgeComponent::learn() {
  ESP_LOGD(TAG, "Learning mode");
  this->write(RF_CODE_START);
  this->write(RF_CODE_LEARN);
  this->write(RF_CODE_STOP);
  this->flush();
}

void RFBridgeComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "RF_Bridge:");
  // Printed unconditionally, including the 0 default. Double-compensation --
  // hand-tuned codes plus a non-zero offset -- is silent on air and silent in
  // MQTT, so the boot log has to be somewhere the effective value can be read
  // off a running bridge without recovering the YAML that built it.
  ESP_LOGCONFIG(TAG, "  TX bucket offset: %u us", static_cast<unsigned>(this->tx_bucket_offset_us_));
  this->check_uart_settings(19200);
}

void RFBridgeComponent::start_advanced_sniffing() {
  ESP_LOGI(TAG, "Advanced Sniffing on");
  this->write(RF_CODE_START);
  this->write(RF_CODE_SNIFFING_ON);
  this->write(RF_CODE_STOP);
  this->flush();
}

void RFBridgeComponent::stop_advanced_sniffing() {
  ESP_LOGI(TAG, "Advanced Sniffing off");
  this->write(RF_CODE_START);
  this->write(RF_CODE_SNIFFING_OFF);
  this->write(RF_CODE_STOP);
  this->flush();
  this->reset_receive_state_();
}

void RFBridgeComponent::start_bucket_sniffing() {
  ESP_LOGI(TAG, "Raw Bucket Sniffing on");
  this->write(RF_CODE_START);
  this->write(RF_CODE_RFIN_BUCKET);
  this->write(RF_CODE_STOP);
  this->flush();
}

bool RFBridgeComponent::clamp_log_due_(uint32_t now_ms) {
  // Plain unsigned subtraction, matching rf433_inbound_guard.h: elapsed wraps
  // modulo 2^32 exactly like the clock it came from, so a rollover between
  // calls still yields the true (small) elapsed duration.
  if (this->clamp_logged_ && (now_ms - this->last_clamp_log_ms_) < CLAMP_LOG_INTERVAL_MS)
    return false;
  this->clamp_logged_ = true;
  this->last_clamp_log_ms_ = now_ms;
  return true;
}

void RFBridgeComponent::send_raw(const std::string &raw_code) {
  // The only path carrying host-supplied BUCKET timings: scheduler dispatch,
  // the OTA wait-for-idle pump, and the fail-safe STOP drain all reach the UART
  // through here, so OB38S003 bucket compensation is applied once, at the last
  // moment before serialization.
  //
  // The two other registered transmit actions write their own frames and do not
  // pass through here. send_advanced_code (0xA8) carries a protocol ID, so the
  // coprocessor generates its edges from its own protocol table and there is
  // nothing host-supplied to compensate. send_code (0xA5) is NOT in that
  // position: its sync/low/high fields are host-supplied timings straight from
  // YAML, and whether they suffer issue #27 the way bucket timings do has not
  // been measured. This knob does not touch them either way.
  //
  // Trim surrounding whitespace before anything looks at the frame. Reading a
  // frame out of a text sensor or a template hands the lambda a trailing
  // newline for free, and write_byte_str_'s pair-at-a-time loop used to ignore
  // an odd trailing character, so such a frame transmitted correctly until this
  // component started enforcing parity. Everything below -- classification AND
  // both serialization paths -- uses `frame`, so the trailing bytes cannot
  // reach the UART either. Interior whitespace is deliberately NOT stripped: it
  // fails the hex check and is refused, because `AAB0 05` gives no honest
  // reading of where the caller's nibbles begin.
  size_t begin = raw_code.find_first_not_of(B0_TRIM_CHARS);
  size_t end = 0;
  if (begin == std::string::npos)
    begin = 0;
  else
    end = raw_code.find_last_not_of(B0_TRIM_CHARS) + 1U;
  // Bind rather than copy when there is nothing to trim: send_raw runs once per
  // repeat of every dispatch, and the default path must not start allocating.
  // The offset-0 bucket floor below holds the same line -- its copy of the
  // frame exists only when a referenced bucket was actually floored.
  const bool trimmed = begin != 0U || end != raw_code.size();
  const std::string trimmed_frame = trimmed ? raw_code.substr(begin, end - begin) : std::string();
  const std::string &frame = trimmed ? trimmed_frame : raw_code;

  const B0FrameStatus status = b0_frame_status(frame);
  if (status == B0FrameStatus::MALFORMED) {
    // Not merely uncompensatable -- unserializable. write_byte_str_ coerces an
    // unparseable nibble to 0 and drops a trailing odd one, so transmitting
    // this frame would put on air a code its author never wrote.
    //
    // The line drawn here is AUTHORSHIP, not safety: the serializer must not
    // invent nibbles. It is deliberately not a ban on short buckets -- a
    // literal `0000` is valid hex and exactly what its author wrote, so it is
    // accepted here and then floored to B0_MIN_BUCKET_US below (at every
    // offset, including the default 0) when a data nibble REFERENCES it. See
    // B0FrameStatus and HARDWARE.md caveat 2a for why an unreferenced zero
    // bucket is nobody's business.
    ESP_LOGW(TAG, "Refusing malformed B0 frame (non-hex, odd length, or truncated), nothing sent: %s",
             frame.c_str());
    return;
  }
  // Frames this pass cannot judge, and every frame at the default offset of 0,
  // reach the UART through this one emit. The floored copy is materialized ONLY
  // when a referenced bucket is actually below the floor
  // (b0_floor_referenced_buckets returning false leaves `floored` untouched):
  // send_raw runs once per repeat of every dispatch, and the default path must
  // not start allocating for the overwhelmingly common frame whose buckets are
  // all legal. Every such frame stays byte-identical to what the caller wrote,
  // lowercase and zero-padded tables included.
  if (status != B0FrameStatus::COMPENSABLE || this->tx_bucket_offset_us_ == 0) {
    std::string floored;
    size_t floored_buckets = 0;
    const std::string *wire = &frame;
    if (status == B0FrameStatus::COMPENSABLE &&
        b0_floor_referenced_buckets(frame, floored, &floored_buckets)) {
      // Same "never silent" rule as the offset>0 floor below, with a distinct
      // message: "lower the offset" would be wrong advice at offset 0.
      if (this->clamp_log_due_(App.get_loop_component_start_time())) {
        ESP_LOGW(TAG,
                 "send_raw floored %u referenced bucket(s) shorter than %u us; this frame no longer "
                 "encodes its written timing",
                 static_cast<unsigned>(floored_buckets), static_cast<unsigned>(B0_MIN_BUCKET_US));
      }
      wire = &floored;
    }
    ESP_LOGD(TAG, "Sending Raw Code: %s", wire->c_str());
    this->write_byte_str_(*wire);
    this->flush();
    return;
  }

  size_t clamped_buckets = 0;
  const std::string compensated =
      b0_with_bucket_offset(frame, this->tx_bucket_offset_us_, &clamped_buckets);
  // Log what reaches the coprocessor, not what the caller handed us: the bucket
  // table differs, and a lowercase input comes back mixed-case.
  ESP_LOGD(TAG, "Sending Raw Code: %s", compensated.c_str());
  // The floor keeps the coprocessor off a 659 ms stuck carrier, but a floored
  // bucket no longer carries the captured duration: the frame goes out encoding
  // something the receiver was never taught. Silent is the one thing that must
  // not happen. Throttled because the condition is deterministic -- it fires on
  // every repeat of every dispatch or on none of them -- and this loop paces
  // against a 5 ms RF margin with warnings republished over MQTT.
  if (clamped_buckets != 0 && this->clamp_log_due_(App.get_loop_component_start_time())) {
    ESP_LOGW(TAG,
             "tx_bucket_offset_us=%u floored %u bucket(s) at %u us; this frame no longer encodes "
             "its captured timing -- lower the offset",
             static_cast<unsigned>(this->tx_bucket_offset_us_),
             static_cast<unsigned>(clamped_buckets), static_cast<unsigned>(B0_MIN_BUCKET_US));
  }
  this->write_byte_str_(compensated);
  this->flush();
}

void RFBridgeComponent::beep(uint16_t ms) {
  ESP_LOGD(TAG, "Beeping for %hu ms", ms);

  this->write(RF_CODE_START);
  this->write(RF_CODE_BEEP);
  this->write((ms >> 8) & 0xFF);
  this->write(ms & 0xFF);
  this->write(RF_CODE_STOP);
  this->flush();
}

}  // namespace esphome::rf_bridge
