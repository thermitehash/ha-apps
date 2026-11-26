// Host unit tests for the JRD-4035 protocol helpers.
//   g++ -std=c++17 -Wall -Wextra -Werror -I../components/jrd4035 test_jrd4035_protocol.cpp && ./a.out
#include <cassert>
#include <cstdio>
#include <vector>

#include "jrd4035_protocol.h"

using namespace jrd4035;

static std::vector<Frame> decode_all(Decoder &d, const std::vector<uint8_t> &bytes) {
  std::vector<Frame> out;
  Frame f;
  for (auto b : bytes)
    if (d.feed(b, f)) out.push_back(f);
  return out;
}

int main() {
  // Commands match M5Stack's published examples byte for byte.
  assert((single_poll() == std::vector<uint8_t>{0xBB, 0x00, 0x22, 0x00, 0x00, 0x22, 0x7E}));
  assert((sleep_command() == std::vector<uint8_t>{0xBB, 0x00, 0x17, 0x00, 0x00, 0x17, 0x7E}));
  assert((set_power(20.0f) == std::vector<uint8_t>{0xBB, 0x00, 0xB6, 0x00, 0x02, 0x07, 0xD0, 0x8F, 0x7E}));
  assert((set_power(15.0f) == std::vector<uint8_t>{0xBB, 0x00, 0xB6, 0x00, 0x02, 0x05, 0xDC, 0x99, 0x7E}));
  assert((set_region(Region::US) == std::vector<uint8_t>{0xBB, 0x00, 0x07, 0x00, 0x01, 0x02, 0x0A, 0x7E}));

  // Tag notice from the command set: RSSI 0xC9 (-55 dBm), PC 3000, 12-byte EPC.
  std::vector<uint8_t> notice = {0xBB, 0x02, 0x22, 0x00, 0x11, 0xC9, 0x34, 0x00, 0x30, 0x75, 0x1F, 0xEB,
                                 0x70, 0x5C, 0x59, 0x04, 0xE3, 0xD5, 0x0D, 0x70, 0x3A, 0x76, 0xEF, 0x7E};
  // Fix the checksum for the example bytes (sum of type..params).
  notice[notice.size() - 2] = checksum(notice.data() + 1, notice.size() - 3);
  Decoder d;
  auto frames = decode_all(d, notice);
  assert(frames.size() == 1);
  TagRead tag;
  assert(parse_tag(frames[0], tag));
  assert(tag.rssi == -55.0f);
  assert(tag.pc == 0x3400);
  assert(tag.epc == "30751FEB705C5904E3D50D70");

  // No-tag error response.
  std::vector<uint8_t> err = {0xBB, 0x01, 0xFF, 0x00, 0x01, 0x15, 0x16, 0x7E};
  frames = decode_all(d, err);
  assert(frames.size() == 1);
  uint8_t code = 0;
  assert(is_error(frames[0], &code) && code == ERR_NO_TAG);
  assert(!parse_tag(frames[0], tag));

  // Garbage, a bad checksum and a missing end byte are skipped; the decoder
  // resyncs on the next frame.
  Decoder g;
  std::vector<uint8_t> noisy = {0x00, 0xFF, 0xBB, 0x09, 0xBB, 0x01, 0xFF, 0x00, 0x01, 0x15, 0x99, 0x7E,
                                0xBB, 0x01, 0xFF, 0x00, 0x01, 0x15, 0x16, 0x00};
  noisy.insert(noisy.end(), notice.begin(), notice.end());
  frames = decode_all(g, noisy);
  assert(frames.size() == 1);
  assert(parse_tag(frames[0], tag));
  assert(g.bad_frames == 3);

  // Oversized length is rejected without buffering it.
  Decoder big;
  frames = decode_all(big, {0xBB, 0x02, 0x22, 0xFF, 0xFF});
  assert(frames.empty() && big.bad_frames == 1);

  // Strongest tag wins; ties keep the first.
  StrongestTag s;
  assert(!s.has());
  s.offer({"AAAA", -60, 0});
  s.offer({"BBBB", -40, 0});
  s.offer({"CCCC", -40, 0});
  assert(s.has() && s.best().epc == "BBBB");
  s.reset();
  assert(!s.has());

  std::puts("jrd4035 protocol tests passed");
  return 0;
}
