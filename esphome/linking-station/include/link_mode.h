// Linking station mode logic: Kroger mode, link mode and the "already in
// stock" session started by the printed mode card.
//
// Pure C++17 with no ESPHome dependencies, so the behaviour is unit-tested on
// the host (tests/test_link_mode.cpp). kroger-scanner.yaml includes it
// (esphome: includes:) and its lambdas only call into this class.
//
//   Kroger mode ──double-click──▶ link mode ──scan IN-STOCK card──▶ link mode
//        ▲                         (blue pulse)  ◀──scan card again── + in-stock
//        └── double-click / 60 s idle ──────────────────────────────  (amber pulse)
//
//   Kroger mode ──triple-click──▶ Unload (slow green pulse)
//               ──hold 6 s─────▶ Stock-take (slow amber pulse)
//        ▲
//        └── single click / same gesture again / 3 min idle / hub "off"
//
// In Unload and Stock-take every product barcode goes to Pantry Hub
// (pantry_hub/linking/<station>/scan); nothing is added to the Kroger cart.
//
// The in-stock session ends whenever link mode ends, and every barcode linked
// while it is active is published with "in_stock": true.
//
// Counted session (Pantry Hub "Just bought" → Tag them): the hub publishes
// linking/<station>/start {"barcode", "count"}; the station enters link mode
// (same blue pulse) and scans one tag per tap until the hub's result says
// remaining = 0, or reports a timeout / error. Scanning a barcode, leaving
// link mode, or the 60 s idle timeout ends it.
#pragma once

#include <cstdint>
#include <string>

namespace link_mode {

// QR payload printed on docs/mode-card-in-stock.{png,pdf}.
constexpr const char *IN_STOCK_CARD = "PANTRY-HUB:IN-STOCK";

enum class ScanKind { PRODUCT, IN_STOCK_CARD, IGNORED };

struct Scan {
  ScanKind kind = ScanKind::IGNORED;
  std::string code;  // digits only, for PRODUCT
};

// What the firmware should do with a scan.
enum class Action {
  KROGER_SCAN,        // Kroger mode: kroger_restock.scan_upc
  LINK_BARCODE,       // link mode: publish barcode (+ in_stock if session)
  SESSION_ON,         // card scanned in link mode: session started
  SESSION_OFF,        // card scanned again: session ended
  CARD_OUTSIDE_LINK,  // card scanned in Kroger mode: ignored (amber blink)
  UNLOAD_BARCODE,     // Unload mode: publish to Pantry Hub, never the cart
  STOCKTAKE_BARCODE,  // Stock-take mode: publish to Pantry Hub, never the cart
  IGNORED,            // QR on packaging, too short, … (amber blink)
};

struct Rgb {
  float r, g, b;
  bool on;
  bool slow = false;  // slow pulse (Unload, Stock-take)
};

// Strip CR/LF/spaces at both ends.
inline std::string trim(const std::string &raw) {
  size_t a = 0, b = raw.size();
  while (a < b && (raw[a] == '\r' || raw[a] == '\n' || raw[a] == ' ')) a++;
  while (b > a && (raw[b - 1] == '\r' || raw[b - 1] == '\n' || raw[b - 1] == ' ')) b--;
  return raw.substr(a, b - a);
}

inline bool iequals(const std::string &a, const char *b) {
  size_t i = 0;
  for (; i < a.size() && b[i] != '\0'; i++) {
    char x = a[i], y = b[i];
    if (x >= 'a' && x <= 'z') x = static_cast<char>(x - 'a' + 'A');
    if (y >= 'a' && y <= 'z') y = static_cast<char>(y - 'a' + 'A');
    if (x != y) return false;
  }
  return i == a.size() && b[i] == '\0';
}

// Classify the bytes of one scan (as the M14 sent them).
inline Scan classify(const std::string &raw) {
  Scan scan;
  std::string text = trim(raw);
  if (iequals(text, IN_STOCK_CARD)) {
    scan.kind = ScanKind::IN_STOCK_CARD;
    return scan;
  }
  // Product codes: digits only (UPC/EAN/PLU); spaces and dashes are allowed
  // separators, anything else (QR codes on packaging) is ignored.
  for (char c : text) {
    if (c >= '0' && c <= '9') {
      scan.code += c;
    } else if (c != ' ' && c != '-') {
      scan.code.clear();
      return scan;  // IGNORED
    }
  }
  if (scan.code.size() >= 4) {
    scan.kind = ScanKind::PRODUCT;
  } else {
    scan.code.clear();
  }
  return scan;
}

// The station's modes. Kroger mode is the default; the others are entered
// by button gestures (or by Pantry Hub) and always return to Kroger mode.
enum class Mode { KROGER, LINK, UNLOAD, STOCKTAKE };

// Button gestures the firmware recognizes (ESPHome on_multi_click).
enum class Click { SINGLE, DOUBLE, TRIPLE, LONG };

// What a gesture did (the firmware runs the side effects).
enum class ClickAction {
  PRINT,            // single click in Kroger / link mode: print the list
  LINK_ON,          // double-click: link mode on
  LINK_OFF,         // double-click again: back to Kroger mode
  UNLOAD_ON,        // triple-click: Unload mode
  STOCKTAKE_ON,     // long-press: Stock-take mode
  EXIT_MODE,        // single click (or the same gesture again) in Unload /
                    // Stock-take: back to Kroger mode
};

class State {
 public:
  Mode mode() const { return mode_; }
  bool link_mode() const { return mode_ == Mode::LINK; }
  bool unload() const { return mode_ == Mode::UNLOAD; }
  bool stocktake() const { return mode_ == Mode::STOCKTAKE; }
  bool in_stock() const { return link_mode() && in_stock_; }

