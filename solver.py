"""
Turnstile solver pakai Patchright — v4.0

Fitur baru:
  - Persistent context (stealth maksimal Patchright)
  - channel="chrome" (real Chrome, bukan Chromium)
  - no_viewport=True (hilangkan viewport fingerprint)
  - DNS over HTTPS via Cloudflare (--dns-over-https-templates)
  - WebRTC leak protection
  - JS-based console logger (Patchright disable console API)
  - Error classification (fatal vs retryable)
  - Frame refetch setiap klik (hindari "Frame detached")
  - Break on fatal error (600010) — tidak buang waktu retry

Berdasarkan riset:
  - Patchright docs (persistent context + chrome channel)
  - Cloudflare DoH flags (--dns-over-https-templates)
  - Cloudflare error 600010 semantics
  - Multi-solver repositori (Theyka, Scrapling, zendriver)
"""
import asyncio
import logging
import os
import random
from pathlib import Path
from urllib.parse import urlparse
from patchright.async_api import async_playwright

log = logging.getLogger("solver")

# ═══════════════════════════════════════════════════════════════
#  KONFIG
# ═══════════════════════════════════════════════════════════════
USER_DATA_DIR = os.getenv("CHROME_PROFILE", "/tmp/chrome-profile")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")  # "chrome" | "chromium"
DOH_TEMPLATE = "https://cloudflare-dns.com/dns-query"
MAX_DEBUG_FILE = 10

# ── State ──
_pw = None
_context = None
_context_lock = asyncio.Lock()
_debug_counter = 0

# ═══════════════════════════════════════════════════════════════
#  HTML SINTETIS (widget Turnstile)
# ═══════════════════════════════════════════════════════════════
TURNSTILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Verify</title></head>
<body>
    <div class="cf-turnstile"></div>
    <script>
        // JS logger — Patchright mematikan console API, jadi kita simpan ke state.
        window.__tsLogs = [];
        window.__ts = { token: null, error: null, rendered: false };
        var _origLog = console.log;
        function tsLog(m) { try { window.__tsLogs.push(String(m)); if (window.__tsLogs.length > 30) window.__tsLogs.shift(); } catch(_) {} }

        function onTurnstileLoad() {
            try {
                turnstile.render('.cf-turnstile', {
                    sitekey: '__SITEKEY__',
                    __ACTION_LINE__
                    __CDATA_LINE__
                    callback: function (token) {
                        window.__ts.token = String(token);
                        tsLog('TS_SUCCESS_' + token.length);
                    },
                    'error-callback': function (err) {
                        window.__ts.error = String(err);
                        tsLog('TS_ERROR_' + err);
                    },
                    'expired-callback': function () { window.__ts.error = 'expired'; tsLog('TS_EXPIRED'); },
                    'timeout-callback': function () { window.__ts.error = 'timeout'; tsLog('TS_TIMEOUT'); }
                });
                window.__ts.rendered = true;
                tsLog('TS_RENDERED');
            } catch (e) {
                window.__ts.error = 'render_exception: ' + e.message;
                tsLog('TS_RENDER_EXCEPTION_' + e.message);
            }
        }
        setTimeout(function () {
            if (typeof turnstile === 'undefined') {
                window.__ts.error = 'script_not_loaded';
                tsLog('TS_API_NOT_LOADED');
            }
        }, 5000);
    </script>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onTurnstileLoad" defer></script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
