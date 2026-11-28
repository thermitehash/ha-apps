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

  // Counted session (hub "start" message): enters link mode, blue pulse.
  State c;
  assert(!c.start_counted(0) && !c.link_mode());
  assert(c.start_counted(3) && c.link_mode() && c.counted());
  assert(c.counted_remaining() == 3);
  assert(c.idle_color().b == 1.0f);
  // One tap per result: keep scanning while the hub says remaining > 0.
  assert(c.on_result("linked", 2) && c.counted_remaining() == 2);
  assert(c.on_result("tag_already_linked", 2));  // skipped tag: keep going
  assert(c.on_result("linked", 1));
  assert(!c.on_result("linked", 0) && !c.counted());  // count reached
  assert(c.link_mode());                              // still in link mode
  // Timeout / error end it.
  c.start_counted(2);
  assert(!c.on_result("timeout", 2) && !c.counted());
  c.start_counted(2);
  assert(!c.on_result("error", 2) && !c.counted());
  // A normal result outside a counted session never restarts scanning.
  assert(!c.on_result("linked", 5));
  // A barcode scan ends it (the hub starts a normal link instead).
  c.start_counted(2);
  assert(c.on_scan(upc) == Action::LINK_BARCODE && !c.counted());
  // Leaving link mode ends it.
  c.start_counted(2);
  c.exit_link();
  assert(!c.counted() && c.counted_remaining() == 0);
  // In-stock session survives a counted start while in link mode.
  State d;
  d.enter_link();
  d.on_scan(card);
  d.start_counted(1);
  assert(d.in_stock() && d.counted());

  // Unload (triple-click): slow green pulse; scans never reach the cart.
  State u;
  assert(u.on_click(Click::TRIPLE) == ClickAction::UNLOAD_ON);
  assert(u.unload() && !u.link_mode() && std::string(u.mode_name()) == "unload");
  Rgb green = u.idle_color();
  assert(green.on && green.slow && green.g == 1.0f && green.r == 0.0f);
  assert(u.on_scan(upc) == Action::UNLOAD_BARCODE);
  assert(u.on_scan(upc) != Action::KROGER_SCAN);
  assert(u.on_scan(card) == Action::CARD_OUTSIDE_LINK && !u.in_stock());
  assert(u.idle_timeout_ms() == 180000);
  // A single click leaves it (instead of printing).
  assert(u.on_click(Click::SINGLE) == ClickAction::EXIT_MODE);
  assert(u.mode() == Mode::KROGER && u.on_scan(upc) == Action::KROGER_SCAN);
  assert(u.on_click(Click::SINGLE) == ClickAction::PRINT);  // Kroger: prints
  // Triple-click again also leaves.
  u.on_click(Click::TRIPLE);
  assert(u.on_click(Click::TRIPLE) == ClickAction::EXIT_MODE && !u.unload());
  // Idle timeout: exit_link() back to Kroger.
  u.on_click(Click::TRIPLE);
  u.exit_link();
  assert(u.mode() == Mode::KROGER && u.idle_timeout_ms() == 0);

  // Stock-take (long-press): slow amber pulse, distinct from the in-stock
  // session's amber (which is a normal-speed pulse in link mode).
  State t;
  assert(t.on_click(Click::LONG) == ClickAction::STOCKTAKE_ON && t.stocktake());
  Rgb slow_amber = t.idle_color();
  assert(slow_amber.on && slow_amber.slow && slow_amber.r == 1.0f);
  assert(t.on_scan(upc) == Action::STOCKTAKE_BARCODE);
  assert(std::string(t.mode_name()) == "stocktake");
  assert(t.on_click(Click::LONG) == ClickAction::EXIT_MODE && !t.stocktake());
  State session;
  session.enter_link();
  session.on_scan(card);
  assert(session.in_stock() && !session.idle_color().slow);

  // Double-click from Unload goes to link mode; again → Kroger.
  State d2;
  d2.on_click(Click::TRIPLE);
  assert(d2.on_click(Click::DOUBLE) == ClickAction::LINK_ON && d2.link_mode());
  assert(d2.on_click(Click::DOUBLE) == ClickAction::LINK_OFF);
  assert(d2.mode() == Mode::KROGER && std::string(d2.mode_name()) == "kroger");

  // Pantry Hub's mode command.
  State h;
  assert(h.set_mode_from_hub("unload") && h.unload());
  assert(!h.set_mode_from_hub("unload"));  // already there
  assert(h.set_mode_from_hub("stocktake") && h.stocktake());
  assert(h.set_mode_from_hub("off") && h.mode() == Mode::KROGER);
  h.enter_link();
  assert(!h.set_mode_from_hub("off") && h.link_mode());  // "off" spares link mode
  assert(!h.set_mode_from_hub("party"));

  // Tag N during a stock-take: counted session, then back to Stock-take.
  State tn;
  tn.on_click(Click::LONG);
  assert(tn.start_counted(2) && tn.link_mode() && tn.counted());
  assert(tn.on_scan(upc) == Action::LINK_BARCODE);  // a barcode cancels resume
  assert(!tn.counted());
  tn.exit_link();
  tn.on_click(Click::LONG);
  tn.start_counted(1);
  assert(!tn.on_result("linked", 0));
  assert(tn.stocktake());  // resumed
  tn.on_click(Click::TRIPLE);
  tn.start_counted(1);
  assert(!tn.on_result("timeout", 1) && tn.unload());

  // The firmware's single shared instance.
  assert(&state() == &state());
  state().enter_link();
  assert(state().link_mode());
  state().exit_link();

  std::puts("link_mode tests passed");
  return 0;
}