  // MQTT name of the mode (pantry_hub/linking/<station>/mode_state).
  const char *mode_name() const {
    switch (mode_) {
      case Mode::LINK: return "link";
      case Mode::UNLOAD: return "unload";
      case Mode::STOCKTAKE: return "stocktake";
      case Mode::KROGER:
      default: return "kroger";
    }
  }

  void enter_link() {
    mode_ = Mode::LINK;
    in_stock_ = false;
  }
  void enter_unload() { set_scan_mode(Mode::UNLOAD); }
  void enter_stocktake() { set_scan_mode(Mode::STOCKTAKE); }
  // Back to Kroger mode from anywhere (double-click, click, idle timeout).
  // Always ends the in-stock and counted sessions.
  void exit_link() {
    mode_ = Mode::KROGER;
    in_stock_ = false;
    counted_ = 0;
    resume_ = Mode::KROGER;
  }

  // Pantry Hub's mode command: "unload", "stocktake" or "off".
  // Returns whether the mode changed.
  bool set_mode_from_hub(const std::string &name) {
    Mode before = mode_;
    if (name == "unload") {
      enter_unload();
    } else if (name == "stocktake") {
      enter_stocktake();
    } else if (name == "off") {
      if (mode_ == Mode::UNLOAD || mode_ == Mode::STOCKTAKE) exit_link();
    }
    return mode_ != before;
  }

  // Counted session started by the hub. Enters link mode; count < 1 ignored.
  // From Unload / Stock-take it returns there when the session ends.
  // Returns whether a session is now running.
  bool start_counted(int count) {
    if (count < 1) return false;
    if (mode_ == Mode::UNLOAD || mode_ == Mode::STOCKTAKE) resume_ = mode_;
    if (!link_mode()) {
      bool keep = in_stock_;
      enter_link();
      in_stock_ = keep;
    }
    counted_ = count;
    return true;
  }
  bool counted() const { return link_mode() && counted_ > 0; }
  int counted_remaining() const { return counted() ? counted_ : 0; }

