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
  IGNORED,            // QR on packaging, too short, … (amber blink)
};

struct Rgb {
  float r, g, b;
  bool on;
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

class State {
 public:
  bool link_mode() const { return link_; }
  bool in_stock() const { return link_ && in_stock_; }

  void enter_link() {
    link_ = true;
    in_stock_ = false;
  }
  // Leaving link mode (double-click or idle timeout) always ends the session.
  void exit_link() {
    link_ = false;
    in_stock_ = false;
    counted_ = 0;
  }

  // Counted session started by the hub. Enters link mode; count < 1 ignored.
  // Returns whether a session is now running.
  bool start_counted(int count) {
    if (count < 1) return false;
    if (!link_) enter_link();
    counted_ = count;
    return true;
  }
  bool counted() const { return link_ && counted_ > 0; }
  int counted_remaining() const { return counted() ? counted_ : 0; }

  // A hub result arrived. Returns true when the station should scan for the
  // next tag (counted session still running), false when it is over.
  bool on_result(const std::string &status, int remaining) {
    if (!counted()) return false;
    bool tap_ok = status == "linked" || status == "tag_already_linked";
    if (!tap_ok || remaining < 1) {
      counted_ = 0;
      return false;
    }
    counted_ = remaining;
    return true;
  }
  // Double-click. Returns the new link-mode state.
  bool toggle_link() {
    if (link_) {
      exit_link();
    } else {
      enter_link();
    }
    return link_;
  }

  Action on_scan(const Scan &scan) {
    switch (scan.kind) {
      case ScanKind::IN_STOCK_CARD:
        if (!link_) return Action::CARD_OUTSIDE_LINK;
        in_stock_ = !in_stock_;
        return in_stock_ ? Action::SESSION_ON : Action::SESSION_OFF;
      case ScanKind::PRODUCT:
        if (!link_) return Action::KROGER_SCAN;
        counted_ = 0;  // a barcode starts a normal link (hub replaces the session)
        return Action::LINK_BARCODE;
      case ScanKind::IGNORED:
      default:
        return Action::IGNORED;
    }
  }

  // Idle LED: off in Kroger mode, dim blue pulse in link mode, dim amber
  // pulse in link mode with an in-stock session.
  Rgb idle_color() const {
    if (!link_) return {0.0f, 0.0f, 0.0f, false};
    if (in_stock_) return {1.0f, 0.55f, 0.0f, true};
    return {0.0f, 0.2f, 1.0f, true};
  }

 private:
  bool link_ = false;
  bool in_stock_ = false;
  int counted_ = 0;  // units still to tag in a counted session
};

// The station's single mode state (one instance per firmware image).
// A function-local static avoids ESPHome globals, which don't support
// custom class types.
inline State &state() {
  static State instance;
  return instance;
}

}  // namespace link_mode
