"""
Chrome DevTools Protocol (CDP) based OAuth monitor.

This module provides tools for monitoring Steam's CEF browser for OAuth 
authorization codes from Epic, GOG, and Amazon logins.
"""
import asyncio
import json
import logging
import re
import urllib.request
from urllib.parse import urlparse, parse_qs
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


class CDPOAuthMonitor:
    """Monitor Steam's CEF browser for OAuth authorization codes via Chrome DevTools Protocol"""

    def __init__(self, cef_port=8080):
        self.cef_url = f'http://127.0.0.1:{cef_port}/json'
        self.monitored_urls = set()

    async def monitor_for_oauth_code(self, expected_store='epic', timeout=300, poll_interval=0.5) -> Tuple[Optional[str], Optional[str]]:
        """
        Monitor CEF pages for OAuth redirect URLs and extract authorization codes

        Args:
            expected_store: Only return codes for this store ('epic', 'gog', or 'amazon')
            timeout: Maximum time to monitor in seconds (default 5 minutes)
            poll_interval: How often to check CEF pages in seconds (default 0.5s)

        Returns:
            (code, store) tuple or (None, None) if timeout/error
        """
        import time

        start_time = time.time()
        logger.info("[CDP] Starting OAuth code monitoring...")

        while time.time() - start_time < timeout:
            try:
                # Get current CEF pages
                with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                    pages = json.loads(response.read().decode())

                for page in pages:
                    url = page.get('url', '')

                    # Skip already monitored URLs
                    if url in self.monitored_urls:
                        continue

                    self.monitored_urls.add(url)

                    # Check for OAuth patterns
                    if any(p in url.lower() for p in ['auth', 'login', 'code=', 'epiclogin', 'on_login_success', 'oauth', 'authorizationcode', '/id/api/redirect']):
                        logger.info(f"[CDP] OAuth page detected: {url[:80]}...")

                        # Special handling for Epic's redirect page (code in JSON body)
                        if '/id/api/redirect' in url or 'epicgames.com' in url.lower():
                            code = await self._extract_epic_code_from_page(url)
                            if code:
                                # Only return if it matches expected store
                                if expected_store == 'epic':
                                    logger.info(f"[CDP] ✓ Found epic authorization code from page content (matches expected: {expected_store})")
                                    return code, 'epic'
                                else:
                                    logger.warning(f"[CDP] Ignoring epic code (expected: {expected_store})")

                        # Try to extract code from URL
                        code, store = self._extract_code(url)
                        if code:
                            # Only return if it matches expected store
                            if store == expected_store:
                                logger.info(f"[CDP] ✓ Found {store} authorization code (matches expected: {expected_store})")
                                return code, store
                            else:
                                logger.warning(f"[CDP] Ignoring {store} code (expected: {expected_store})")

            except Exception as e:
                logger.debug(f"[CDP] Polling error (normal): {e}")

            await asyncio.sleep(poll_interval)

        logger.warning("[CDP] OAuth monitoring timeout - no code found")
        return None, None

    async def close_page_by_url(self, url_pattern: str) -> bool:
        """Close browser page matching URL pattern via CDP"""
        try:
            # Get current CEF pages
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            # Find page matching URL pattern
            for page in pages:
                if url_pattern in page.get('url', ''):
                    ws_url = page.get('webSocketDebuggerUrl')

                    if ws_url:
                        logger.info(f"[CDP] Closing page via CDP: {page.get('url', '')[:80]}...")

                        import websockets

                        async with websockets.connect(ws_url, ping_interval=None) as websocket:
                            await websocket.send(json.dumps({
                                'id': 1,
                                'method': 'Page.close',
                                'params': {}
                            }))
                            logger.info(f"[CDP] ✓ Page close command sent")
                            return True

            logger.warning(f"[CDP] No page found matching: {url_pattern}")
            return False

        except Exception as e:
            logger.error(f"[CDP] Error closing page: {e}")
            return False

    async def refresh_page_by_url(self, url_pattern: str) -> bool:
        """Refresh/reload a browser page matching URL pattern via CDP"""
        try:
            # Get current CEF pages
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            # Find page matching URL pattern
            for page in pages:
                if url_pattern in page.get('url', ''):
                    ws_url = page.get('webSocketDebuggerUrl')

                    if ws_url:
                        logger.info(f"[CDP] Refreshing page via CDP: {page.get('url', '')[:80]}...")

                        import websockets

                        async with websockets.connect(ws_url, ping_interval=None) as websocket:
                            await websocket.send(json.dumps({
                                'id': 1,
                                'method': 'Page.reload',
                                'params': {'ignoreCache': True}
                            }))
                            logger.info(f"[CDP] ✓ Page refresh command sent")
                            return True

            logger.warning(f"[CDP] No page found matching: {url_pattern}")
            return False

        except Exception as e:
            logger.error(f"[CDP] Error refreshing page: {e}")
            return False

    async def clear_cookies_for_domain(self, domain: str) -> bool:
        """Clear browser cookies for specific domain via CDP"""
        try:
            logger.info(f"[CDP] Clearing cookies for domain: {domain}")

            # Get any CEF page to connect to CDP
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            if not pages:
                logger.error("[CDP] No pages available for CDP connection")
                return False

            # Use first available page
            ws_url = pages[0].get('webSocketDebuggerUrl')
            if not ws_url:
                logger.error("[CDP] No WebSocket URL available")
                return False

            # Connect and clear cookies
            import websockets

            async with websockets.connect(ws_url, ping_interval=None) as websocket:
                # Clear cookies for domain
                await websocket.send(json.dumps({
                    'id': 1,
                    'method': 'Network.clearBrowserCookies',
                    'params': {}
                }))

                response_text = await asyncio.wait_for(websocket.recv(), timeout=5)
                logger.info(f"[CDP] ✓ Cleared browser cookies for {domain}")
                return True

        except Exception as e:
            logger.error(f"[CDP] Error clearing cookies: {e}")
            return False

    async def _extract_epic_code_from_page(self, url) -> Optional[str]:
        """Extract authorizationCode from browser page via CDP WebSocket"""
        try:
            logger.info(f"[CDP] Getting page details for: {url[:80]}...")

            # Get page info from CDP to find WebSocket debugger URL
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            # Find the page matching this URL
            target_page = None
            for page in pages:
                if url in page.get('url', ''):
                    target_page = page
                    break

            if not target_page or 'webSocketDebuggerUrl' not in target_page:
                logger.error(f"[CDP] Could not find page or WebSocket URL for: {url[:80]}")
                return None

            ws_url = target_page['webSocketDebuggerUrl']
            logger.info(f"[CDP] Connecting to page via WebSocket...")

            # Connect via WebSocket and get page content
            import websockets

            async with websockets.connect(ws_url, ping_interval=None) as websocket:
                # Send Runtime.evaluate command to get page text content
                await websocket.send(json.dumps({
                    'id': 1,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': 'document.body.innerText',
                        'returnByValue': True
                    }
                }))

                # Wait for response
                response_text = await asyncio.wait_for(websocket.recv(), timeout=5)
                response_data = json.loads(response_text)

                # Extract the page content from CDP response
                if 'result' in response_data and 'result' in response_data['result']:
                    page_content = response_data['result']['result'].get('value', '')
                    logger.info(f"[CDP] Got page content from browser: {len(page_content)} chars")

                    # Look for authorizationCode in the JSON content
                    match = re.search(r'"authorizationCode"\s*:\s*"([^"]+)"', page_content)
                    if match:
                        code = match.group(1)
                        logger.info(f"[CDP] ✓ Extracted authorizationCode from browser page")
                        return code

                    logger.info(f"[CDP] No authorizationCode in page content (first 200 chars): {page_content[:200]}")
                    return None
                else:
                    logger.error(f"[CDP] Unexpected response format: {response_data}")
                    return None

        except Exception as e:
            logger.error(f"[CDP] Error extracting Epic code via WebSocket: {e}")
            return None

    def _extract_code(self, url) -> Tuple[Optional[str], Optional[str]]:
        """Extract OAuth code from URL"""
        # Epic style (check first - more specific)
        if 'authorizationCode=' in url:
            match = re.search(r'authorizationCode=([^&\s]+)', url)
            if match:
                return match.group(1), 'epic'

        # Amazon style - looks for openid.oa2.authorization_code in URL
        if 'amazon.com' in url.lower() and 'openid.oa2.authorization_code=' in url:
            match = re.search(r'openid\.oa2\.authorization_code=([^&\s]+)', url)
            if match:
                return match.group(1), 'amazon'

        # GOG style
        if 'code=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'code' in params:
                return params['code'][0], 'gog'

        return None, None
