"""
Turnstile solver pakai Patchright — v3.0 (checkbox click).

Logika klik (berdasarkan riset dari Theyka/Turnstile-Solver, Scrapling,
zendriver_cloudflare, docs.rs, MaestroOfAutomation, Kameleo):

1. Checkbox Turnstile ada di dalam iframe challenges.cloudflare.com
   yang di-wrap dalam closed shadow root → TIDAK bisa diakses via
   document.querySelector dari halaman induk.
2. Patchright bisa akses iframe itu via Frame API (page.frames),
   meski shadow DOM-nya closed, karena Frame API bypass DOM traversal.
3. Posisi checkbox di dalam iframe relatif tetap:
   - ~10-15% dari kiri (checkbox kecil di pojok kiri)
   - ~50% dari atas (tengah vertikal)
   Untuk widget standar 300x65, kira-kira di (28, 32).
4. Klik pakai page.mouse.move(x, y, steps=N) + down + up
   (Real CDP Input.dispatchMouseEvent), bukan element.click().
5. Kalau setelah ~8 detik tidak ada token → nudge (geser 3-8px)
   dan klik ulang. Max 3-4 attempt.
"""
import asyncio
import logging
import random
from urllib.parse import urlparse
from patchright.async_api import async_playwright

log = logging.getLogger("solver")

# ── Browser state ──
_pw = None
_browser = None
_browser_lock = asyncio.Lock()

# ── HTML sintetis untuk render widget ──
TURNSTILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Loading</title></head>
<body>
    <div class="cf-turnstile"></div>
    <script>
        window.__ts = { token: null, error: null, rendered: false };
        function onTurnstileLoad() {
            try {
                turnstile.render('.cf-turnstile', {
                    sitekey: '__SITEKEY__',
                    __ACTION_LINE__
                    __CDATA_LINE__
                    callback: function (token) {
                        window.__ts.token = String(token);
                        console.log('TS_TOKEN_LEN_' + token.length);
                    },
                    'error-callback': function (err) {
                        window.__ts.error = String(err);
                        console.log('TS_ERROR_' + err);
                    },
                    'expired-callback': function () { window.__ts.error = 'expired'; },
                    'timeout-callback': function () { window.__ts.error = 'timeout'; }
                });
                window.__ts.rendered = true;
            } catch (e) {
                window.__ts.error = 'render_exception: ' + e.message;
            }
        }
        setTimeout(function () {
            if (typeof turnstile === 'undefined') {
                window.__ts.error = 'script_not_loaded';
            }
        }, 5000);
    </script>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoad" defer></script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
#  BROWSER MANAGEMENT
# ═══════════════════════════════════════════════════════════════
async def _get_browser():
    global _pw, _browser
    async with _browser_lock:
        alive = False
        if _browser is not None:
            try:
                alive = _browser.is_connected()
            except Exception:
                alive = False

        if not alive:
            if _pw is None:
                _pw = await async_playwright().start()
            log.info("Launching chromium...")
            _browser = await _pw.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            log.info("Chromium launched")
    return _browser


def _parse_proxy(proxy_str):
    if not proxy_str:
        return None
    if isinstance(proxy_str, dict):
        return proxy_str
    u = urlparse(proxy_str)
    if not u.hostname:
        return None
    server = f"{u.scheme or 'http'}://{u.hostname}"
    if u.port:
        server += f":{u.port}"
    out = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


