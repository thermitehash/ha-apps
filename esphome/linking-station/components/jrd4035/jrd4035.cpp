#include "jrd4035.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace jrd4035 {

static const char *const TAG = "jrd4035";
// The module wakes on any UART traffic; give it time before real commands.
static const uint32_t WAKE_DELAY_MS = 50;

void JRD4035Component::setup() {
  // Radio off until link mode asks for a scan.
  this->sleep_();
}

void JRD4035Component::dump_config() {
  ESP_LOGCONFIG(TAG, "JRD-4035 UHF RFID (M5Stack U107):");
  ESP_LOGCONFIG(TAG, "  TX power: %.1f dBm", this->tx_power_);
  ESP_LOGCONFIG(TAG, "  Region code: 0x%02X", this->region_);
  ESP_LOGCONFIG(TAG, "  Scan interval: %u ms, RSSI window: %u ms", (unsigned) this->scan_interval_,
                (unsigned) this->rssi_window_);
}

void JRD4035Component::send_(const std::vector<uint8_t> &bytes) { this->write_array(bytes.data(), bytes.size()); }

void JRD4035Component::sleep_() { this->send_(::jrd4035::sleep_command()); }

void JRD4035Component::start_scan(uint32_t timeout_ms) {
  uint32_t now = millis();
  // Any byte wakes the module; a single poll is harmless if it was awake.
  this->send_(::jrd4035::single_poll());
  this->wake_until_ = now + WAKE_DELAY_MS;
  this->strongest_.reset();
  this->scanning_ = true;
  this->scan_started_ = now;
  this->scan_timeout_ = timeout_ms;
  this->first_tag_at_ = 0;
  this->last_poll_ = 0;
  this->polls_ = 0;
  ESP_LOGD(TAG, "Scan started (%u ms, %.1f dBm)", (unsigned) timeout_ms, this->tx_power_);
}

void JRD4035Component::stop_scan() {
  if (this->scanning_) ESP_LOGD(TAG, "Scan stopped");
  this->scanning_ = false;
  this->sleep_();
}

void JRD4035Component::finish_(bool found) {
  this->scanning_ = false;
  this->sleep_();
  if (found) {
    const auto &best = this->strongest_.best();
    ESP_LOGI(TAG, "Strongest tag %s (%.0f dBm) after %u polls", best.epc.c_str(), best.rssi, (unsigned) this->polls_);
    for (auto &cb : this->tag_callbacks_) cb(best.epc, best.rssi);
  } else {
    ESP_LOGI(TAG, "No tag within %u ms", (unsigned) this->scan_timeout_);
    for (auto &cb : this->timeout_callbacks_) cb();
  }
}

void JRD4035Component::loop() {
  ::jrd4035::Frame frame;
  while (this->available()) {
    uint8_t byte;
    if (!this->read_byte(&byte)) break;
    if (!this->decoder_.feed(byte, frame)) continue;
    ::jrd4035::TagRead tag;
    uint8_t code = 0;
    if (::jrd4035::parse_tag(frame, tag)) {
      if (!this->scanning_) continue;
      if (this->first_tag_at_ == 0) this->first_tag_at_ = millis();
      this->strongest_.offer(tag);
      ESP_LOGV(TAG, "Tag %s %.0f dBm", tag.epc.c_str(), tag.rssi);
    } else if (::jrd4035::is_error(frame, &code)) {
      if (code != ::jrd4035::ERR_NO_TAG) {
        this->errors_++;
        ESP_LOGW(TAG, "Module error 0x%02X (%u so far)", code, (unsigned) this->errors_);
      }
    } else if (frame.type == ::jrd4035::TYPE_RESPONSE && frame.cmd == ::jrd4035::CMD_SET_POWER) {
      bool ok = !frame.params.empty() && frame.params[0] == 0x00;
      if (!ok) ESP_LOGW(TAG, "Module rejected TX power %.1f dBm; check its supported range", this->tx_power_);
    }
  }

  if (!this->scanning_) return;
  uint32_t now = millis();
  if (static_cast<int32_t>(now - this->wake_until_) < 0) return;  // wrap-safe
  if (this->polls_ == 0 && this->last_poll_ == 0) {
    // First command after waking: apply region and power every scan, since
    // the module may not keep them across sleep.
    this->send_(::jrd4035::set_region(static_cast<::jrd4035::Region>(this->region_)));
    this->send_(::jrd4035::set_power(this->tx_power_));
  }
  if (this->first_tag_at_ != 0 && now - this->first_tag_at_ >= this->rssi_window_) {
    this->finish_(true);
    return;
  }
  if (now - this->scan_started_ >= this->scan_timeout_) {
    this->finish_(this->strongest_.has());
    return;
  }
  if (now - this->last_poll_ >= this->scan_interval_) {
    this->last_poll_ = now;
    this->polls_++;
    this->send_(::jrd4035::single_poll());
  }
}

}  // namespace jrd4035
}  // namespace esphome
