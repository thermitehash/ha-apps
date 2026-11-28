// Phone camera barcode scanner (Unload / Stock-take fallback for the station).
//
// Uses the browser's BarcodeDetector where it exists (Chrome on Android,
// Safari 17+ on iOS for some formats); otherwise loads the bundled ZXing
// library (static/zxing.min.js, never a CDN). A typed barcode always works,
// also where the camera isn't allowed (it needs HTTPS or localhost).

const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"];
const REPEAT_MS = 2500; // the same code isn't sent twice within this

let zxingLoading = null;

function loadZxing(url) {
  if (window.ZXing) return Promise.resolve(window.ZXing);
  if (!zxingLoading) {
    zxingLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = url;
      s.onload = () => (window.ZXing ? resolve(window.ZXing) : reject(new Error("ZXing didn't load")));
      s.onerror = () => reject(new Error("Couldn't load the barcode reader"));
      document.head.append(s);
    });
  }
  return zxingLoading;
}

async function nativeDetector() {
  if (!("BarcodeDetector" in window)) return null;
  try {
    const supported = await window.BarcodeDetector.getSupportedFormats();
    const formats = FORMATS.filter((f) => supported.includes(f));
    if (!formats.length) return null;
    return new window.BarcodeDetector({ formats });
  } catch {
    return null;
  }
}

/**
 * Open the scanner overlay. `onCode(code)` is called for each barcode read
 * or typed (it returns a promise with {ok, message}, shown under the video).
 */
export function openScanner({ title, zxingUrl, onCode, onClose }) {
  const root = document.createElement("div");
  root.className = "scanner";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", title);
  root.innerHTML = `
<div class="scanner-box">
<div class="row"><h2 class="grow">${title}</h2><button class="btn" type="button" data-scan="close">Done</button></div>
<video class="scanner-video" playsinline muted></video>
<p class="sub scanner-status" role="status" aria-live="polite">Starting the camera…</p>
<form class="inline-form" data-scan="manual">
<label class="field"><span>Or type the barcode</span><input class="input" name="code" inputmode="numeric" autocomplete="off" enterkeyhint="send"></label>
<div class="actions"><button class="btn primary" type="submit">Add</button></div>
</form>
</div>`;
  document.body.append(root);
  const video = root.querySelector("video");
  const status = root.querySelector(".scanner-status");
  let stream = null;
  let stopped = false;
  let zxingControls = null;
  let last = { code: "", at: 0 };

  const say = (text, bad = false) => {
    status.textContent = text;
    status.classList.toggle("error-text", bad);
  };

  async function handle(code, typed = false) {
    const now = Date.now();
    // A camera sees the same barcode many times a second; typed codes count each time.
    if (!typed && code === last.code && now - last.at < REPEAT_MS) return;
    last = { code, at: now };
    if (navigator.vibrate) navigator.vibrate(60);
    say(`Read ${code}…`);
    try {
      const res = await onCode(code);
      say(res.message || (res.ok ? "Added" : "Didn't work"), !res.ok);
    } catch (err) {
      say(`Failed: ${err.message || err}`, true);
    }
  }

  function close() {
    stopped = true;
    if (zxingControls) zxingControls.reset();
    if (stream) for (const t of stream.getTracks()) t.stop();
    root.remove();
    if (onClose) onClose();
  }

  root.addEventListener("click", (e) => {
    if (e.target.closest("[data-scan='close']")) close();
  });
  root.querySelector("form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = e.target.elements.code;
    const code = input.value.replace(/\D/g, "");
    input.value = "";
    if (code) handle(code, true);
  });
  document.addEventListener(
    "keydown",
    function esc(e) {
      if (e.key === "Escape" && !stopped) {
        document.removeEventListener("keydown", esc);
        close();
      }
    },
  );

  (async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      say("The camera needs Home Assistant opened over HTTPS. Type the barcode below.", true);
      return;
    }
    const detector = await nativeDetector();
    if (detector) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      } catch (err) {
        say(`Camera not available (${err.name || err}). Type the barcode below.`, true);
        return;
      }
      if (stopped) return close();
      video.srcObject = stream;
      await video.play().catch(() => {});
      say("Point the camera at a barcode");
      const tick = async () => {
        if (stopped) return;
        try {
          const found = await detector.detect(video);
          if (found.length) await handle(found[0].rawValue);
        } catch {
          /* frame not ready */
        }
        setTimeout(tick, 250);
      };
      tick();
      return;
    }
    try {
      const ZXing = await loadZxing(zxingUrl);
      if (stopped) return;
      const reader = new ZXing.BrowserMultiFormatReader();
      zxingControls = reader;
      say("Point the camera at a barcode");
      await reader.decodeFromConstraints({ video: { facingMode: "environment" } }, video, (result) => {
        if (result && !stopped) handle(result.getText());
      });
    } catch (err) {
      say(`Camera not available (${err.name || err.message || err}). Type the barcode below.`, true);
    }
  })();

  return { close, handle };
}
