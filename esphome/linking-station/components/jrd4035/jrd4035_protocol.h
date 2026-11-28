// JRD-4035 UHF RFID module protocol (M5Stack Unit UHF-RFID, U107).
//
// Pure C++17, no ESPHome/Arduino dependencies, so it can be unit-tested on
// the host (see tests/test_jrd4035_protocol.cpp).
//
// Frame (from M5Stack's published command set for the U107 / JRD-4035):
//
//   0xBB | type | cmd | PL_hi | PL_lo | params[PL] | checksum | 0x7E
//
//   type: 0x00 command, 0x01 response, 0x02 notice (tag data)
//   checksum: low byte of the sum of type, cmd, PL_hi, PL_lo and params
//
// Commands used:
//   0x22 single polling inventory   BB 00 22 00 00 22 7E
//        tag notice:  BB 02 22 00 PL RSSI PC(2) EPC(PL-5) CRC(2) CS 7E
//        no tag:      BB 01 FF 00 01 15 16 7E
//   0xB6 set TX power (centi-dBm)   BB 00 B6 00 02 07 D0 8F 7E  (20.00 dBm)
//   0x07 set region                 BB 00 07 00 01 02 0A 7E     (02 = US)
//   0x17 sleep                      BB 00 17 00 00 17 7E
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace jrd4035_proto {

constexpr uint8_t HEADER = 0xBB;
constexpr uint8_t END = 0x7E;
constexpr uint8_t TYPE_COMMAND = 0x00;
constexpr uint8_t TYPE_RESPONSE = 0x01;
constexpr uint8_t TYPE_NOTICE = 0x02;
constexpr uint8_t CMD_SINGLE_POLL = 0x22;
constexpr uint8_t CMD_SET_POWER = 0xB6;
constexpr uint8_t CMD_SET_REGION = 0x07;
constexpr uint8_t CMD_SLEEP = 0x17;
constexpr uint8_t CMD_ERROR = 0xFF;
constexpr uint8_t ERR_NO_TAG = 0x15;
constexpr size_t MAX_PARAMS = 64;

enum class Region : uint8_t { CHINA_900 = 0x01, US = 0x02, EUROPE = 0x03, CHINA_800 = 0x04, KOREA = 0x06 };

inline uint8_t checksum(const uint8_t *data, size_t len) {
  uint32_t sum = 0;
  for (size_t i = 0; i < len; i++) sum += data[i];
  return static_cast<uint8_t>(sum & 0xFF);
}

inline std::vector<uint8_t> build_command(uint8_t cmd, const std::vector<uint8_t> &params = {}) {
  std::vector<uint8_t> out;
  out.reserve(params.size() + 7);
  out.push_back(HEADER);
  out.push_back(TYPE_COMMAND);
  out.push_back(cmd);
  out.push_back(static_cast<uint8_t>((params.size() >> 8) & 0xFF));
  out.push_back(static_cast<uint8_t>(params.size() & 0xFF));
  out.insert(out.end(), params.begin(), params.end());
  out.push_back(checksum(out.data() + 1, out.size() - 1));
  out.push_back(END);
  return out;
}

inline std::vector<uint8_t> single_poll() { return build_command(CMD_SINGLE_POLL); }
inline std::vector<uint8_t> sleep_command() { return build_command(CMD_SLEEP); }
inline std::vector<uint8_t> set_region(Region region) {
  return build_command(CMD_SET_REGION, {static_cast<uint8_t>(region)});
}

// TX power in dBm, sent as centi-dBm (e.g. 15.0 → 1500 = 0x05DC).
inline std::vector<uint8_t> set_power(float dbm) {
  int centi = static_cast<int>(dbm * 100.0f + 0.5f);
  if (centi < 0) centi = 0;
  if (centi > 0xFFFF) centi = 0xFFFF;
  return build_command(CMD_SET_POWER, {static_cast<uint8_t>(centi >> 8), static_cast<uint8_t>(centi & 0xFF)});
}

// Highest TX power the module family accepts (M100 / JRD-4035: ~26 dBm).
constexpr float MAX_TX_POWER = 26.0f;

// After the module rejects `dbm`, the next power to try: 1 dBm higher, up
// to MAX_TX_POWER. Returns a value <= dbm when there is nothing left to try.
// Low power is wanted (only the item in hand is read), so the station steps
// up from the configured value to the lowest one the module accepts.
inline float next_power_after_reject(float dbm) {
  if (dbm >= MAX_TX_POWER) return dbm;
  float next = static_cast<float>(static_cast<int>(dbm + 1.0f));  // whole dBm
  return next > MAX_TX_POWER ? MAX_TX_POWER : next;
}

struct Frame {
  uint8_t type = 0;
  uint8_t cmd = 0;
  std::vector<uint8_t> params;
};

struct TagRead {
  std::string epc;  // upper-case hex
  float rssi = 0;   // dBm
  uint16_t pc = 0;
};

// Incremental frame decoder: feed bytes one at a time; returns true when a
// complete, checksum-valid frame is available in `frame`. Garbage and bad
// frames are skipped (counted in bad_frames) and never stall the decoder.
class Decoder {
 public:
  bool feed(uint8_t byte, Frame &frame) {
    switch (state_) {
      case State::HEADER:
        if (byte == HEADER) {
          buf_.clear();
          state_ = State::TYPE;
        }
        return false;
      case State::TYPE:
        if (byte != TYPE_COMMAND && byte != TYPE_RESPONSE && byte != TYPE_NOTICE) {
          reset_bad(byte);
          return false;
        }
        buf_.push_back(byte);
        state_ = State::CMD;
        return false;
      case State::CMD:
        buf_.push_back(byte);
        state_ = State::PL_HI;
        return false;
      case State::PL_HI:
        buf_.push_back(byte);
        state_ = State::PL_LO;
        return false;
      case State::PL_LO:
        buf_.push_back(byte);
        len_ = (static_cast<size_t>(buf_[2]) << 8) | byte;
        if (len_ > MAX_PARAMS) {
          reset_bad(byte);
          return false;
        }
        state_ = len_ ? State::PARAMS : State::CHECKSUM;
        return false;
      case State::PARAMS:
        buf_.push_back(byte);
        if (buf_.size() == 4 + len_) state_ = State::CHECKSUM;
        return false;
      case State::CHECKSUM:
        if (byte != checksum(buf_.data(), buf_.size())) {
          reset_bad(byte);
          return false;
        }
        state_ = State::END_BYTE;
        return false;
      case State::END_BYTE:
        state_ = State::HEADER;
        if (byte != END) {
          bad_frames++;
          return false;
        }
        frame.type = buf_[0];
        frame.cmd = buf_[1];
        frame.params.assign(buf_.begin() + 4, buf_.end());
        return true;
    }
    return false;
  }

  uint32_t bad_frames = 0;

 private:
  enum class State { HEADER, TYPE, CMD, PL_HI, PL_LO, PARAMS, CHECKSUM, END_BYTE };

  void reset_bad(uint8_t byte) {
    bad_frames++;
    state_ = (byte == HEADER) ? State::TYPE : State::HEADER;
    buf_.clear();
  }

  State state_ = State::HEADER;
  std::vector<uint8_t> buf_;
  size_t len_ = 0;
};

// Parse a single-poll tag notice. Returns false for anything else.
inline bool parse_tag(const Frame &frame, TagRead &tag) {
  if (frame.type != TYPE_NOTICE || frame.cmd != CMD_SINGLE_POLL) return false;
  const auto &p = frame.params;
  // RSSI(1) + PC(2) + EPC(>=2, even) + CRC(2)
  if (p.size() < 7 || ((p.size() - 5) % 2) != 0) return false;
  tag.rssi = static_cast<float>(static_cast<int8_t>(p[0]));
  tag.pc = static_cast<uint16_t>((p[1] << 8) | p[2]);
  static const char *HEX = "0123456789ABCDEF";
  tag.epc.clear();
  for (size_t i = 3; i < p.size() - 2; i++) {
    tag.epc.push_back(HEX[p[i] >> 4]);
    tag.epc.push_back(HEX[p[i] & 0x0F]);
  }
  return true;
}

inline bool is_error(const Frame &frame, uint8_t *code = nullptr) {
  if (frame.type != TYPE_RESPONSE || frame.cmd != CMD_ERROR) return false;
  if (code != nullptr) *code = frame.params.empty() ? 0 : frame.params[0];
  return true;
}

// Picks the strongest tag seen; ties keep the first one.
class StrongestTag {
 public:
  void reset() { has_ = false; }
  void offer(const TagRead &tag) {
    if (!has_ || tag.rssi > best_.rssi) {
      best_ = tag;
      has_ = true;
    }
  }
  bool has() const { return has_; }
  const TagRead &best() const { return best_; }

 private:
  bool has_ = false;
  TagRead best_;
};

}  // namespace jrd4035_proto
