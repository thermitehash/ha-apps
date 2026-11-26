# Changelog

## 0.2.0

- **Already in stock** (existing stock): link a tag with "already in stock"
  (web UI checkbox, or `"in_stock": true` in the linking station's barcode or
  tag message) when Grocy already counts that unit. The add for its current
  arrival (or the next one, if the tag isn't in the pantry yet) is skipped;
  when it leaves, one unit is still consumed. Later arrivals behave normally.
  Off by default.
- **No double consume**: a consume that may have reached Grocy but has no
  stock entry id to check is no longer sent again. It's marked for review,
  counted in the Grocy queue sensor (`failed`, plus a new `review`
  attribute), listed in the web UI with **Consume now** / **Dismiss**, and
  included in the resync report.
- **Item left before its add reached Grocy**: before a queued (or retried)
  add is replayed, the hub checks that the item is still in the pantry. If
  that arrival has already departed, the add and its consume are both
  cancelled (net zero) and logged.
- Database schema 2 (adds two columns; a 0.1.0 database is migrated
  automatically).

## 0.1.0

First release.

- Chainway UR4 auto-mode UDP reads (via `aiochainway` 0.2.0) drive a per-tag
  presence state machine: unknown → arriving → present → missing → departed.
- Linked tags add one unit to Grocy when they arrive in the pantry and
  consume that same unit when they leave. Changes go through a journal that
  survives restarts, retries with backoff, and never adds an arrival twice.
- Linking over MQTT: scan a barcode on the linking station (the reflashed
  Kroger wall scanner in link mode), hold the tagged item near its RFID
  unit, done.
- Home Assistant entities over MQTT discovery: reader online, present tags,
  unlinked tags, Grocy queue, last link, resync differences, item event,
  resync button.
- Ingress web UI: status, links (unlink/relink), unlinked tags, barcodes that
  need a Grocy product, failed operations with retry.
