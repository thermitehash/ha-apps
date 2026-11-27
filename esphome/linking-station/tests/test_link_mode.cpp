// Host unit tests for the linking station mode logic (Q8 in-stock session).
//   g++ -std=c++17 -Wall -Wextra -Werror -I../include test_link_mode.cpp && ./a.out
#include <cassert>
#include <cstdio>

#include "link_mode.h"

using namespace link_mode;

int main() {
  // Classification.
  assert(classify("PANTRY-HUB:IN-STOCK\r").kind == ScanKind::IN_STOCK_CARD);
  assert(classify(" pantry-hub:in-stock\r\n").kind == ScanKind::IN_STOCK_CARD);
  assert(classify("PANTRY-HUB:IN-STOCKX").kind == ScanKind::IGNORED);
  assert(classify("PANTRY-HUB").kind == ScanKind::IGNORED);
  Scan upc = classify("0 49000-02891 1\r");
  assert(upc.kind == ScanKind::PRODUCT && upc.code == "049000028911");
  assert(classify("123\r").kind == ScanKind::IGNORED);  // too short
  assert(classify("https://example.com/qr").kind == ScanKind::IGNORED);
  assert(classify("").kind == ScanKind::IGNORED);

  Scan card = classify(IN_STOCK_CARD);

  // Kroger mode: products go to Kroger; the card is ignored there.
  State s;
  assert(!s.link_mode() && !s.in_stock());
  assert(s.on_scan(upc) == Action::KROGER_SCAN);
  assert(s.on_scan(card) == Action::CARD_OUTSIDE_LINK);
  assert(!s.in_stock());
  Rgb off = s.idle_color();
  assert(!off.on);

  // Link mode: blue pulse, products are linked without in_stock.
  assert(s.toggle_link());
  assert(s.on_scan(upc) == Action::LINK_BARCODE && !s.in_stock());
  Rgb blue = s.idle_color();
  assert(blue.on && blue.b == 1.0f && blue.r == 0.0f);

  // Card toggles the session on: amber pulse, every link is in-stock.
  assert(s.on_scan(card) == Action::SESSION_ON && s.in_stock());
  Rgb amber = s.idle_color();
  assert(amber.on && amber.r == 1.0f && amber.g == 0.55f && amber.b == 0.0f);
  assert(s.on_scan(upc) == Action::LINK_BARCODE && s.in_stock());
  assert(s.on_scan(upc) == Action::LINK_BARCODE && s.in_stock());  // stays on

  // Card again ends it; still in link mode, back to blue.
  assert(s.on_scan(card) == Action::SESSION_OFF && !s.in_stock() && s.link_mode());
  assert(s.idle_color().b == 1.0f);

  // Session ends when link mode exits by double-click…
  s.on_scan(card);
  assert(s.in_stock());
  assert(!s.toggle_link());
  assert(!s.in_stock() && !s.link_mode());
  // … and does not come back when link mode is entered again.
  s.toggle_link();
  assert(s.link_mode() && !s.in_stock());

  // … or by the 60 s idle timeout (exit_link).
  s.on_scan(card);
  assert(s.in_stock());
  s.exit_link();
  assert(!s.in_stock() && !s.link_mode());
  assert(s.on_scan(upc) == Action::KROGER_SCAN);

  std::puts("link_mode tests passed");
  return 0;
}
