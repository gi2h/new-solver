"""
Turnstile solver pakai Patchright.

Fix: gunakan .replace() bukan .format() untuk hindari KeyError
karena template HTML berisi kurung kurawal JS object literal.
"""
import asyncio
import logging
from urllib.parse import urlparse
from patchright.async_api import async_playwright

log = logging.getLogger("solver")

_pw = None
_browser = None
_browser_lock = asyncio.Lock()

# Template HTML — placeholder pakai __XXX__ agar tidak bentrok dengan sintaks JS
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
                    },
                    'error-callback': function (err) {
                        window.__ts.error = String(err);
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


async def solve_turnstile(domain, sitekey, action=None, cdata=None,
                          proxy=None, timeout=90):
    action_line = f"action: '{action}'," if action else ""
    cdata_line  = f"cData: '{cdata}'," if cdata else ""

    # Gunakan replace, bukan .format()
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

        deadline = asyncio.get_event_loop().time() + timeout
        last_log = 0.0

        while asyncio.get_event_loop().time() < deadline:
            token = None
            error = None
            rendered = False

            try:
                token = await page.evaluate(
                    "() => (window.__ts && window.__ts.token) ? window.__ts.token : null"
                )
            except Exception as e:
                log.warning(f"eval token err: {type(e).__name__}: {e}")

            if not token:
                try:
                    error = await page.evaluate(
                        "() => (window.__ts && window.__ts.error) ? String(window.__ts.error) : null"
                    )
                except Exception as e:
                    log.warning(f"eval error err: {type(e).__name__}: {e}")

            try:
                rendered = bool(await page.evaluate(
                    "() => !!(window.__ts && window.__ts.rendered)"
                ))
            except Exception:
                pass

            if token and len(token) > 10:
                log.info(f"Token OK ({len(token)} chars)")
                return {"success": True, "token": token}

            if error:
                log.warning(f"Widget error: {error}")
                return {"success": False,
                        "error": f"turnstile error-callback: {error}"}

            now = asyncio.get_event_loop().time()
            if now - last_log >= 5:
                log.info(f"Waiting rendered={rendered} t={int(now - (deadline - timeout))}s")
                last_log = now

            await asyncio.sleep(1)

        return {"success": False, "error": "timeout waiting for token"}

    finally:
        try:
            await context.close()
        except Exception:
            pass


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