#  BROWSER (persistent context — stealth maksimal)
# ═══════════════════════════════════════════════════════════════
async def _get_context(proxy=None):
    """
    Persistent context sesuai Patchright best-practice.
    TIDAK menambah UA / viewport / header custom — Patchright
    sudah handle stealth tanpa perlu override apa pun.
    """
    global _pw, _context

    async with _context_lock:
        # Cek hidup
        alive = False
        if _context is not None:
            try:
                # Persistent context tidak punya is_connected; test dengan page
                _ = _context.pages
                alive = True
            except Exception:
                alive = False

        if alive:
            return _context

        # Bersihkan profile lama
        Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

        if _pw is None:
            _pw = await async_playwright().start()

        # ── Args: DNS + stealth + WebRTC ──
        args = [
            # Sandbox / dev-shm
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",

            # DNS over HTTPS — Cloudflare (encrypt DNS queries)
            f"--dns-over-https-templates={DOH_TEMPLATE}",
            "--enable-features=DnsOverHttps",

            # WebRTC leak protection (jangan expose IP lokal)
            "--force-webrtc-ip-handling-policy=default_public_interface_only",
            "--disable-features=WebRtcHideLocalIpsWithMdns",

            # Disable background services yang bikin noisy
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-client-side-phishing-detection",
            "--disable-crash-reporter",
            "--disable-domain-reliability",
            "--disable-sync",

            # Language
            "--lang=en-US",

            # Jangan muat extension default
            "--disable-default-apps",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
        ]

        # Proxy ke args kalau ada
        if proxy and proxy.get("server"):
            args.append(f"--proxy-server={proxy['server']}")

        log.info(f"Launching persistent context (channel={BROWSER_CHANNEL}, profile={USER_DATA_DIR})")
        try:
            _context = await _pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                channel=BROWSER_CHANNEL,
                headless=False,          # via Xvfb di Docker
                no_viewport=True,        # <- patchright best practice
                # TIDAK set user_agent, locale, timezone, dll.
                args=args,
                timeout=60000,
            )
        except Exception as e:
            # Fallback ke chromium bundled
            log.warning(f"channel={BROWSER_CHANNEL} gagal ({e}); fallback ke bundled chromium")
            _context = await _pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=False,
                no_viewport=True,
                args=args,
                timeout=60000,
            )

        log.info("Browser launched")
        return _context


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
def _find_turnstile_frame(page):
    """Refetch setiap kali dipanggil. Return fresh frame atau None."""
    try:
        for f in page.frames:
            try:
                if f.url.startswith("https://challenges.cloudflare.com"):
                    return f
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _wait_turnstile_frame(page, timeout=25):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        f = _find_turnstile_frame(page)
        if f:
            return f
        await asyncio.sleep(0.5)
    return None