# ═══════════════════════════════════════════════════════════════
#  DETEKSI IFRAME TURNSTILE
# ═══════════════════════════════════════════════════════════════
async def _wait_turnstile_frame(page, timeout=25):
    """
    Tunggu iframe challenges.cloudflare.com muncul.
    Pakai Frame API — bisa tembus closed shadow DOM.
    Return: Frame object atau None.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in page.frames:
            try:
                if frame.url.startswith("https://challenges.cloudflare.com"):
                    return frame
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return None


# ═══════════════════════════════════════════════════════════════
#  KLIK CHECKBOX
# ═══════════════════════════════════════════════════════════════
async def _click_checkbox(page, frame, x_ratio=0.11, y_ratio=0.50, nudge=(0, 0)):
    """
    Klik checkbox di dalam iframe Turnstile pakai page.mouse.

    Koordinat:
      x = frame.x + frame.width  * x_ratio + nudge_x
      y = frame.y + frame.height * y_ratio + nudge_y

    x_ratio 0.11 = ~28px untuk widget 300px lebar
    y_ratio 0.50 = ~32px untuk widget 65px tinggi

    Klik dilakukan dalam 3 fase:
      1. mouse.move(x, y, steps=10..18)  ← gerakan bertahap, mirip manusia
      2. delay 150-350ms
      3. mouse.down + delay 50-150ms + mouse.up
    """
    try:
        frame_element = await frame.frame_element()
        bbox = await frame_element.bounding_box()
        if not bbox or bbox["width"] < 10 or bbox["height"] < 10:
            log.warning(f"[CLICK] bbox invalid: {bbox}")
            return False

        x = bbox["x"] + bbox["width"] * x_ratio + nudge[0]
        y = bbox["y"] + bbox["height"] * y_ratio + nudge[1]
        log.info(
            f"[CLICK] bbox=({bbox['x']:.0f},{bbox['y']:.0f},"
            f"{bbox['width']:.0f}x{bbox['height']:.0f}) "
            f"→ click=({x:.1f},{y:.1f}) nudge={nudge}"
        )

        # Gerakan mouse bertahap (mirip manusia)
        steps = random.randint(10, 18)
        await page.mouse.move(x, y, steps=steps)
        await asyncio.sleep(random.uniform(0.15, 0.35))

        # Tekan dan lepas
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.up()

        return True
    except Exception as e:
        log.warning(f"[CLICK] error: {type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  TOKEN POLLING
# ═══════════════════════════════════════════════════════════════
async def _get_token(page):
    """Ambil token dari 3 sumber."""
    # Sumber 1: window.__ts.token (dari callback kita)
    try:
        t = await page.evaluate(
            "() => (window.__ts && window.__ts.token) ? window.__ts.token : null"
        )
        if t and len(t) > 20:
            return t
    except Exception:
        pass

    # Sumber 2: turnstile.getResponse()
    try:
        t = await page.evaluate("""() => {
            try {
                if (typeof turnstile !== 'undefined' && turnstile.getResponse) {
                    const r = turnstile.getResponse();
                    return r && r.length > 20 ? r : null;
                }
            } catch(_) {}
            return null;
        }""")
        if t and len(t) > 20:
            return t
    except Exception:
        pass

    # Sumber 3: input hidden cf-turnstile-response
    try:
        t = await page.evaluate("""() => {
            const el = document.querySelector(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
            );
            return el && el.value && el.value.length > 20 ? el.value : null;
        }""")
        if t and len(t) > 20:
            return t
    except Exception:
        pass

    return None


async def _get_error(page):
    """Ambil error dari window.__ts.error."""
    try:
        return await page.evaluate(
            "() => (window.__ts && window.__ts.error) ? String(window.__ts.error) : null"
        )
    except Exception:
        return None


async def _get_state_summary(page):
    """Info DOM untuk debug."""
    try:
        return await page.evaluate("""() => {
            const out = {
                iframes: document.querySelectorAll('iframe').length,
                cfIframes: 0,
                hasTurnstile: typeof turnstile !== 'undefined',
                rendered: false,
            };
            for (const f of document.querySelectorAll('iframe')) {
                if (f.src && f.src.includes('challenges.cloudflare.com')) out.cfIframes++;
            }
            try { out.rendered = !!(window.__ts && window.__ts.rendered); } catch(_) {}
            return out;
        }""")
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
#  MAIN SOLVE
# ═══════════════════════════════════════════════════════════════
async def solve_turnstile(domain, sitekey, action=None, cdata=None,
                          proxy=None, timeout=90):
    action_line = f"action: '{action}'," if action else ""
    cdata_line  = f"cData: '{cdata}'," if cdata else ""

    html = TURNSTILE_HTML \
        .replace("__SITEKEY__", sitekey) \
        .replace("__ACTION_LINE__", action_line) \
        .replace("__CDATA_LINE__", cdata_line)

    browser = await _get_browser()
    context_opts = {}
    proxy_dict = _parse_proxy(proxy)
    if proxy_dict:
        context_opts["proxy"] = proxy_dict
        log.info(f"Using proxy: {proxy_dict['server']}")
    else:
        log.info("No proxy")

    context = await browser.new_context(**context_opts)

    try:
        page = await context.new_page()
        target_host = urlparse(domain).netloc

        async def route_handler(route):
            try:
                req = route.request
                req_host = urlparse(req.url).netloc
                if req.resource_type == "document" and req_host == target_host:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=html,
                    )
                else:
                    await route.continue_()
            except Exception as e:
                log.warning(f"route error: {e}")
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", route_handler)
        log.info(f"Navigating to {domain}")
        await page.goto(domain, wait_until="domcontentloaded",
                        timeout=timeout * 1000)

        start = asyncio.get_event_loop().time()
        deadline = start + timeout
        last_log = 0.0

        # ── TAHAP 1: Tunggu iframe Turnstile muncul ──
        log.info("[STEP1] Menunggu iframe Turnstile...")
        frame = await _wait_turnstile_frame(page, timeout=25)
        if frame:
            log.info(f"[STEP1] ✅ Iframe Turnstile ditemukan: {frame.url[:60]}...")
        else:
            log.warning("[STEP1] ❌ Iframe tidak muncul dalam 25s — cek screenshot")

        # ── TAHAP 2: Klik checkbox (kalau ada) ──
        clicked_attempts = 0
        click_offsets = [
            (0.11, 0.50, (0, 0)),      # default: ~28px, ~32px
            (0.11, 0.50, (3, -4)),      # nudge kanan-atas
            (0.11, 0.50, (-3, 4)),      # nudge kiri-bawah
            (0.13, 0.48, (0, 0)),       # variasi posisi
            (0.09, 0.52, (0, 0)),
        ]

        if frame:
            log.info("[STEP2] Coba klik checkbox...")
            for x_r, y_r, nudge in click_offsets:
                if asyncio.get_event_loop().time() > deadline:
                    break
                clicked_attempts += 1
                await _click_checkbox(page, frame, x_ratio=x_r,
                                     y_ratio=y_r, nudge=nudge)

                # Tunggu reaksi CF: 8 detik
                click_deadline = asyncio.get_event_loop().time() + 8
                while asyncio.get_event_loop().time() < click_deadline:
                    tok = await _get_token(page)
                    if tok:
                        log.info(f"[STEP2] ✅ Token muncul setelah klik #{clicked_attempts}")
                        return {"success": True, "token": tok}
                    await asyncio.sleep(1)

                # Cek error
                err = await _get_error(page)
                if err:
                    log.warning(f"[STEP2] error setelah klik: {err}")
                    if err == 'script_not_loaded':
                        return {"success": False,
                                "error": f"turnstile error-callback: {err}"}

                log.info(f"[STEP2] Klik #{clicked_attempts} belum berhasil, coba posisi lain...")

        # ── TAHAP 3: Polling token tanpa klik (invisible challenge) ──
        log.info("[STEP3] Polling token (invisible challenge mode)...")
        while asyncio.get_event_loop().time() < deadline:
            tok = await _get_token(page)
            if tok:
                log.info(f"[STEP3] ✅ Token OK ({len(tok)} chars)")
                return {"success": True, "token": tok}

            err = await _get_error(page)
            if err:
                log.warning(f"[STEP3] Widget error: {err}")
                return {"success": False,
                        "error": f"turnstile error-callback: {err}"}

            now = asyncio.get_event_loop().time()
            if now - last_log >= 5:
                state = await _get_state_summary(page)
                elapsed = int(now - start)
                log.info(f"[STEP3] [{elapsed}s] state={state}")
                last_log = now

            await asyncio.sleep(1)

        return {"success": False, "error": "timeout waiting for token"}

    finally:
        try:
            await context.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  SHUTDOWN
# ═══════════════════════════════════════════════════════════════
async def shutdown_browser():
    global _pw, _browser
    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = None
    _browser = None
