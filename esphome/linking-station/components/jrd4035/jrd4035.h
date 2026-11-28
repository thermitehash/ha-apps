// ESPHome component for the M5Stack Unit UHF-RFID (U107, JRD-4035 module).
//
// Single-tag "linking" use: start_scan() wakes the module, applies region and
// TX power, then sends single-poll inventory commands every scan_interval.
// After the first tag, it keeps polling for rssi_window and reports the
// strongest tag once (on_tag), or reports on_timeout. Between scans the
// module is put to sleep, so its radio only transmits during a scan.
#pragma once

#include <functional>
#include <string>
#include <vector>

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/components/uart/uart.h"

#include "jrd4035_protocol.h"

namespace esphome {
namespace jrd4035 {

class JRD4035Component : public Component, public uart::UARTDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_tx_power(float dbm) {
    this->tx_power_ = dbm;
    this->configured_power_ = dbm;
  }
  void set_region(uint8_t region) { this->region_ = region; }
  void set_scan_interval(uint32_t ms) { this->scan_interval_ = ms; }
  void set_rssi_window(uint32_t ms) { this->rssi_window_ = ms; }

  void add_on_tag_callback(std::function<void(const std::string &, float)> &&cb) {
    this->tag_callbacks_.push_back(std::move(cb));
  }
  void add_on_timeout_callback(std::function<void()> &&cb) { this->timeout_callbacks_.push_back(std::move(cb)); }

  // Start a scan lasting up to timeout_ms; restarts a running scan.
  void start_scan(uint32_t timeout_ms);
  // Stop scanning (no callback) and put the module to sleep.
  void stop_scan();
  bool is_scanning() const { return this->scanning_; }
  // Change TX power at runtime (applied at the next scan).
  void set_tx_power_runtime(float dbm) { this->tx_power_ = dbm; }

 protected:
  void send_(const std::vector<uint8_t> &bytes);
  void finish_(bool found);
  void sleep_();

  float tx_power_{15.0f};
  float configured_power_{15.0f};  // as set in YAML; tx_power_ may be raised
  bool power_raised_{false};       // the module rejected a lower value
  uint8_t region_{0x02};
  uint32_t scan_interval_{100};
  uint32_t rssi_window_{1000};

  ::jrd4035_proto::Decoder decoder_;
  ::jrd4035_proto::StrongestTag strongest_;
  bool scanning_{false};
  uint32_t scan_started_{0};
  uint32_t scan_timeout_{0};
  uint32_t first_tag_at_{0};
  uint32_t last_poll_{0};
  uint32_t wake_until_{0};
  uint32_t polls_{0};
  uint32_t errors_{0};

  std::vector<std::function<void(const std::string &, float)>> tag_callbacks_;
  std::vector<std::function<void()>> timeout_callbacks_;
};

class TagTrigger : public Trigger<std::string, float> {
 public:
  explicit TagTrigger(JRD4035Component *parent) {
    parent->add_on_tag_callback([this](const std::string &epc, float rssi) { this->trigger(epc, rssi); });
  }
};

class TimeoutTrigger : public Trigger<> {
 public:
  explicit TimeoutTrigger(JRD4035Component *parent) {
    parent->add_on_timeout_callback([this]() { this->trigger(); });
  }
};

}  // namespace jrd4035
}  // namespace esphome
