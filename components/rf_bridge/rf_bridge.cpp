#include "rf_bridge.h"
#include "rf_bridge_protocol.h"
#include "esphome/core/application.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include <cinttypes>
#include <cstring>

namespace esphome::rf_bridge {

static const char *const TAG = "rf_bridge";

void RFBridgeComponent::ack_() {
  ESP_LOGV(TAG, "Sending ACK");
  this->write(RF_CODE_START);
  this->write(RF_CODE_ACK);
  this->write(RF_CODE_STOP);
  this->flush();
}

void RFBridgeComponent::finish_bucket_capture_(bool publish) {
  // ACK before the automation publishes MQTT so broker backpressure cannot
  // hold the EFM8BB1 in its completed-capture path.
  this->ack_();
  if (publish) {
    const std::string str = compact_hex(this->rx_buffer_);
    ESP_LOGI(TAG, "Received RFBridge Bucket: %s", str.c_str());
    this->bucket_data_callback_.call(str);
  } else {
    ESP_LOGD(TAG, "Rejected non-AOK RFBridge Bucket frame");
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

  if (byte == RF_CODE_STOP && action != RF_CODE_ACK)
    this->ack_();

  // return false to reset buffer
  return false;
}

void RFBridgeComponent::write_byte_str_(const std::string &codes) {
  uint8_t code;
  int size = codes.length();
  for (int i = 0; i < size; i += 2) {
    code = strtol(codes.substr(i, 2).c_str(), nullptr, 16);
    this->write(code);
  }
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

void RFBridgeComponent::send_raw(const std::string &raw_code) {
  ESP_LOGD(TAG, "Sending Raw Code: %s", raw_code.c_str());

  this->write_byte_str_(raw_code);
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
