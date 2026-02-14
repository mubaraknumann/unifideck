import aiohttp
import asyncio
import json
import re
from typing import Optional

# Known CEF tab identifiers for Steam's library UI (SP = Steam Platform)
_SP_TAB_PATTERNS = [
    "SP",                          # Big Picture / Steam Deck mode title
    "steamloopback.host",          # URL pattern for SP tab
    "Valve Steam Gamepad/default", # URL pattern variant
    "Valve%20Steam%20Gamepad",     # URL-encoded variant
]


def _is_sp_tab(page: dict) -> bool:
    """Check if a CEF page entry is the Steam Platform (library UI) tab."""
    title = page.get("title", "")
    url = page.get("url", "")
    page_type = page.get("type", "")

    # Must be a page target (not service worker, background, etc.)
    if page_type and page_type != "page":
        return False

    for pattern in _SP_TAB_PATTERNS:
        if pattern in title or pattern in url:
            return True
    return False


def _escape_css_for_template_literal(css: str) -> str:
    """Escape CSS content so it's safe inside a JS template literal."""
    # Escape backticks and ${...} sequences that would break template literals
    css = css.replace("\\", "\\\\")
    css = css.replace("`", "\\`")
    css = css.replace("${", "\\${")
    return css


class UnifideckCDPClient:
    """Chrome DevTools Protocol client for CSS injection.

    Connects to Steam's SP (Steam Platform) CEF tab specifically,
    NOT the browser-level endpoint, to ensure CSS is injected into
    the correct DOM that hosts the game details page.
    """

    def __init__(self):
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.client: Optional[aiohttp.ClientSession] = None
        self.ws_url: Optional[str] = None
        self.msg_id = 0
        self.connected = False

    async def connect(self):
        """Connect to Steam's SP (library UI) tab via CDP.

        Uses /json (page list) instead of /json/version (browser-level)
        to enumerate all CEF targets and find the SP tab specifically.
        This matches the pattern used by the OAuth monitor in auth/browser.py.
        """
        try:
            # Step 1: List all CEF page targets
            async with aiohttp.ClientSession() as web:
                res = await web.get(
                    "http://127.0.0.1:8080/json",
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                if res.status != 200:
                    raise Exception(f"CDP page list returned {res.status}")

                pages = await res.json()

            # Step 2: Find the SP (Steam Platform / library) tab
            sp_page = None
            for page in pages:
                if _is_sp_tab(page):
                    ws_url = page.get("webSocketDebuggerUrl")
                    if ws_url:
                        sp_page = page
                        break

            if sp_page is None:
                # Log available targets for debugging
                available = [
                    f"  title={p.get('title','?')!r}  url={p.get('url','?')[:60]}  type={p.get('type','?')}"
                    for p in pages
                ]
                print(f"[Unifideck CDP] SP tab not found. Available targets ({len(pages)}):\n" + "\n".join(available))
                raise Exception(
                    f"Steam SP tab not found among {len(pages)} CEF targets. "
                    "Ensure Steam is running with --remote-debugging-port=8080"
                )

            self.ws_url = sp_page["webSocketDebuggerUrl"]
            print(f"[Unifideck CDP] Found SP tab: title={sp_page.get('title')!r}, url={sp_page.get('url','')[:60]}")

            # Step 3: Connect websocket to SP tab
            self.client = aiohttp.ClientSession()
            self.websocket = await self.client.ws_connect(self.ws_url)
            self.connected = True

            print("[Unifideck CDP] Connected to Steam SP tab")

        except Exception as e:
            print(f"[Unifideck CDP] Failed to connect: {e}")
            self.connected = False
            raise

    async def disconnect(self):
        """Close CDP connection"""
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.websocket = None
        self.client = None
        self.ws_url = None
        self.connected = False

    async def execute_js(self, js: str) -> dict:
        """Execute JavaScript via Runtime.evaluate with proper timeout.

        Returns the CDP response dict. Raises on timeout, disconnect, or
        if the evaluated JS threw an exception.
        """
        if not self.connected or not self.websocket:
            raise Exception("CDP not connected")

        self.msg_id += 1
        msg_id = self.msg_id

        await self.websocket.send_json({
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "userGesture": True,
                "awaitPromise": False,
                "returnByValue": True,
            },
        })

        # Wait for the matching response with a hard timeout
        async def _wait_for_response():
            async for msg in self.websocket:
                data = msg.json()
                if data.get("id") == msg_id:
                    return data
                # Discard unrelated CDP events/responses (keep reading)
            raise Exception("CDP connection closed while waiting for response")

        try:
            result = await asyncio.wait_for(_wait_for_response(), timeout=8.0)
        except asyncio.TimeoutError:
            raise Exception("CDP timeout waiting for response (8s)")

        # Validate: check for JS exceptions in the result
        if "exceptionDetails" in result.get("result", {}):
            exc = result["result"]["exceptionDetails"]
            text = exc.get("text", "")
            desc = exc.get("exception", {}).get("description", "")
            raise Exception(f"CDP JS exception: {text} — {desc}")

        return result

    async def inject_hide_css(self, appId: int, css_rules: str) -> str:
        """Inject CSS to hide native PlaySection in the SP tab's DOM.

        Args:
            appId: Steam app ID
            css_rules: CSS rules from frontend (with current class names from Decky)

        Returns:
            The style element ID
        """
        css_id = f"unifideck-hide-native-play-{appId}"
        safe_css = _escape_css_for_template_literal(css_rules)

        js = f"""
(function() {{
    var styleId = '{css_id}';
    if (document.getElementById(styleId)) return styleId;

    var style = document.createElement('style');
    style.id = styleId;
    style.textContent = `{safe_css}`;
    document.head.appendChild(style);

    console.log('[Unifideck CDP] Injected hide CSS: ' + styleId);
    return styleId;
}})()
        """

        result = await self.execute_js(js)

        # Validate the return value
        ret_val = result.get("result", {}).get("result", {}).get("value")
        if ret_val != css_id:
            print(f"[Unifideck CDP] Warning: inject returned {ret_val!r}, expected {css_id!r}")

        print(f"[Unifideck CDP] Injected hide CSS for app {appId}")
        return css_id

    async def remove_hide_css(self, appId: int):
        """Remove CSS for specific app"""
        css_id = f"unifideck-hide-native-play-{appId}"

        js = f"""
(function() {{
    var el = document.getElementById('{css_id}');
    if (el) {{
        el.remove();
        console.log('[Unifideck CDP] Removed hide CSS: {css_id}');
        return true;
    }}
    return false;
}})()
        """

        result = await self.execute_js(js)
        removed = result.get("result", {}).get("result", {}).get("value", False)
        if removed:
            print(f"[Unifideck CDP] Removed hide CSS for app {appId}")
        else:
            print(f"[Unifideck CDP] No hide CSS found to remove for app {appId}")

    async def remove_all_hide_css(self):
        """Remove all Unifideck hide CSS"""
        js = """
(function() {
    var els = document.querySelectorAll('[id^="unifideck-hide-native-play-"]');
    var count = els.length;
    els.forEach(function(el) { el.remove(); });
    console.log('[Unifideck CDP] Removed ' + count + ' hide CSS elements');
    return count;
})()
        """

        result = await self.execute_js(js)
        count = result.get("result", {}).get("result", {}).get("value", 0)
        print(f"[Unifideck CDP] Removed {count} hide CSS elements")

    async def hide_native_play_section(self, appId: int) -> bool:
        """Hide native Play button area by finding it in DOM and hiding its container.

        Strategy: Find the native Play/Install button by its text content,
        walk up 4 parent levels to the section container, and set display:none.
        Uses a data attribute marker for reliable unhiding.
        NOTE: No "already_hidden" check - must re-hide after every React re-render.
        """
        app_id_str = str(appId)
        js = (
            '(function() {\n'
            '    var appId = "' + app_id_str + '";\n'
            '    var buttons = document.querySelectorAll(\'button, [class*="Focusable"]\');\n'
            '    var playBtn = null;\n'
            '    for (var i = 0; i < buttons.length; i++) {\n'
            '        var btn = buttons[i];\n'
            '        // Skip buttons inside our custom play section\n'
            '        var parent = btn;\n'
            '        var isCustom = false;\n'
            '        while (parent) {\n'
            '            if (parent.getAttribute && parent.getAttribute("data-unifideck-play-wrapper") === "true") {\n'
            '                isCustom = true;\n'
            '                break;\n'
            '            }\n'
            '            parent = parent.parentElement;\n'
            '        }\n'
            '        if (isCustom) continue;\n'
            '        \n'
            '        // Skip already hidden elements\n'
            '        var alreadyHidden = btn;\n'
            '        var isHidden = false;\n'
            '        while (alreadyHidden) {\n'
            '            if (alreadyHidden.getAttribute && alreadyHidden.getAttribute("data-unifideck-hidden-native")) {\n'
            '                isHidden = true;\n'
            '                break;\n'
            '            }\n'
            '            alreadyHidden = alreadyHidden.parentElement;\n'
            '        }\n'
            '        if (isHidden) continue;\n'
            '        \n'
            '        var txt = btn.textContent.trim();\n'
            '        if (/^(Play|Install|Stream|Resume|Update|Pre-load|Pre-Load|Downloading|Download)$/i.test(txt)) {\n'
            '            var rect = btn.getBoundingClientRect();\n'
            '            if (rect.width > 100 && rect.height > 30) {\n'
            '                playBtn = btn;\n'
            '                break;\n'
            '            }\n'
            '        }\n'
            '    }\n'
            '    if (!playBtn) {\n'
            '        console.log("[Unifideck CDP] No visible native play button found for app " + appId);\n'
            '        return "not_found";\n'
            '    }\n'
            '    var container = playBtn;\n'
            '    for (var level = 0; level < 4; level++) {\n'
            '        if (container.parentElement) {\n'
            '            container = container.parentElement;\n'
            '        } else {\n'
            '            break;\n'
            '        }\n'
            '    }\n'
            '    container.setAttribute("data-unifideck-hidden-native", appId);\n'
            '    container.style.setProperty("display", "none", "important");\n'
            '    container.style.setProperty("visibility", "hidden", "important");\n'
            '    container.style.setProperty("pointer-events", "none", "important");\n'
            '    console.log("[Unifideck CDP] Hidden native play section for app " + appId);\n'
            '    return "hidden";\n'
            '})()'
        )

        result = await self.execute_js(js)
        value = result.get("result", {}).get("result", {}).get("value", "error")
        print(f"[Unifideck CDP] hide_native_play_section({appId}) => {value}")
        return value == "hidden"

    async def unhide_native_play_section(self, appId: int) -> bool:
        """Unhide the native Play button area previously hidden for this app."""
        app_id_str = str(appId)
        js = (
            '(function() {\n'
            '    var appId = "' + app_id_str + '";\n'
            '    var el = document.querySelector(\'[data-unifideck-hidden-native="\' + appId + \'"]\');\n'
            '    if (el) {\n'
            '        el.style.removeProperty("display");\n'
            '        el.style.removeProperty("visibility");\n'
            '        el.style.removeProperty("pointer-events");\n'
            '        el.removeAttribute("data-unifideck-hidden-native");\n'
            '        console.log("[Unifideck CDP] Unhidden native play section for app " + appId);\n'
            '        return true;\n'
            '    }\n'
            '    console.log("[Unifideck CDP] No hidden element found for app " + appId);\n'
            '    return false;\n'
            '})()'
        )

        result = await self.execute_js(js)
        value = result.get("result", {}).get("result", {}).get("value", False)
        print(f"[Unifideck CDP] unhide_native_play_section({appId}) => {value}")
        return value

    async def focus_unifideck_button(self, appId: int) -> bool:
        """Focus the Unifideck action button in the SP tab's DOM.

        Finds the first button inside our [data-unifideck-play-wrapper] container,
        calls .focus() on it AND adds the 'gpfocus' class. Steam's gamepad focus
        system uses its own focus manager that adds 'gpfocus' — native .focus()
        alone doesn't trigger it, so we add the class manually for visual feedback.
        Also removes gpfocus from any previously focused element to avoid stale highlights.
        """
        app_id_str = str(appId)
        js = (
            '(function() {\n'
            '    var wrapper = document.querySelector(\'[data-unifideck-play-wrapper="true"]\');\n'
            '    if (!wrapper || wrapper.style.display === "none") {\n'
            '        console.log("[Unifideck CDP] No visible play wrapper found for focus");\n'
            '        return false;\n'
            '    }\n'
            '    var btn = wrapper.querySelector("button");\n'
            '    if (btn) {\n'
            '        // Remove gpfocus from any other element first\n'
            '        var prev = document.querySelectorAll(".gpfocus");\n'
            '        for (var i = 0; i < prev.length; i++) {\n'
            '            prev[i].classList.remove("gpfocus");\n'
            '        }\n'
            '        btn.focus();\n'
            '        btn.classList.add("gpfocus");\n'
            '        console.log("[Unifideck CDP] Focused button + added gpfocus class");\n'
            '        return true;\n'
            '    }\n'
            '    console.log("[Unifideck CDP] No button found in play wrapper");\n'
            '    return false;\n'
            '})()'
        )

        result = await self.execute_js(js)
        value = result.get("result", {}).get("result", {}).get("value", False)
        print(f"[Unifideck CDP] focus_unifideck_button({appId}) => {value}")
        return value


# Module-level singleton
_cdp_client: Optional[UnifideckCDPClient] = None


async def get_cdp_client() -> UnifideckCDPClient:
    """Get or create CDP client singleton"""
    global _cdp_client

    if _cdp_client is None:
        _cdp_client = UnifideckCDPClient()
        await _cdp_client.connect()
    elif not _cdp_client.connected:
        # Stale client — reconnect
        print("[Unifideck CDP] Singleton exists but disconnected, reconnecting...")
        await _cdp_client.disconnect()
        _cdp_client = UnifideckCDPClient()
        await _cdp_client.connect()

    return _cdp_client


async def shutdown_cdp_client():
    """Disconnect CDP client and clean up injected styles first."""
    global _cdp_client

    if _cdp_client:
        try:
            if _cdp_client.connected:
                await _cdp_client.remove_all_hide_css()
        except Exception as e:
            print(f"[Unifideck CDP] Failed to remove styles during shutdown: {e}")
        await _cdp_client.disconnect()
        _cdp_client = None
