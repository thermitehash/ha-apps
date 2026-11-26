# Changelog

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