async def _is_frame_alive(frame):
    try:
        if frame.is_detached():
            return False
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  KLIK CHECKBOX
# ═══════════════════════════════════════════════════════════════
async def _click_checkbox(page, x_ratio=0.11, y_ratio=0.50, nudge=(0, 0)):
    """
    Refetch frame setiap kali. Jangan pakai Frame yang di-cache.
    Posisi checkbox di dalam iframe Turnstile standar:
      x = frame.x + frame.width  * 0.11  (≈ 33px untuk widget 300px)
      y = frame.y + frame.height * 0.50  (≈ 32px untuk widget 65px)
    """
    frame = _find_turnstile_frame(page)
    if frame is None:
        log.warning("[CLICK] tidak ada frame Turnstile")
        return False

    if not await _is_frame_alive(frame):
        log.warning("[CLICK] frame detached")
        return False

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
            f"{bbox['width']:.0f}x{bbox['height']:.0f}) → ({x:.1f},{y:.1f})"
        )

        steps = random.randint(10, 18)
        await page.mouse.move(x, y, steps=steps)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.up()
        return True
    except Exception as e:
        log.warning(f"[CLICK] error: {type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  TOKEN / ERROR POLLING
# ═══════════════════════════════════════════════════════════════
async def _get_token(page):
    # Sumber 1: window.__ts.token
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

    # Sumber 3: input hidden
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
    try:
        return await page.evaluate(
            "() => (window.__ts && window.__ts.error) ? String(window.__ts.error) : null"
        )
    except Exception:
        return None


async def _get_js_logs(page):
    try:
        return await page.evaluate("() => (window.__tsLogs || []).slice(-5)")
    except Exception:
        return []


async def _get_state(page):
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


async def _save_debug(page, sitekey):
    """Screenshot + HTML dump saat gagal."""
    global _debug_counter
    _debug_counter += 1
    if _debug_counter > MAX_DEBUG_FILE:
        return
    try:
        d = "/tmp/ts-debug"
        os.makedirs(d, exist_ok=True)
        tag = f"{_debug_counter}-{sitekey[:10]}"
        await page.screenshot(path=f"{d}/{tag}.png", full_page=True)
        html = await page.content()
        with open(f"{d}/{tag}.html", "w") as f:
            f.write(html)
        log.info(f"[DEBUG] saved {d}/{tag}.png + .html")
    except Exception as e:
        log.warning(f"[DEBUG] save failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  ERROR CLASSIFICATION
# ═══════════════════════════════════════════════════════════════
FATAL_ERRORS = {
    "600010",   # generic_challenge_failure (IP/fingerprint blocked)
    "600001",   # configuration error
    "script_not_loaded",
    "expired",
}

def _is_fatal(err: str) -> bool:
    if not err:
        return False
    err = str(err)
    for f in FATAL_ERRORS:
        if f in err:
            return True
    return False


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

    proxy_dict = _parse_proxy(proxy)
    if proxy_dict:
        log.info(f"Proxy: {proxy_dict['server']}")
    else:
        log.info("No proxy")

    context = await _get_context(proxy_dict)
    page = await context.new_page()

    try:
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

        # ── TAHAP 1: deteksi iframe ──
        log.info("[STEP1] Menunggu iframe Turnstile...")
        frame = await _wait_turnstile_frame(page, timeout=25)
        if frame:
            log.info(f"[STEP1] ✅ Iframe ditemukan: {frame.url[:70]}...")
        else:
            log.warning("[STEP1] ❌ Iframe tidak muncul dalam 25s")
            await _save_debug(page, sitekey)

        # ── TAHAP 2: klik checkbox ──
        click_offsets = [
            (0.11, 0.50, (0, 0)),
            (0.11, 0.50, (3, -4)),
            (0.11, 0.50, (-3, 4)),
            (0.13, 0.48, (0, 0)),
            (0.09, 0.52, (0, 0)),
        ]

        if frame:
            log.info("[STEP2] Coba klik checkbox...")
            for idx, (x_r, y_r, nudge) in enumerate(click_offsets, 1):
                if asyncio.get_event_loop().time() > deadline:
                    break

                # Cek frame masih hidup
                if not await _is_frame_alive(_find_turnstile_frame(page)):
                    log.warning(f"[STEP2] Frame detached sebelum klik #{idx}, hentikan")
                    break

                clicked = await _click_checkbox(page, x_ratio=x_r,
                                                y_ratio=y_r, nudge=nudge)
                if not clicked:
                    log.warning(f"[STEP2] Klik #{idx} gagal terkirim")
                    break

                # Tunggu reaksi CF — cek token + error di dalam loop
                click_deadline = asyncio.get_event_loop().time() + 8
                fatal_detected = False
                while asyncio.get_event_loop().time() < click_deadline:
                    tok = await _get_token(page)
                    if tok:
                        log.info(f"[STEP2] ✅ Token setelah klik #{idx} ({len(tok)} chars)")
                        return {"success": True, "token": tok}

                    err = await _get_error(page)
                    if err:
                        log.warning(f"[STEP2] error setelah klik #{idx}: {err}")
                        if _is_fatal(err):
                            fatal_detected = True
                            break
                        # non-fatal, log & tunggu
                    await asyncio.sleep(1)

                if fatal_detected:
                    log.warning(f"[STEP2] error fatal — stop retry klik")
                    await _save_debug(page, sitekey)
                    return {"success": False,
                            "error": f"turnstile error-callback: {await _get_error(page)}"}

                log.info(f"[STEP2] Klik #{idx} belum berhasil, lanjut posisi lain")

        # ── TAHAP 3: polling token (invisible mode) ──
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
                state = await _get_state(page)
                logs = await _get_js_logs(page)
                elapsed = int(now - start)
                log.info(f"[STEP3] [{elapsed}s] state={state} jsLogs={logs}")
                last_log = now

            await asyncio.sleep(1)

        await _save_debug(page, sitekey)
        return {"success": False, "error": "timeout waiting for token"}

    finally:
        try:
            await page.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  SHUTDOWN
# ═══════════════════════════════════════════════════════════════
async def shutdown_browser():
    global _pw, _context
    try:
        if _context:
            await _context.close()
    except Exception:
        pass
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _context = None
    _pw = None
