# Tagging items

## Which tags

UHF RFID (EPC Gen2) inlays, read by the UR4 in the pantry and the U107 at
the linking station.

| Tag | Use for | Notes |
|---|---|---|
| **DogBone**-style (~95 × 23 mm) | boxes, bags, big jars | Long range; the default choice when it fits. |
| **Belt**-style (~70 × 15 mm or similar) | bottles, narrow packaging | Wrap it around a curve, but don't fold it. |
| **Small inlays** (~40 × 20 mm or less) | spice jars, small packs | Shorter range; face them toward the antennas. |
| **Flag tags** | **cans** and other metal | The tag sticks out from the metal like a flag. Metal right behind a normal inlay detunes it and it won't read. |

## Where to put the tag

- **Not on metal.** Cans, foil pouches and foil-lined lids kill reads. Use
  a flag tag, or tag the paper label with the tag sticking off the edge.
- **Not on liquid if you can avoid it.** Water absorbs UHF. On bottles,
  put the tag on the cap or shoulder, or on the air gap at the top.
- **Flat and unfolded.** A creased antenna reads poorly.
- **Facing out**, toward where the pantry antennas are, when the item sits
  on the shelf.
- One tag per item. Two items with one tag between them read as one.

## Linking (step by step)

You need: the item, its tag stuck on, and the linking station (the Kroger
wall scanner).

1. **Double-click** the scanner's button. The LED starts a slow, dim **blue
   pulse**: link mode.
2. **Scan the item's barcode.** The station sends it to Pantry Hub, which
   looks it up in Grocy.
3. **Hold the tag close to the U107** (within a few cm; it runs at low
   power on purpose) for a second or two.
4. The LED shows the result:
   - **green**: linked. The tag now belongs to that Grocy product.
   - **amber**: Grocy doesn't know this barcode, and the lookup plugin
     didn't find it either. It's added to the **needs product** list in the
     Pantry Hub page: create the product in Grocy (add the barcode to it),
     then scan again.
   - **magenta**: this tag is already linked (to the product the hub
     names). To change it, use **Relink** in the Pantry Hub page.
   - **red**: no tag read within 15 s, or an error (Grocy down, MQTT down).
     Check the hub's log.
5. Link the next item, or leave link mode: **double-click** again, or just
   wait 60 s. A short **cyan flash** shows link mode is off. Kroger mode is
   back (barcodes go to the Kroger list again).

Then **put the item in the pantry**. Its arrival (3 reads within 10 s)
adds one unit to Grocy at the Pantry location. Linking itself never changes
stock, so nothing is counted twice.

> **Existing stock:** if Grocy already counts an item you're tagging for
> the first time, its arrival adds one more. Either tag items as they come
> home from shopping (before you add them in Grocy), or correct the amount
> once in Grocy after tagging what's already on the shelves. Resync shows
> where the counts differ.

## Taking things out

Take the item out of the pantry. After 15 minutes unseen
(`depart_after_s`), Pantry Hub consumes that exact unit in Grocy. Put it back
before then and nothing happens.

Used up the item? Throw the tag away with the packaging, or peel it off and
re-use it: re-linking is **Relink** in the web UI.

## Relinking and fixing mistakes

In the Pantry Hub page (sidebar):

- **Unlink**: forget the tag ↔ product link. Grocy stock isn't changed.
- **Link / Relink**: type a barcode, or a Grocy product id as `#12`.
- **Unlinked tags**: tags seen in the pantry that have no link. Link them
  from here if you know what they are, or forget them.
- **Resync** (button or `button.pantry_hub_resync`): compares, per product,
  the linked tags in the pantry with Grocy's amount at the Pantry location,
  and lists differences. It never changes Grocy; fix counts there.