  // A hub result arrived. Returns true when the station should scan for the
  // next tag (counted session still running), false when it is over.
  bool on_result(const std::string &status, int remaining) {
    if (!counted()) return false;
    bool tap_ok = status == "linked" || status == "tag_already_linked";
    if (!tap_ok || remaining < 1) {
      counted_ = 0;
      if (resume_ != Mode::KROGER) {
        mode_ = resume_;  // back to Unload / Stock-take
        resume_ = Mode::KROGER;
      }
      return false;
    }
    counted_ = remaining;
    return true;
  }
  // Double-click. Returns the new link-mode state.
  bool toggle_link() {
    if (link_mode()) {
      exit_link();
    } else {
      enter_link();
    }
    return link_mode();
  }

  // A button gesture. Single click prints in Kroger / link mode and leaves
  // Unload / Stock-take; triple-click toggles Unload; long-press toggles
  // Stock-take; double-click toggles link mode (from any mode).
  ClickAction on_click(Click click) {
    switch (click) {
      case Click::SINGLE:
        if (mode_ == Mode::UNLOAD || mode_ == Mode::STOCKTAKE) {
          exit_link();
          return ClickAction::EXIT_MODE;
        }
        return ClickAction::PRINT;
      case Click::DOUBLE:
        return toggle_link() ? ClickAction::LINK_ON : ClickAction::LINK_OFF;
      case Click::TRIPLE:
        if (mode_ == Mode::UNLOAD) {
          exit_link();
          return ClickAction::EXIT_MODE;
        }
        enter_unload();
        return ClickAction::UNLOAD_ON;
      case Click::LONG:
      default:
        if (mode_ == Mode::STOCKTAKE) {
          exit_link();
          return ClickAction::EXIT_MODE;
        }
        enter_stocktake();
        return ClickAction::STOCKTAKE_ON;
    }
  }

  Action on_scan(const Scan &scan) {
    switch (scan.kind) {
      case ScanKind::IN_STOCK_CARD:
        if (!link_mode()) return Action::CARD_OUTSIDE_LINK;
        in_stock_ = !in_stock_;
        return in_stock_ ? Action::SESSION_ON : Action::SESSION_OFF;
      case ScanKind::PRODUCT:
        // Unload / Stock-take never reach the Kroger cart.
        if (mode_ == Mode::UNLOAD) return Action::UNLOAD_BARCODE;
        if (mode_ == Mode::STOCKTAKE) return Action::STOCKTAKE_BARCODE;
        if (!link_mode()) return Action::KROGER_SCAN;
        counted_ = 0;  // a barcode starts a normal link (hub replaces the session)
        resume_ = Mode::KROGER;
        return Action::LINK_BARCODE;
      case ScanKind::IGNORED:
      default:
        return Action::IGNORED;
    }
  }

  // Idle LED: off in Kroger mode; dim blue pulse in link mode (amber with an
  // in-stock session); slow green pulse in Unload; slow amber pulse in
  // Stock-take.
  Rgb idle_color() const {
    switch (mode_) {
      case Mode::LINK:
        if (in_stock_) return {1.0f, 0.55f, 0.0f, true};
        return {0.0f, 0.2f, 1.0f, true};
      case Mode::UNLOAD: return {0.0f, 1.0f, 0.2f, true, true};
      case Mode::STOCKTAKE: return {1.0f, 0.45f, 0.0f, true, true};
      case Mode::KROGER:
      default: return {0.0f, 0.0f, 0.0f, false};
    }
  }

  // Back to Kroger mode after this long without activity (0: never).
  uint32_t idle_timeout_ms() const {
    switch (mode_) {
      case Mode::LINK: return 60000;
      case Mode::UNLOAD:
      case Mode::STOCKTAKE: return 180000;
      case Mode::KROGER:
      default: return 0;
    }
  }

 private:
  void set_scan_mode(Mode m) {
    mode_ = m;
    in_stock_ = false;
    counted_ = 0;
    resume_ = Mode::KROGER;
  }

  Mode mode_ = Mode::KROGER;
  bool in_stock_ = false;
  int counted_ = 0;  // units still to tag in a counted session
  Mode resume_ = Mode::KROGER;  // mode to return to after a counted session
};

// The station's single mode state (one instance per firmware image).
// A function-local static avoids ESPHome globals, which don't support
// custom class types.
inline State &state() {
  static State instance;
  return instance;
}

}  // namespace link_mode
